"""Lokalna baza faktur (SQLite + FTS5). Wyszukiwanie i filtrowanie działają offline."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .crypto import sha256_b64
from .fa_parser import searchable_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    ksef_number TEXT NOT NULL,
    direction TEXT NOT NULL,              -- 'issued' | 'received' | 'third' | 'authorized'
    invoice_number TEXT,
    invoice_type TEXT,
    issue_date TEXT,
    acquisition_date TEXT,
    permanent_storage_date TEXT,
    seller_nip TEXT,
    seller_name TEXT,
    buyer_id TEXT,
    buyer_name TEXT,
    counterparty_id TEXT,
    counterparty_name TEXT,
    net REAL,
    vat REAL,
    gross REAL,
    currency TEXT,
    invoice_hash TEXT,
    metadata TEXT,
    xml BLOB,
    PRIMARY KEY (ksef_number, direction)
);
CREATE INDEX IF NOT EXISTS invoices_issue ON invoices(direction, issue_date);
CREATE VIRTUAL TABLE IF NOT EXISTS invoices_fts USING fts5(
    ksef_number UNINDEXED, direction UNINDEXED, content, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TABLE IF NOT EXISTS sync_state (
    subject TEXT PRIMARY KEY,
    checkpoint TEXT,
    last_sync TEXT
);
-- Ostatnia próba synchronizacji każdego rodzaju faktur - także nieudana (pasek stanu mówi, czego brakuje).
CREATE TABLE IF NOT EXISTS sync_attempts (
    subject TEXT PRIMARY KEY,
    attempted TEXT NOT NULL,
    error TEXT,                           -- NULL: udana
    error_kind TEXT                       -- 'permissions' | 'rate_limit' | 'connection' | 'other'
);
-- Historia wysyłek (każda próba wysłania faktury). Wpis powstaje przed połączeniem z KSeF, numery referencyjne
-- trafiają do niego, gdy tylko są znane - po awarii lub zamknięciu aplikacji wynik da się odnaleźć.
CREATE TABLE IF NOT EXISTS sent (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    status TEXT NOT NULL,                 -- zob. SENT_STATES
    ksef_number TEXT,
    session_ref TEXT,
    invoice_ref TEXT,
    message TEXT,
    xml BLOB,
    upo BLOB,
    xml_hash TEXT,                        -- SHA-256 (base64) wysłanego XML - rozpoznaje ponowną wysyłkę pliku
    file_name TEXT,                       -- nazwa pliku przy wysyłce plików XML (wynik w paczce szukamy po nazwie)
    env TEXT,
    context_id TEXT,
    seller_nip TEXT,
    invoice_kind TEXT,
    draft_id INTEGER,
    upo_state TEXT NOT NULL DEFAULT 'none',  -- 'none' | 'ready' | 'failed' (jeszcze niedostępne)
    checked_at TEXT,                      -- ostatnie sprawdzenie statusu w KSeF
    updated TEXT,
    source TEXT NOT NULL DEFAULT 'app',   -- 'app' (wystawiona tutaj) | 'import' (plik XML)
    file_path TEXT
);
CREATE INDEX IF NOT EXISTS sent_hash ON sent(xml_hash);
CREATE INDEX IF NOT EXISTS sent_number ON sent(invoice_number);
"""

