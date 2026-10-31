from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from ksefik.codes import VAT_RATES
from ksefik.fa_parser import parse_draft, parse_invoice
from ksefik.invoice import (
    CorrectedInvoice,
    Draft,
    Line,
    Payment,
    as_correction,
    as_new,
    build_xml,
    check_draft,
    default_dates,
    default_period,
    format_number,
    new_draft,
    next_number,
    summary_fields,
    validate_xml,
)
from ksefik.profile import BankAccount, InvoiceSettings, Party, ThirdParty

CREATED = datetime(2026, 11, 4, 12, tzinfo=UTC)
SELLER = Party(
    nazwa="Pracownia Przykładowa", nip="5438539502", adres_l1="ul. Przykładowa 1", adres_l2="00-001 Warszawa"
)
BUYER = Party(nazwa="Przykładowa Sp. z o.o.", nip="7163454949", adres_l1="ul. Złota 1", adres_l2="00-019 Warszawa")
EU_BUYER = Party(nazwa="Beispiel GmbH", vat_ue="DE123456789", adres_l1="Hauptstraße 1", kraj="DE")


def draft(**changes) -> Draft:
    base = Draft(
        number="FV/1/11/2026",
        issue_date=date(2026, 11, 4),
        sale_date=date(2026, 11, 4),
        seller=SELLER,
        buyer=BUYER,
        lines=(Line("Usługa", Decimal(1), "usł.", Decimal("1000.00"), "23"),),
        payment=Payment(form="6", due_date=date(2026, 11, 18), accounts=(BankAccount("61109010140000071219812874"),)),
    )
    return replace(base, **changes)


def blocking(d: Draft, today: date = date(2026, 11, 4)) -> list[str]:
    return [i.message for i in check_draft(d, today) if i.blocking]


def valid_xml(d: Draft) -> bytes:
    xml = build_xml(d, created=CREATED)
    assert validate_xml(xml) == []
    return xml


# ------------------------------------------------------------------- numeracja i daty


@pytest.mark.parametrize(
    ("pattern", "existing", "day", "expected"),
    [
        ("{nr}/{m}/{rrrr}", [], date(2026, 10, 31), "1/10/2026"),
        ("{nr}/{m}/{rrrr}", ["1/9/2026", "1/10/2026", "2/10/2026"], date(2026, 10, 5), "3/10/2026"),
        ("{nr}/{m}/{rrrr}", ["4/09/2026"], date(2026, 9, 1), "5/9/2026"),
        ("FV/{nr}/{mm}/{rrrr}", ["FV/7/09/2026", "fv / 2 / 10 / 2026"], date(2026, 10, 1), "FV/3/10/2026"),
        ("FV/{rrrr}/{nr:4}", ["FV/2026/0041", "FV/2025/0099"], date(2026, 12, 1), "FV/2026/0042"),
        ("{nr}", ["7", "12", "FV/1"], date(2026, 1, 1), "13"),
        ("{nr}/{rr}", ["3/25", "1/26"], date(2026, 6, 1), "2/26"),
    ],
)
def test_next_number(pattern, existing, day, expected):
    assert next_number(pattern, existing, day) == expected


def test_format_number():
    assert format_number("FV/{nr:3}/{mm}/{rr}", 7, date(2026, 3, 9)) == "FV/007/03/26"


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (date(2026, 11, 3), (2026, 10)),
        (date(2026, 11, 10), (2026, 10)),
        (date(2026, 11, 11), (2026, 11)),
        (date(2026, 1, 5), (2025, 12)),
    ],
)
def test_default_period(today, expected):
    assert default_period(today, 10) == expected


def test_default_dates():
    today = date(2026, 11, 4)
    assert default_dates(InvoiceSettings(daty="dzisiaj"), today) == (today, today)
    end = date(2026, 10, 31)
    assert default_dates(InvoiceSettings(daty="koniec_miesiaca", dzien_graniczny=10), today) == (end, end)
    assert default_dates(InvoiceSettings(daty="dzisiaj_za_miesiac", dzien_graniczny=10), today) == (today, end)
    # Bez dnia granicznego "za miesiąc" nie może wskazać daty z przyszłości.
    assert default_dates(InvoiceSettings(daty="dzisiaj_za_miesiac"), today) == (today, today)


# --------------------------------------------------------------- projekt z profilu


def test_new_draft_with_month_end_dates(month_end_profile):
    d = new_draft(month_end_profile, date(2026, 11, 4), ["1/9/2026"], net=Decimal("46696"))
    assert d.number == "1/10/2026"
    assert d.issue_date == d.sale_date == date(2026, 10, 31)
    assert d.payment.due_date == date(2026, 11, 10)
    assert (d.net, d.vat, d.gross) == (Decimal("46696.00"), Decimal("10740.08"), Decimal("57436.08"))
    xml = valid_xml(d)
    assert b"<P_18A>2</P_18A>" in xml  # bez mechanizmu podzielonej płatności


