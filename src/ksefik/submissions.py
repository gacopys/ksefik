"""Wysyłki faktur: wspólny zapis przyjętej faktury i rozstrzyganie wysyłek, na których wynik jeszcze czekamy.

Każda przyjęta faktura - wysłana z edytora, sprawdzona ponownie, odnaleziona po restarcie albo wysłana z pliku -
przechodzi przez `finalize_accepted`. Najpierw zapis w bazie, dopiero potem pliki na dysku: błąd zapisu pliku
nie może zmienić wyniku wysyłki. `reconcile` nigdy nie uznaje faktury za odrzuconą na podstawie błędu połączenia.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .client import (
    AuthError,
    InvoiceRejected,
    KsefClient,
    KsefError,
    RateLimitError,
    StillProcessing,
    find_session_invoice,
    parse_dt,
)
from .config import os_error_text
from .crypto import sha256_b64
from .fa_parser import parse_draft, parse_invoice
from .profile import Party, Profile
from .store import Store, now_iso

# Wysyłka bez śladu w sesji KSeF po tym czasie uznawana jest za niedostarczoną.
NOT_DELIVERED_AFTER = timedelta(days=1)
INVOICE_TYPES = {"VAT": "Vat", "KOR": "Kor", "ZAL": "Zal", "ROZ": "Roz", "UPR": "Upr", "KOR_ZAL": "KorZal"}


def buyer_identifier(party: Party) -> dict[str, str]:
    if party.nip:
        return {"type": "Nip", "value": party.nip}
    if party.vat_ue:
        return {"type": "VatUe", "value": party.vat_ue}
    if party.nr_id:
        return {"type": "Other", "value": party.nr_id}
    return {"type": "None"}


def metadata_from_xml(xml: bytes, ksef_number: str, acquired: datetime | None = None) -> dict[str, Any]:
    """Metadane w formacie KSeF odczytane z treści faktury (gdy nie mamy ich z eksportu)."""
    view = parse_invoice(xml)
    try:
        buyer = buyer_identifier(parse_draft(xml).buyer)
    except (ValueError, TypeError):
        buyer = {"type": "Nip", "value": view.buyer.nip} if view.buyer.nip else {"type": "None"}
    return {
        "ksefNumber": ksef_number,
        "invoiceNumber": view.number,
        "invoiceType": INVOICE_TYPES.get(view.kind_code, view.kind_code or "Vat"),
        "issueDate": view.issue_date.isoformat() if view.issue_date else "",
        "acquisitionDate": (acquired or datetime.now(UTC)).isoformat(),
        "seller": {"nip": view.seller.nip, "name": view.seller.name},
        "buyer": {"identifier": buyer, "name": view.buyer.name},
        "netAmount": float(view.net_total),
        "vatAmount": float(view.vat_total),
        "grossAmount": float(view.total if view.total is not None else view.net_total + view.vat_total),
        "currency": view.currency,
        "invoiceHash": sha256_b64(xml),
    }


def _file_base(number: str, ksef_number: str) -> str:
    return (re.sub(r"[^\w.-]+", "-", number).strip("-") or "faktura") + f"_{ksef_number}"


@dataclass
class Exported:
    """Wynik zapisu plików przyjętej faktury w katalogu wystawione/ profilu."""

    folder: Path
    xml_path: Path | None = None
    upo_path: Path | None = None
    error: str = ""  # opis błędu zapisu (faktura i tak pozostaje przyjęta)


def export_files(profile: Profile, number: str, ksef_number: str, xml: bytes | None, upo: bytes | None) -> Exported:
    folder = profile.data_dir / "wystawione"
    result = Exported(folder)
    base = _file_base(number, ksef_number)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if xml:
            result.xml_path = folder / f"{base}.xml"
            result.xml_path.write_bytes(xml)
        if upo:
            result.upo_path = folder / f"{base}_UPO.xml"
            result.upo_path.write_bytes(upo)
    except OSError as exc:
        result.error = os_error_text(exc)
        result.xml_path = result.upo_path = None
    return result


def finalize_accepted(
    store: Store,
    profile: Profile,
    sent_id: int,
    ksef_number: str,
    *,
    acquired: datetime | None = None,
    upo: bytes | None = None,
    session_ref: str | None = None,
    invoice_ref: str | None = None,
    upo_attempted: bool = True,
) -> Exported:
    """Zapisuje przyjęcie faktury: historia wysyłek → lista wystawionych → pliki. Można wołać wielokrotnie."""
    row = store.sent_entry(sent_id)
    assert row is not None, f"brak wysyłki {sent_id}"
    fields: dict[str, Any] = {
        "status": "accepted",
        "ksef_number": ksef_number,
        "checked_at": now_iso(),
        "message": None,
    }
    if session_ref:
        fields["session_ref"] = session_ref
    if invoice_ref:
        fields["invoice_ref"] = invoice_ref
    if upo:
        fields.update(upo=upo, upo_state="ready")
    elif upo_attempted and row["upo_state"] != "ready":
        fields["upo_state"] = "failed"  # jeszcze niedostępne - pobierzesz później
    store.update_sent(sent_id, **fields)
    if row["draft_id"] is not None:
        store.archive_draft(row["draft_id"])  # szkic zakończony - faktura jest w KSeF

    xml = row["xml"]
    if xml:
        existing = store.get(ksef_number, "issued")
        if existing is None:
            # Przyjęta faktura od razu na liście wystawionych - bez czekania na synchronizację.
            store.upsert_metadata("issued", [metadata_from_xml(xml, ksef_number, acquired)], {ksef_number: xml})
        elif not existing["xml"]:
            store.set_xml(ksef_number, "issued", xml)
    return export_files(profile, row["invoice_number"], ksef_number, xml, upo or row["upo"])


@dataclass
class Reconciled:
    """Stan wysyłki po sprawdzeniu w KSeF."""

    status: str
    message: str = ""
    ksef_number: str = ""
    exported: Exported | None = None
    changed: bool = False


async def reconcile(
    client: KsefClient,
    store: Store,
    profile: Profile,
    row: sqlite3.Row,
    sessions: dict[str, list[dict[str, Any]]] | None = None,
    *,
    timeout: float = 10,
) -> Reconciled:
    """Sprawdza w KSeF wysyłkę bez wyniku i zapisuje jej nowy stan.

    Odrzucenie zapisuje tylko na podstawie jednoznacznego statusu faktury w KSeF. Błąd połączenia zostawia stan
    bez zmian. RateLimitError i AuthError przepuszcza dalej - kolejne zapytania skończyłyby się tak samo.
    """
    sent_id, status, session_ref = row["id"], row["status"], row["session_ref"]
    if not session_ref:
        if status != "sending":
            return Reconciled(status)
        message = "Wysyłka została przerwana przed połączeniem z KSeF - faktura nie została wysłana."
        store.update_sent(sent_id, status="not_submitted", message=message, checked_at=now_iso())
        return Reconciled("not_submitted", message, changed=True)

    invoice_ref = row["invoice_ref"]
    if not invoice_ref:
        sessions = {} if sessions is None else sessions
        try:
            if session_ref not in sessions:
                sessions[session_ref] = await client.session_invoices(session_ref)
        except (RateLimitError, AuthError):
            raise
        except KsefError as exc:
            store.update_sent(sent_id, checked_at=now_iso())
            return Reconciled(status, exc.message)
        found = find_session_invoice(
            sessions[session_ref],
            invoice_hash=row["xml_hash"],
            file_name=row["file_name"],
            invoice_number=row["invoice_number"],
        )
        invoice_ref = (found or {}).get("referenceNumber")
        if not invoice_ref:
            created = datetime.fromisoformat(row["created"])
            if datetime.now(UTC) - created > NOT_DELIVERED_AFTER:
                # Po dobie KSeF dawno skończył przetwarzanie sesji - skoro faktury w niej nie ma, nie dotarła.
                message = f"Faktura nie dotarła do KSeF (brak w sesji {session_ref}). Można ją wysłać ponownie."
                store.update_sent(sent_id, status="not_submitted", message=message, checked_at=now_iso())
                return Reconciled("not_submitted", message, changed=True)
            store.update_sent(sent_id, status="unknown", checked_at=now_iso())
            return Reconciled("unknown", "Faktury jeszcze nie widać w sesji KSeF.", changed=status != "unknown")
        store.update_sent(sent_id, invoice_ref=invoice_ref, status="processing")
        status = "processing"

    try:
        result = await client.wait_for_invoice(session_ref, invoice_ref, timeout=timeout)
    except StillProcessing:
        store.update_sent(sent_id, status="processing", checked_at=now_iso())
        return Reconciled("processing", "KSeF otrzymał fakturę i nadal ją przetwarza.", changed=True)
    except InvoiceRejected as exc:
        store.update_sent(sent_id, status="rejected", message=exc.human(), checked_at=now_iso())
        return Reconciled("rejected", exc.human(), changed=True)
    except (RateLimitError, AuthError):
        raise
    except KsefError as exc:
        store.update_sent(sent_id, checked_at=now_iso())
        return Reconciled(status, exc.message)
    try:
        upo = await client.fetch_upo(session_ref, invoice_ref)
    except KsefError:
        upo = None  # brak UPO nie zmienia przyjęcia faktury
    ksef_number = result["ksefNumber"]
    exported = finalize_accepted(
        store,
        profile,
        sent_id,
        ksef_number,
        acquired=parse_dt(result.get("acquisitionDate")),
        upo=upo,
        session_ref=session_ref,
        invoice_ref=invoice_ref,
    )
    return Reconciled("accepted", ksef_number=ksef_number, exported=exported, changed=True)


async def fetch_upo(client: KsefClient, store: Store, record: sqlite3.Row) -> bytes:
    """UPO przyjętej faktury: z historii wysyłek albo z KSeF (wtedy zapisywane w historii).

    KsefError - UPO jeszcze niedostępne (bywa gotowe dopiero po zamknięciu sesji); stan zapisuje się jako "failed".
    """
    if record["upo"]:
        return record["upo"]
    try:
        upo = await client.upo_by_ksef_number(record["session_ref"], record["ksef_number"])
    except KsefError:
        store.update_sent(record["id"], upo_state="failed")
        raise
    store.update_sent(record["id"], upo=upo, upo_state="ready")
    return upo