DRAFTS_SCHEMA = """
-- Szkice faktur: wszystko, co wpisano w edytorze (także niedokończone), zanim faktura trafi do KSeF.
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    env TEXT,
    context_id TEXT,
    number TEXT,
    kind TEXT,
    buyer_name TEXT,
    gross REAL,
    currency TEXT,
    payload TEXT NOT NULL,                -- JSON: {"draft": projekt faktury, "fields": surowe wartości pól edytora}
    source_ksef TEXT,                     -- faktura, z której powstał szkic (kopia albo korekta)
    archived TEXT                         -- kiedy faktura z tego szkicu została przyjęta przez KSeF
);
-- Ustawienia tej kopii aplikacji, które nie należą do profile.toml (np. ostatni folder zapisu plików).
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Stany wysyłki i ich opis dla człowieka.
SENT_STATES = {
    "sending": "Wysyłanie",
    "processing": "KSeF przetwarza",
    "unknown": "Nie można potwierdzić statusu",
    "not_submitted": "Nie wysłano",
    "accepted": "Przyjęta przez KSeF",
    "rejected": "Odrzucona przez KSeF",
}
UNRESOLVED_STATES = ("sending", "processing", "unknown")
# Wysyłki bez rozstrzygnięcia - ich faktury mogą już być w KSeF, więc nie wolno wysłać ich drugi raz.
UNRESOLVED = "status IN ('sending', 'processing', 'unknown')"
# Numer faktury jest zajęty, dopóki wysyłka nie skończyła się pewną porażką.
RESERVING = "status NOT IN ('not_submitted', 'rejected')"


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def fold(text: str) -> str:
    """Małe litery bez polskich znaków - żeby 'uslugi' znajdowało 'Usługi'."""
    text = text.lower().replace("ł", "l")
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def fts_query(text: str) -> str | None:
    terms = [t for t in re.split(r"\s+", fold(text)) if t]
    phrases = []
    for term in terms:
        term = term.replace('"', "")
        if re.search(r"\w", term):
            phrases.append(f'"{term}"*')
    return " AND ".join(phrases) or None


@dataclass
class InvoiceRow:
    ksef_number: str
    direction: str
    invoice_number: str
    issue_date: date | None
    counterparty_id: str
    counterparty_name: str
    net: float
    vat: float
    gross: float
    currency: str
    has_xml: bool


@dataclass
class Found:
    """Wynik wyszukiwania: pokazane wiersze i pełne liczby dla wszystkich pasujących faktur."""

    rows: list[InvoiceRow]
    total: int = 0  # pasujące wiersze (bez limitu wyświetlania)
    unique: int = 0  # różne faktury - ta sama faktura w dwóch rolach liczy się raz
    without_xml: int = 0  # pasujące, ale bez pobranej treści (szukane tylko po metadanych)
    pln: tuple[float, float, float] = (0.0, 0.0, 0.0)  # netto, VAT, brutto faktur w PLN (każda raz)
    foreign: int = 0  # faktury w innych walutach (nie wliczone do sum)
    error: str = ""  # "syntax" - niedokończone zapytanie; inny tekst - błąd bazy

    @property
    def truncated(self) -> bool:
        return self.total > len(self.rows)


@dataclass
class Filters:
    direction: str | None = None  # None = obie strony
    date_from: date | None = None
    date_to: date | None = None
    counterparty: str | None = None
    text: str = ""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.executescript(DRAFTS_SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ----------------------------------------------------------- zapis faktur

    def upsert_metadata(self, direction: str, items: Iterable[dict[str, Any]], xml: dict[str, bytes]) -> int:
        """Zapisuje metadane (i XML, jeśli jest). Zwraca liczbę faktur, których wcześniej nie było."""
        added = 0
        with self.db:
            for meta in items:
                ksef = meta["ksefNumber"]
                existed = self.db.execute(
                    "SELECT 1 FROM invoices WHERE ksef_number = ? AND direction = ?", (ksef, direction)
                ).fetchone()
                added += existed is None
                seller = meta.get("seller") or {}
                buyer = meta.get("buyer") or {}
                buyer_id = (buyer.get("identifier") or {}).get("value") or ""
                if direction == "issued":
                    cp_id, cp_name = buyer_id, buyer.get("name") or ""
                else:
                    # Dla otrzymanych, podmiotu 3 i upoważnionego kontrahentem jest wystawca faktury.
                    cp_id, cp_name = seller.get("nip") or "", seller.get("name") or ""
                body = xml.get(ksef)
                self.db.execute(
                    """
                    INSERT INTO invoices (ksef_number, direction, invoice_number, invoice_type, issue_date,
                        acquisition_date, permanent_storage_date, seller_nip, seller_name, buyer_id, buyer_name,
                        counterparty_id, counterparty_name, net, vat, gross, currency, invoice_hash, metadata, xml)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT (ksef_number, direction) DO UPDATE SET
                        invoice_number = excluded.invoice_number, invoice_type = excluded.invoice_type,
                        issue_date = excluded.issue_date, acquisition_date = excluded.acquisition_date,
                        permanent_storage_date = excluded.permanent_storage_date,
                        seller_nip = excluded.seller_nip, seller_name = excluded.seller_name,
                        buyer_id = excluded.buyer_id, buyer_name = excluded.buyer_name,
                        counterparty_id = excluded.counterparty_id, counterparty_name = excluded.counterparty_name,
                        net = excluded.net, vat = excluded.vat, gross = excluded.gross,
                        currency = excluded.currency, invoice_hash = excluded.invoice_hash,
                        metadata = excluded.metadata, xml = COALESCE(excluded.xml, invoices.xml)
                    """,
                    (
                        ksef,
                        direction,
                        meta.get("invoiceNumber") or "",
                        meta.get("invoiceType") or "",
                        (meta.get("issueDate") or "")[:10],
                        meta.get("acquisitionDate"),
                        meta.get("permanentStorageDate"),
                        seller.get("nip") or "",
                        seller.get("name") or "",
                        buyer_id,
                        buyer.get("name") or "",
                        cp_id,
                        cp_name,
                        meta.get("netAmount") or 0,
                        meta.get("vatAmount") or 0,
                        meta.get("grossAmount") or 0,
                        meta.get("currency") or "PLN",
                        meta.get("invoiceHash"),
                        json.dumps(meta, ensure_ascii=False),
                        body,
                    ),
                )
                self._reindex(ksef, direction)
        return added

    def _reindex(self, ksef: str, direction: str) -> None:
        row = self.db.execute(
            "SELECT * FROM invoices WHERE ksef_number = ? AND direction = ?", (ksef, direction)
        ).fetchone()
        parts = [
            row["ksef_number"],
            row["invoice_number"],
            row["seller_nip"],
            row["seller_name"],
            row["buyer_id"],
            row["buyer_name"],
        ]
        if row["xml"]:
            parts.append(searchable_text(row["xml"]))
        self.db.execute("DELETE FROM invoices_fts WHERE ksef_number = ? AND direction = ?", (ksef, direction))
        self.db.execute(
            "INSERT INTO invoices_fts (ksef_number, direction, content) VALUES (?, ?, ?)",
            (ksef, direction, fold(" ".join(p for p in parts if p))),
        )

    # -------------------------------------------------------------- odczyt

    def _where(self, filters: Filters) -> tuple[str, list[Any]]:
        where, params = [], []
        if filters.direction:
            where.append("i.direction = ?")
            params.append(filters.direction)
        if filters.date_from:
            where.append("i.issue_date >= ?")
            params.append(filters.date_from.isoformat())
        if filters.date_to:
            where.append("i.issue_date <= ?")
            params.append(filters.date_to.isoformat())
        if filters.counterparty:
            where.append("i.counterparty_id = ?")
            params.append(filters.counterparty)
        if query := fts_query(filters.text):
            where.append(
                "EXISTS (SELECT 1 FROM invoices_fts f WHERE f.ksef_number = i.ksef_number "
                "AND f.direction = i.direction AND invoices_fts MATCH ?)"
            )
            params.append(query)
        return (" WHERE " + " AND ".join(where)) if where else "", params

    def find_invoices(self, filters: Filters, limit: int = 5000) -> Found:
        """Faktury spełniające filtry (najnowsze najpierw, najwyżej `limit`) z liczbami i sumami dla wszystkich."""
        where, params = self._where(filters)
        sql = f"SELECT i.*, i.xml IS NOT NULL AS has_xml FROM invoices i{where}"
        sql += " ORDER BY i.issue_date DESC, i.acquisition_date DESC LIMIT ?"
        try:
            rows = self.db.execute(sql, [*params, limit]).fetchall()
            counts = self.db.execute(
                "SELECT COUNT(*), COUNT(DISTINCT i.ksef_number), COALESCE(SUM(i.xml IS NULL), 0) "
                f"FROM invoices i{where}",
                params,
            ).fetchone()
            # Sumy: każda faktura raz (w zakładce „Wszystkie” ta sama faktura może być w dwóch rolach).
            sums = self.db.execute(
                "SELECT COALESCE(SUM(CASE WHEN currency = 'PLN' THEN net END), 0), "
                "COALESCE(SUM(CASE WHEN currency = 'PLN' THEN vat END), 0), "
                "COALESCE(SUM(CASE WHEN currency = 'PLN' THEN gross END), 0), "
                "COALESCE(SUM(currency != 'PLN'), 0) "
                f"FROM (SELECT i.net, i.vat, i.gross, COALESCE(i.currency, 'PLN') AS currency FROM invoices i{where} "
                "GROUP BY i.ksef_number)",
                params,
            ).fetchone()
        except sqlite3.OperationalError as exc:
            syntax = "fts5" in str(exc) or "syntax" in str(exc)
            return Found([], error="syntax" if syntax else str(exc))
        return Found(
            rows=[self._row(r) for r in rows],
            total=counts[0],
            unique=counts[1],
            without_xml=counts[2],
            pln=(round(sums[0], 2), round(sums[1], 2), round(sums[2], 2)),
            foreign=sums[3],
        )

    @staticmethod
    def _row(r: sqlite3.Row) -> InvoiceRow:
        return InvoiceRow(
            ksef_number=r["ksef_number"],
            direction=r["direction"],
            invoice_number=r["invoice_number"] or "",
            issue_date=date.fromisoformat(r["issue_date"]) if r["issue_date"] else None,
            counterparty_id=r["counterparty_id"] or "",
            counterparty_name=r["counterparty_name"] or "",
            net=r["net"] or 0.0,
            # KSeF podaje VAT zawsze w PLN; dla faktur w walucie obcej pokazujemy VAT w walucie faktury.
            vat=(r["vat"] or 0.0)
            if (r["currency"] or "PLN") == "PLN"
            else round((r["gross"] or 0) - (r["net"] or 0), 2),
            gross=r["gross"] or 0.0,
            currency=r["currency"] or "PLN",
            has_xml=bool(r["has_xml"]),
        )

    def search(self, filters: Filters, limit: int = 5000) -> list[InvoiceRow]:
        return self.find_invoices(filters, limit).rows

    def counterparties(self, direction: str | None) -> list[tuple[str, str, int]]:
        sql = (
            "SELECT counterparty_id, MAX(counterparty_name) AS name, COUNT(*) AS n FROM invoices "
            "WHERE counterparty_id != ''"
        )
        params: list[Any] = []
        if direction:
            sql += " AND direction = ?"
            params.append(direction)
        sql += " GROUP BY counterparty_id ORDER BY name COLLATE NOCASE"
        return [(r["counterparty_id"], r["name"] or r["counterparty_id"], r["n"]) for r in self.db.execute(sql, params)]

    def get(self, ksef_number: str, direction: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM invoices WHERE ksef_number = ? AND direction = ?", (ksef_number, direction)
        ).fetchone()

    def find(self, ksef_number: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM invoices WHERE ksef_number = ? LIMIT 1", (ksef_number,)).fetchone()

    def set_xml(self, ksef_number: str, direction: str, xml: bytes) -> None:
        with self.db:
            self.db.execute(
                "UPDATE invoices SET xml = ? WHERE ksef_number = ? AND direction = ?", (xml, ksef_number, direction)
            )
            self._reindex(ksef_number, direction)

    def reserved_numbers(self, seller_nip: str | None = None) -> list[str]:
        """Zajęte numery faktur: wystawione oraz wysłane, chyba że wysyłka na pewno się nie udała.

        Rezerwacja działa w obrębie tej instalacji: faktur wystawionych w innym programie nie zna przed synchronizacją.
        """
        invoices = "SELECT invoice_number FROM invoices WHERE direction = 'issued'"
        sent = f"SELECT invoice_number FROM sent WHERE {RESERVING}"
        params: list[Any] = []
        if seller_nip:
            invoices += " AND seller_nip = ?"
            sent += " AND (seller_nip IS NULL OR seller_nip = ?)"
            params = [seller_nip, seller_nip]
        return [r[0] for r in self.db.execute(f"{invoices} UNION {sent}", params)]

    def recent_buyer_invoices(self, limit: int = 30) -> list[bytes]:
        """XML najnowszej wystawionej faktury dla każdego nabywcy (do podpowiadania kontrahentów)."""
        rows = self.db.execute(
            """
            SELECT xml FROM invoices i
            WHERE direction = 'issued' AND xml IS NOT NULL AND counterparty_id != '' AND issue_date = (
                SELECT MAX(issue_date) FROM invoices j
                WHERE j.direction = 'issued' AND j.counterparty_id = i.counterparty_id AND j.xml IS NOT NULL
            )
            GROUP BY counterparty_id ORDER BY MAX(issue_date) DESC LIMIT ?
            """,
            (limit,),
        )
        return [r[0] for r in rows]

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]

    # ----------------------------------------------------- stan synchronizacji

    def checkpoint(self, subject: str) -> datetime | None:
        row = self.db.execute("SELECT checkpoint FROM sync_state WHERE subject = ?", (subject,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def set_checkpoint(self, subject: str, value: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sync_state (subject, checkpoint, last_sync) VALUES (?, ?, ?) "
                "ON CONFLICT (subject) DO UPDATE SET checkpoint = excluded.checkpoint, last_sync = excluded.last_sync",
                (subject, value.isoformat(), datetime.now(UTC).isoformat()),
            )

    def last_sync(self, subject: str | None = None) -> datetime | None:
        if subject is None:
            row = self.db.execute("SELECT MIN(last_sync) FROM sync_state").fetchone()
        else:
            row = self.db.execute("SELECT last_sync FROM sync_state WHERE subject = ?", (subject,)).fetchone()
        return datetime.fromisoformat(row[0]) if row and row[0] else None

    def record_sync_attempt(self, subject: str, error: str | None = None, kind: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO sync_attempts (subject, attempted, error, error_kind) VALUES (?, ?, ?, ?) "
                "ON CONFLICT (subject) DO UPDATE SET attempted = excluded.attempted, error = excluded.error, "
                "error_kind = excluded.error_kind",
                (subject, now_iso(), error, kind if error else None),
            )

    def sync_attempt(self, subject: str) -> sqlite3.Row | None:
        """Ostatnia próba synchronizacji danego rodzaju faktur (attempted, error, error_kind) albo None."""
        return self.db.execute("SELECT * FROM sync_attempts WHERE subject = ?", (subject,)).fetchone()

    # ---------------------------------------------------------- ustawienia

    def setting(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ------------------------------------------------- historia wysłanych

    def record_sent(self, **fields: Any) -> int:
        fields.setdefault("created", now_iso())
        fields.setdefault("updated", fields["created"])
        if fields.get("xml") is not None:
            fields.setdefault("xml_hash", sha256_b64(fields["xml"]))
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.db:
            cur = self.db.execute(f"INSERT INTO sent ({cols}) VALUES ({marks})", tuple(fields.values()))
        return int(cur.lastrowid)

    def update_sent(self, sent_id: int, **fields: Any) -> None:
        fields.setdefault("updated", now_iso())
        assignments = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE sent SET {assignments} WHERE id = ?", (*fields.values(), sent_id))

    def unresolved_sent(self) -> list[sqlite3.Row]:
        """Wysyłki bez rozstrzygnięcia (wysyłanie, przetwarzanie, wynik nieznany) - do sprawdzenia w KSeF."""
        return self.db.execute(f"SELECT * FROM sent WHERE {UNRESOLVED} ORDER BY id").fetchall()

    def active_attempt(self, number: str, seller_nip: str | None = None) -> sqlite3.Row | None:
        """Wysyłka faktury o tym numerze, która trwa, czeka na wynik albo została przyjęta."""
        sql = f"SELECT * FROM sent WHERE invoice_number = ? AND ({UNRESOLVED} OR status = 'accepted')"
        params: list[Any] = [number.strip()]
        if seller_nip:
            sql += " AND (seller_nip IS NULL OR seller_nip = ?)"
            params.append(seller_nip)
        return self.db.execute(sql + " ORDER BY id DESC LIMIT 1", params).fetchone()

    # ------------------------------------------------------------------ szkice

    def save_draft(self, draft_id: int | None, payload: dict[str, Any], **summary: Any) -> int:
        """Zapisuje szkic (nowy, gdy draft_id is None). `summary`: number, kind, buyer_name, gross, currency, …"""
        now = now_iso()
        body = json.dumps(payload, ensure_ascii=False)
        with self.db:
            if draft_id is not None and self.draft(draft_id) is not None:
                assignments = ", ".join(f"{k} = ?" for k in summary)
                sql = f"UPDATE drafts SET payload = ?, updated = ?{', ' if summary else ''}{assignments} WHERE id = ?"
                self.db.execute(sql, (body, now, *summary.values(), draft_id))
                return draft_id
            cols = ", ".join(["created", "updated", "payload", *summary])
            marks = ", ".join("?" for _ in range(3 + len(summary)))
            cur = self.db.execute(f"INSERT INTO drafts ({cols}) VALUES ({marks})", (now, now, body, *summary.values()))
            return int(cur.lastrowid)

    def draft(self, draft_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()

    def drafts(self) -> list[sqlite3.Row]:
        """Otwarte szkice (najnowsze najpierw) ze stanem ostatniej wysyłki, jeśli była."""
        return self.db.execute(
            "SELECT d.*, (SELECT status FROM sent s WHERE s.draft_id = d.id ORDER BY s.id DESC LIMIT 1) AS send_status "
            "FROM drafts d WHERE d.archived IS NULL ORDER BY d.updated DESC"
        ).fetchall()

    def draft_numbers(self, exclude: int | None = None) -> list[str]:
        """Numery w otwartych szkicach - żeby dwa szkice nie dostały tego samego kolejnego numeru."""
        rows = self.db.execute(
            "SELECT number FROM drafts WHERE archived IS NULL AND number IS NOT NULL AND id IS NOT ?", (exclude,)
        )
        return [r[0] for r in rows]

    def archive_draft(self, draft_id: int) -> None:
        with self.db:
            self.db.execute("UPDATE drafts SET archived = ? WHERE id = ?", (now_iso(), draft_id))

    def delete_draft(self, draft_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM drafts WHERE id = ?", (draft_id,))

    def import_history(self, limit: int = 100) -> list[sqlite3.Row]:
        """Ostatnia wysyłka każdego pliku XML (najnowsze najpierw)."""
        return self.db.execute(
            "SELECT * FROM sent WHERE id IN (SELECT MAX(id) FROM sent WHERE source = 'import' "
            "GROUP BY COALESCE(xml_hash, id)) ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def sent_history(self, limit: int = 500) -> list[sqlite3.Row]:
        """Ostatnie wysyłki (najnowsze najpierw) - bez treści XML i UPO."""
        return self.db.execute(
            "SELECT id, created, updated, checked_at, invoice_number, status, ksef_number, session_ref, invoice_ref, "
            "message, file_name, source, upo_state, upo IS NOT NULL AS has_upo, invoice_kind, draft_id "
            "FROM sent ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def sent_entry(self, sent_id: int) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM sent WHERE id = ?", (sent_id,)).fetchone()

    def sent_for_xml(self, xml: bytes) -> sqlite3.Row | None:
        """Ostatnia wysyłka tego samego pliku, która jest w toku albo została przyjęta (nie wysyłamy go ponownie)."""
        return self.db.execute(
            f"SELECT * FROM sent WHERE xml_hash = ? AND (status = 'accepted' OR {UNRESOLVED}) ORDER BY id DESC LIMIT 1",
            (sha256_b64(xml),),
        ).fetchone()

    def sent_record(self, ksef_number: str) -> sqlite3.Row | None:
        """Wpis z historii wysyłki (numer sesji i faktury - potrzebne do pobrania UPO)."""
        return self.db.execute(
            "SELECT * FROM sent WHERE ksef_number = ? ORDER BY id DESC LIMIT 1", (ksef_number,)
        ).fetchone()