def test_new_draft_for_eu_contractor(profile):
    eu = profile.contractors[1]
    d = new_draft(profile, date(2026, 11, 4), [], buyer=eu.party, net=Decimal(1000))
    assert d.currency == "EUR"
    assert d.lines[0].vat_rate == "np II" and d.reverse_charge
    assert d.payment.accounts[0].waluta == "EUR"  # rachunek walutowy
    assert d.payment.due_date == date(2026, 12, 4)  # termin kontrahenta: 30 dni
    assert not d.needs_exchange_rate  # brak VAT - kurs nie jest potrzebny
    xml = valid_xml(d)
    view = parse_invoice(xml)
    assert view.buyer.other_id == "VAT UE: DE123456789"
    assert "Odwrotne obciążenie" in view.annotations


def test_new_draft_for_jst_contractor_has_recipient(profile):
    d = new_draft(profile, date(2026, 11, 4), [], buyer=profile.contractors[2].party, net=Decimal(100))
    assert d.third_parties[0].rola == 8
    assert not [i for i in check_draft(d, date(2026, 11, 4)) if "JST" in i.message]
    view = parse_invoice(valid_xml(d))
    assert view.third_parties[0].role == "JST – odbiorca"


# ------------------------------------------------------------------------ XML FA(3)


def test_multi_rate_invoice():
    lines = (
        Line("Usługa A", Decimal(2), "godz.", Decimal("150.00"), "23", gtu="GTU_12"),
        Line("Usługa B", Decimal("1.5"), "godz.", Decimal("100.00"), "23"),
        Line("Książka", Decimal(1), "szt.", Decimal("40.00"), "5", pkwiu="58.11.1"),
        Line("Szkolenie", Decimal(1), "usł.", Decimal("100.00"), "zw"),
        Line("Eksport", Decimal(1), "szt.", Decimal("10.00"), "0 EX"),
    )
    d = draft(lines=lines, exemption_basis="art. 43 ust. 1 pkt 29 ustawy o VAT")
    sums = summary_fields(d)
    assert sums == {
        "P_13_1": Decimal("450.00"),
        "P_14_1": Decimal("103.50"),
        "P_13_3": Decimal("40.00"),
        "P_14_3": Decimal("2.00"),
        "P_13_6_3": Decimal("10.00"),
        "P_13_7": Decimal("100.00"),
    }
    assert d.gross == Decimal("705.50")
    xml = valid_xml(d)
    assert b"<P_19>1</P_19><P_19A>art. 43 ust. 1 pkt 29 ustawy o VAT</P_19A>" in xml
    view = parse_invoice(xml)
    assert view.total == Decimal("705.50")
    assert [line.rate for line in view.lines] == ["23", "23", "5", "zw", "0 EX"]


@pytest.mark.parametrize("code", list(VAT_RATES))
def test_every_vat_rate_produces_valid_xml(code):
    d = draft(
        lines=(Line("Pozycja", Decimal(1), "szt.", Decimal("100.00"), code),),
        exemption_basis="art. 113 ust. 1 ustawy o VAT",
        margin_procedure="towary_uzywane",
        buyer=EU_BUYER if code in ("0 WDT", "np II") else BUYER,
    )
    valid_xml(d)
    assert not blocking(d)


def test_foreign_currency_with_exchange_rate():
    d = draft(currency="EUR", exchange_rate=Decimal("4.2512"), exchange_rate_info="tabela NBP 205/A/NBP/2026")
    assert summary_fields(d)["P_14_1W"] == Decimal("977.78")  # 230 EUR * 4.2512
    xml = valid_xml(d)
    assert b"<KursWaluty>4.2512</KursWaluty>" in xml
    assert "1 EUR = 4.2512 PLN, tabela NBP 205/A/NBP/2026" in xml.decode()
    assert any("kurs waluty" in m for m in blocking(draft(currency="EUR")))


def test_buyer_identifiers():
    for buyer, marker in (
        (Party(nazwa="John Doe Inc.", nr_id="12-3456789", kraj="US", adres_l1="1 Main St"), b"<NrID>12-3456789</NrID>"),
        (Party(nazwa="Jan Kowalski", brak_id=True, adres_l1="ul. Prosta 1"), b"<BrakID>1</BrakID>"),
    ):
        assert marker in valid_xml(draft(buyer=buyer))
    assert any("Podaj NIP nabywcy" in m for m in blocking(draft(buyer=Party(nazwa="X", adres_l1="Y"))))


