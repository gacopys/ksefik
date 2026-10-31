from datetime import date

import pytest

from ksefik.store import Filters, Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "store.db")
    yield store
    store.close()


def meta(number="A", **values):
    return {
        "ksefNumber": number,
        "invoiceNumber": f"FV/{number}",
        "issueDate": "2026-10-23",
        "seller": {"nip": "5438539502", "name": "Seller"},
        "buyer": {"identifier": {"value": "7163454949"}, "name": "Buyer"},
        **values,
    }


def test_empty_database(store):
    assert store.count() == 0
    assert store.find("missing") is None
    assert store.get("missing", "issued") is None
    assert store.checkpoint("Subject1") is None
    assert store.last_sync() is None
    assert store.counterparties(None) == []
    assert store.unresolved_sent() == []
    assert store.sent_record("missing") is None


def test_directions_are_independent_and_xml_updates_replace_search_index(store):
    store.upsert_metadata("issued", [meta()], {"A": b"<r>oldword</r>"})
    store.upsert_metadata("received", [meta()], {})
    assert store.count() == 2
    assert store.counterparties("issued") == [("7163454949", "Buyer", 1)]
    assert store.counterparties("received") == [("5438539502", "Seller", 1)]
    assert len(store.counterparties(None)) == 2
    store.set_xml("A", "issued", b"<r>newword</r>")
    assert store.search(Filters(text="oldword")) == []
    assert [r.direction for r in store.search(Filters(text="newword"))] == ["issued"]
    assert not store.get("A", "received")["xml"]


def test_search_order_limit_dates_and_foreign_currency_amounts(store):
    store.upsert_metadata("received", [meta("old", issueDate="2026-10-01"), meta("new")], {})
    store.upsert_metadata("issued", [meta("eur", currency="EUR", netAmount=100, vatAmount=97.75, grossAmount=123)], {})
    row = store.search(Filters(direction="issued"))[0]
    assert (row.net, row.vat, row.gross, row.currency) == (100, 23, 123, "EUR")
    assert row.issue_date == date(2026, 10, 23) and not row.has_xml
    assert store.search(Filters(), limit=1)[0].ksef_number != "old"
    assert [r.ksef_number for r in store.search(Filters(date_to=date(2026, 10, 1)))] == ["old"]
    assert len(store.search(Filters(text='" * OR ()'))) == 0


def test_upsert_rolls_back_entire_package_on_invalid_metadata(store):
    with pytest.raises(KeyError):
        store.upsert_metadata("issued", [meta(), {}], {})
    assert store.count() == 0
    assert store.search(Filters(text="Buyer")) == []


def test_recent_buyers_uses_latest_xml_per_buyer(store):
    store.upsert_metadata("issued", [meta("old", issueDate="2026-10-01")], {"old": b"<old/>"})
    store.upsert_metadata("issued", [meta("new")], {"new": b"<new/>"})
    store.upsert_metadata("issued", [meta("no-xml", issueDate="2026-10-24")], {})
    store.upsert_metadata("received", [meta("received")], {"received": b"<received/>"})
    assert store.recent_buyer_invoices() == [b"<new/>"]
    assert store.recent_buyer_invoices(0) == []


def test_sent_history_tracks_pending_accepted_rejected_and_latest_receipt(store):
    first = store.record_sent(invoice_number="FV/1", status="processing", session_ref="S", invoice_ref="I", xml=b"one")
    second = store.record_sent(invoice_number="FV/2", status="unknown", session_ref="S", xml=b"two")
    assert [r["id"] for r in store.unresolved_sent()] == [first, second]
    store.update_sent(first, status="accepted", ksef_number="K", upo=b"upo")
    store.update_sent(second, status="rejected", message="bad invoice")
    assert store.unresolved_sent() == []
    assert store.sent_record("K")["upo"] == b"upo"
    third = store.record_sent(invoice_number="FV/1", status="accepted", ksef_number="K")
    assert store.sent_record("K")["id"] == third
    assert [r["id"] for r in store.sent_history()] == [third, second, first]


def test_sent_xml_lookup_finds_only_pending_or_accepted_sends(store):
    assert store.sent_for_xml(b"one") is None
    rejected = store.record_sent(invoice_number="FV/1", status="rejected", xml=b"one")
    store.record_sent(invoice_number="FV/1", status="not_submitted", xml=b"one")
    assert store.sent_for_xml(b"one") is None
    pending = store.record_sent(invoice_number="FV/1", status="processing", xml=b"one", file_name="one.xml")
    assert store.sent_for_xml(b"one")["id"] == pending != rejected
    store.update_sent(pending, status="accepted", ksef_number="K")
    assert store.sent_for_xml(b"one")["ksef_number"] == "K"
    assert store.sent_for_xml(b"two") is None


def test_numbers_are_reserved_until_a_send_definitely_failed(store):
    store.upsert_metadata("issued", [meta("K")], {})
    for number, status in (
        ("FV/2", "processing"),
        ("FV/3", "unknown"),
        ("FV/4", "sending"),
        ("FV/5", "rejected"),
        ("FV/6", "not_submitted"),
        ("FV/7", "accepted"),
    ):
        store.record_sent(invoice_number=number, status=status, seller_nip="5438539502")
    store.record_sent(invoice_number="OTHER/1", status="accepted", seller_nip="1111111111")
    assert sorted(store.reserved_numbers("5438539502")) == ["FV/2", "FV/3", "FV/4", "FV/7", "FV/K"]
    assert "OTHER/1" in store.reserved_numbers()
    assert [r["invoice_number"] for r in store.unresolved_sent()] == ["FV/2", "FV/3", "FV/4"]
    assert store.active_attempt("FV/3")["status"] == "unknown"
    assert store.active_attempt("FV/7", "5438539502")["status"] == "accepted"
    assert store.active_attempt("FV/5") is None and store.active_attempt("OTHER/1", "5438539502") is None
    assert store.sent_entry(1)["invoice_number"] == "FV/2" and store.sent_entry(99) is None
