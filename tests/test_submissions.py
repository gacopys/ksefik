"""Wspólny zapis przyjętej faktury i rozstrzyganie wysyłek bez wyniku (submissions.py)."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from ksefik import profile as profile_module
from ksefik.client import AuthError, InvoiceRejected, KsefError, RateLimitError, StillProcessing
from ksefik.invoice import build_xml, new_draft
from ksefik.profile import Party
from ksefik.store import Store
from ksefik.submissions import finalize_accepted, metadata_from_xml, reconcile


@pytest.fixture
def env(profile, tmp_path, monkeypatch):
    monkeypatch.setattr(profile_module, "DATA_ROOT", tmp_path / "data")
    store = Store(tmp_path / "s.db")
    draft = new_draft(profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    yield profile, store, draft, xml
    store.close()


def test_finalize_is_idempotent_and_keeps_synced_metadata(env):
    profile, store, draft, xml = env
    sent = store.record_sent(invoice_number=draft.number, status="processing", session_ref="S", xml=xml)
    first = finalize_accepted(store, profile, sent, "K", upo=None, invoice_ref="I")
    row = store.sent_entry(sent)
    assert (row["status"], row["ksef_number"], row["invoice_ref"], row["upo_state"]) == ("accepted", "K", "I", "failed")
    assert store.get("K", "issued")["xml"] == xml and first.xml_path.read_bytes() == xml and first.upo_path is None
    # Ponowne zakończenie (np. sprawdzenie po restarcie) z UPO: bez duplikatów, z UPO na dysku.
    second = finalize_accepted(store, profile, sent, "K", upo=b"<upo/>")
    assert store.count() == 1 and store.sent_entry(sent)["upo_state"] == "ready"
    assert second.upo_path.read_bytes() == b"<upo/>"


def test_finalize_records_acceptance_before_files_and_reports_export_errors(env):
    profile, store, draft, xml = env
    profile.data_dir.mkdir(parents=True)
    (profile.data_dir / "wystawione").write_text("to nie katalog")
    sent = store.record_sent(invoice_number=draft.number, status="processing", xml=xml)
    exported = finalize_accepted(store, profile, sent, "K")
    assert exported.error and exported.xml_path is None
    assert store.sent_entry(sent)["status"] == "accepted" and store.find("K") is not None


def test_metadata_keeps_non_nip_buyer_identity(env):
    profile, _, draft, _ = env
    eu = build_xml(draft.with_changes(buyer=Party(nazwa="GmbH", vat_ue="DE123456789", adres_l1="Str. 1", kraj="DE")))
    assert metadata_from_xml(eu, "K")["buyer"]["identifier"] == {"type": "VatUe", "value": "DE123456789"}
    private = build_xml(draft.with_changes(buyer=Party(nazwa="Jan", brak_id=True, adres_l1="ul. 1")))
    assert metadata_from_xml(private, "K")["buyer"]["identifier"] == {"type": "None"}


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (None, "accepted"),
        (StillProcessing("S", "I"), "processing"),
        (InvoiceRejected("zły NIP", code=450), "rejected"),
        (KsefError("brak połączenia"), "unknown"),  # błąd połączenia nie zmienia stanu
    ],
)
def test_reconcile_maps_only_definitive_statuses(env, failure, expected):
    profile, store, draft, xml = env
    sent = store.record_sent(invoice_number=draft.number, status="unknown", session_ref="S", invoice_ref="I", xml=xml)
    client = AsyncMock()
    client.wait_for_invoice.return_value = {"ksefNumber": "K"}
    client.wait_for_invoice.side_effect = failure
    client.fetch_upo.return_value = b"<upo/>"
    result = run(reconcile(client, store, profile, store.sent_entry(sent)))
    assert result.status == store.sent_entry(sent)["status"] == expected
    if expected == "accepted":
        assert store.find("K") is not None and store.sent_entry(sent)["upo"] == b"<upo/>"


@pytest.mark.parametrize("failure", [RateLimitError("wait"), AuthError("expired", http_status=401)])
def test_reconcile_lets_rate_limits_and_login_errors_through_without_changing_state(env, failure):
    profile, store, draft, xml = env
    sent = store.record_sent(invoice_number=draft.number, status="processing", session_ref="S", invoice_ref="I")
    client = AsyncMock()
    client.wait_for_invoice.side_effect = failure
    with pytest.raises(type(failure)):
        run(reconcile(client, store, profile, store.sent_entry(sent)))
    assert store.sent_entry(sent)["status"] == "processing"


def test_reconcile_send_interrupted_before_connecting_is_not_submitted(env):
    profile, store, draft, xml = env
    sent = store.record_sent(invoice_number=draft.number, status="sending", xml=xml)
    result = run(reconcile(AsyncMock(), store, profile, store.sent_entry(sent)))
    assert result.status == "not_submitted" and draft.number not in store.reserved_numbers()


@pytest.mark.parametrize("age,expected", [(timedelta(hours=2), "unknown"), (timedelta(days=2), "not_submitted")])
def test_reconcile_finds_interrupted_upload_in_its_session_or_releases_it_after_a_day(env, age, expected):
    profile, store, draft, xml = env
    created = (datetime.now(UTC) - age).isoformat()
    sent = store.record_sent(invoice_number=draft.number, status="sending", session_ref="S", xml=xml, created=created)
    client = AsyncMock()
    client.session_invoices.return_value = []
    assert run(reconcile(client, store, profile, store.sent_entry(sent))).status == expected
    # Gdy faktura pojawi się w sesji (szukamy po skrócie XML), sprawdzamy jej status.
    client.session_invoices.return_value = [{"referenceNumber": "I", "invoiceHash": store.sent_entry(sent)["xml_hash"]}]
    client.wait_for_invoice.return_value = {"ksefNumber": "K"}
    client.fetch_upo.return_value = None
    store.update_sent(sent, status="unknown")
    assert run(reconcile(client, store, profile, store.sent_entry(sent))).status == "accepted"
    assert store.sent_entry(sent)["invoice_ref"] == "I"
