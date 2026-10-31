from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from ksefik.crypto import SymmetricKey
from ksefik.invoice import build_xml, new_draft
from ksefik.money import fmt_account, fmt_amount, nip_valid, parse_amount, parse_date
from ksefik.store import Filters, Store, fold, fts_query


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("46696", "46696"),
        ("46 696,00", "46696.00"),
        ("46 696,5", "46696.5"),
        ("46.696,50", "46696.50"),
        ("46,696.50", "46696.50"),
        ("12 345,67 zł", "12345.67"),
        ("abc", None),
        ("", None),
    ],
)
def test_parse_amount(text, expected):
    result = parse_amount(text)
    assert (str(result) if result is not None else None) == expected


def test_fmt_amount_and_account():
    assert fmt_amount(Decimal("57436.08")) == "57\u00a0436,08"  # twarda spacja jako separator tysięcy
    assert fmt_amount(Decimal("-1234.5"), "PLN") == "-1\u00a0234,50 PLN"
    assert fmt_account("61109010140000071219812874") == "61 1090 1014 0000 0712 1981 2874"


def test_parse_date():
    assert parse_date("31.10.2026") == date(2026, 10, 31)
    assert parse_date("2026-10-31") == date(2026, 10, 31)
    assert parse_date("31.02.2026") is None


def test_nip_valid():
    assert nip_valid("5438539502")
    assert nip_valid("7163454949")
    assert not nip_valid("7163454948")
    assert not nip_valid("123")


def test_aes_roundtrip():
    key = SymmetricKey.generate()
    data = "zażółć gęślą jaźń".encode() * 10
    assert key.decrypt(key.encrypt(data)) == data


def test_fts_helpers():
    assert fold("Usługi Programistyczne") == "uslugi programistyczne"
    assert fts_query('uslugi "przyk') == '"uslugi"* AND "przyk"*'
    assert fts_query("   ") is None


def test_store_search(tmp_path, month_end_profile):
    store = Store(tmp_path / "t.db")
    draft = new_draft(month_end_profile, date(2026, 10, 1), [], net=Decimal("46696"))
    xml = build_xml(draft)
    meta = {
        "ksefNumber": "5438539502-20260930-7E8E6F800001-40",
        "invoiceNumber": "1/9/2026",
        "issueDate": "2026-09-30",
        "seller": {"nip": "5438539502", "name": "Pracownia Przykładowa"},
        "buyer": {"identifier": {"type": "Nip", "value": "7163454949"}, "name": "Przykładowa Spółka"},
        "netAmount": 46696.0,
        "vatAmount": 10740.08,
        "grossAmount": 57436.08,
        "currency": "PLN",
    }
    assert store.upsert_metadata("issued", [meta], {meta["ksefNumber"]: xml}) == 1
    assert store.upsert_metadata("issued", [meta], {}) == 0  # duplikat nie dodaje drugiego wiersza

    def numbers(**kwargs):
        return [r.invoice_number for r in store.search(Filters(**kwargs))]

    assert numbers(text="uslugi progr") == ["1/9/2026"]
    assert numbers(text="1/9/2026") == ["1/9/2026"]
    assert numbers(text="przykladowa", direction="issued") == ["1/9/2026"]
    assert numbers(text="przykladowa", direction="received") == []
    assert numbers(counterparty="7163454949") == ["1/9/2026"]
    assert numbers(date_from=date(2026, 10, 1)) == []
    assert numbers(date_from=date(2026, 9, 1), date_to=date(2026, 9, 30)) == ["1/9/2026"]
    assert store.get(meta["ksefNumber"], "issued")["xml"] == xml  # XML nie znika przy aktualizacji metadanych
    assert store.reserved_numbers() == ["1/9/2026"]
    assert store.recent_buyer_invoices() == [xml]

    store.set_checkpoint("Subject1", datetime(2026, 10, 1, tzinfo=UTC))
    assert store.checkpoint("Subject1") == datetime(2026, 10, 1, tzinfo=UTC)
    assert datetime.now(UTC) - store.last_sync() < timedelta(minutes=1)

    other = "7163454949-20261002-1A2B3C000001-11"
    store.upsert_metadata("third", [{**meta, "ksefNumber": other, "invoiceNumber": "Z/1"}], {})
    assert store.get(other, "third")["counterparty_id"] == "5438539502"  # kontrahent = wystawca
    store.set_xml(other, "third", xml)
    assert numbers(text="Z/1", direction="third") == ["Z/1"]
    store.close()