def test_payment_variants():
    paid = draft(payment=Payment(form="1", paid=True, paid_date=date(2026, 11, 4)))
    assert b"<Zaplacono>1</Zaplacono><DataZaplaty>2026-11-04</DataZaplaty>" in valid_xml(paid)
    other = draft(payment=Payment(form="inna", other_description="kompensata"))
    assert b"<PlatnoscInna>1</PlatnoscInna><OpisPlatnosci>kompensata</OpisPlatnosci>" in valid_xml(other)
    assert any("Opisz formę płatności" in m for m in blocking(draft(payment=Payment(form="inna"))))


def test_annotations_footer_and_third_parties():
    d = draft(
        split_payment=True,
        cash_method=True,
        related=True,
        period=(date(2026, 10, 1), date(2026, 10, 31)),
        place="Warszawa",
        notes="Projekt X",
        order_number="ZAM/12",
        contract_number="U/2026/1",
        footer="Dziękujemy",
        third_parties=(ThirdParty(Party(nazwa="Płatnik", nip="4584450427", adres_l1="ul. A 1"), rola=6),),
    )
    view = parse_invoice(valid_xml(d))
    assert {"Mechanizm podzielonej płatności", "Metoda kasowa"} <= set(view.annotations)
    assert view.sale_period == (date(2026, 10, 1), date(2026, 10, 31))
    assert view.orders == ["Umowa: U/2026/1", "Zamówienie: ZAM/12"]
    assert view.notes == [("Uwagi", "Projekt X")]
    assert view.third_parties[0].role == "Dokonujący płatności"


def test_correction():
    original = draft(
        lines=(
            Line("Usługa", Decimal(2), "godz.", Decimal("150"), "23"),
            Line("Licencja", Decimal(1), "szt.", Decimal("40"), "5"),
        )
    )
    kor = replace(
        original,
        kind="KOR",
        number="KOR/1/11/2026",
        lines=(replace(original.lines[0], quantity=Decimal(1)),),  # druga pozycja usunięta
        correction=CorrectedInvoice(
            "FV/1/11/2026",
            date(2026, 11, 4),
            "5438539502-20261104-8E55F0400000-06",
            original.lines,
            reason="Rabat",
            effect="2",
        ),
    )
    assert (kor.net, kor.vat, kor.gross) == (Decimal("-190.00"), Decimal("-36.50"), Decimal("-226.50"))
    xml = valid_xml(kor)
    assert xml.count(b"<StanPrzed>1</StanPrzed>") == 2
    view = parse_invoice(xml)
    assert view.kind == "Faktura korygująca" and view.correction_reason == "Rabat"
    assert [(line.name, line.before, line.quantity) for line in view.lines] == [
        ("Usługa", True, 2),
        ("Usługa", False, 1),
        ("Licencja", True, 1),
        ("Licencja", False, 0),
    ]
    assert not blocking(kor)


def test_parse_draft_roundtrip_and_new_copies(profile):
    d = draft(lines=(Line("A", Decimal("2.5"), "godz.", Decimal("120"), "8", gtu="GTU_12"),), notes="X")
    back = parse_draft(build_xml(d))
    assert back.lines == d.lines
    assert back.buyer == d.buyer and back.seller == d.seller
    assert back.payment == d.payment and back.notes == "X"

    copy = as_new(back, profile, date(2026, 12, 2), ["FV/1/12/2026"])
    assert copy.number == "FV/2/12/2026" and copy.issue_date == date(2026, 12, 2)
    assert copy.seller == profile.seller and copy.lines == d.lines

    kor = as_correction(back, "5438539502-20261104-8E55F0400000-06", profile, date(2026, 12, 2), [])
    assert kor.kind == "KOR" and kor.number == "KOR/1/12/2026"
    assert kor.correction.number == d.number and kor.correction.lines == d.lines
    assert kor.net == 0


# ------------------------------------------------------------------- kontrola projektu


def test_check_blocks_future_issue_date_and_duplicates():
    future = draft(issue_date=date(2026, 11, 30))
    assert any("w przyszłości" in m for m in blocking(future))
    assert not blocking(draft())
    assert [i for i in check_draft(draft(), date(2026, 11, 4), ["FV/1/11/2026"]) if i.blocking]


def test_check_requires_exemption_basis_and_split_payment():
    zw = draft(lines=(Line("Szkolenie", Decimal(1), "usł.", Decimal(100), "zw"),))
    assert any("podstawy zwolnienia" in m for m in blocking(zw))
    big = draft(lines=(Line("Telefony", Decimal(10), "szt.", Decimal(2000), "23", annex15=True),))
    assert any("podzielonej płatności" in m for m in blocking(big))
    assert not blocking(replace(big, split_payment=True))
