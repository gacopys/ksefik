from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from lxml import etree

from ksefik.fa_parser import parse_draft, parse_invoice, searchable_text
from ksefik.invoice import Line, Payment, as_correction, build_xml, new_draft, validate_xml


def document(body):
    return f"<Faktura><Fa>{body}</Fa></Faktura>".encode()


def test_non_invoice_and_malformed_documents():
    assert parse_invoice(b"<other/>").number == ""
    with pytest.raises(ValueError, match="brak elementu Fa"):
        parse_draft(b"<other/>")
    with pytest.raises(etree.XMLSyntaxError):
        parse_invoice(b"broken")
    assert searchable_text(b"broken") == ""
    assert searchable_text(b"<r> first <a> second </a></r>") == "first second"


def test_invalid_optional_dates_and_numbers_do_not_break_view():
    view = parse_invoice(document("<P_1>bad-date</P_1><P_15>invalid</P_15><FaWiersz><P_8B>x</P_8B></FaWiersz>"))
    assert view.issue_date is None and view.total is None
    assert view.lines[0].quantity is None
    assert view.currency == "PLN"


def test_zero_quantity_correction_roundtrip_preserves_removed_line(profile):
    original = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    corrected = as_correction(original, "", profile, date(2026, 10, 23), [])
    corrected = replace(corrected, lines=(replace(original.lines[0], quantity=Decimal(0)),))
    xml = build_xml(corrected)
    assert validate_xml(xml) == []
    parsed = parse_draft(xml)
    assert parsed.lines[0].quantity == 0
    assert parsed.lines[0].net == 0


def test_paid_invoice_roundtrip_preserves_payment(profile):
    draft = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    draft = replace(draft, payment=Payment(form="1", paid=True, paid_date=date(2026, 10, 23)))
    xml = build_xml(draft)
    assert validate_xml(xml) == []
    assert parse_draft(xml).payment == draft.payment


def test_gross_unit_price_is_converted_to_net_before_vat_is_applied():
    xml = document(
        "<FaWiersz><P_7>Service</P_7><P_8B>2</P_8B><P_9B>123.00</P_9B><P_11A>246.00</P_11A><P_12>23</P_12></FaWiersz>"
    )
    parsed = parse_draft(xml)
    assert parsed.lines[0].unit_price == Decimal("100")
    assert parsed.gross == Decimal("246")


def test_fractional_quantity_net_total_does_not_lose_price_precision(profile):
    draft = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    draft = replace(draft, lines=(Line("Service", Decimal(3), unit_price=Decimal("0.333333")),))
    xml = build_xml(draft)
    root = etree.fromstring(xml)
    price = root.find(".//{*}P_9A")
    price.getparent().remove(price)
    parsed = parse_draft(etree.tostring(root))
    assert parsed.net == draft.net == Decimal("1.00")


@pytest.mark.parametrize(
    "row,price,net",
    [
        ("<P_8B>1</P_8B><P_9B>100.00</P_9B><P_11A>100.00</P_11A><P_12>23</P_12>", "81.30081301", "81.30"),
        ("<P_8B>3</P_8B><P_11>1.00</P_11><P_12>23</P_12>", "0.33333333", "1.00"),
    ],
)
def test_derived_unit_prices_keep_the_precision_of_fa3(row, price, net):
    from ksefik.ui.editor import _plain_amount

    line = parse_draft(document(f"<FaWiersz><P_7>Service</P_7>{row}</FaWiersz>")).lines[0]
    assert line.unit_price == Decimal(price) and line.net == Decimal(net)
    assert _plain_amount(line.unit_price) == price.replace(".", ",")


def test_xml_preserves_fractional_unit_prices(profile):
    draft = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    draft = replace(draft, lines=(Line("Service", Decimal(3), unit_price=Decimal("0.33333333")),))
    xml = build_xml(draft)
    assert validate_xml(xml) == []
    assert parse_draft(xml).lines == draft.lines
    assert parse_draft(xml).net == draft.net == Decimal("1.00")


def test_parser_reads_foreign_identity_contacts_annotations_terms_and_footer():
    xml = b"""<Faktura>
      <Podmiot1><DaneIdentyfikacyjne><Nazwa>Seller</Nazwa><NrID>123</NrID><KodKraju>US</KodKraju>
      </DaneIdentyfikacyjne><Adres><AdresL1>Street</AdresL1><KodKraju>US</KodKraju></Adres>
      <DaneKontaktowe><Email>a@example.test</Email><Telefon>12345</Telefon></DaneKontaktowe></Podmiot1>
      <Podmiot2><DaneIdentyfikacyjne><Nazwa>Buyer</Nazwa><BrakID>1</BrakID></DaneIdentyfikacyjne></Podmiot2>
      <Podmiot3><DaneIdentyfikacyjne><Nazwa>Internal</Nazwa><IDWew>X</IDWew></DaneIdentyfikacyjne>
      <RolaInna>1</RolaInna><OpisRoli>Custom role</OpisRoli><Udzial>50</Udzial></Podmiot3>
      <Fa><P_1>2026-10-23</P_1><KodWaluty>USD</KodWaluty><P_13_1>100</P_13_1><P_14_1>23</P_14_1>
        <Adnotacje><P_17>1</P_17><P_23>1</P_23><Zwolnienie><P_19>1</P_19></Zwolnienie>
        <PMarzy><P_PMarzy>1</P_PMarzy></PMarzy></Adnotacje>
        <FaWiersz><P_7>Item</P_7><P_11>10</P_11><P_12>23</P_12></FaWiersz>
        <Platnosc><PlatnoscInna>1</PlatnoscInna><OpisPlatnosci>Offset</OpisPlatnosci>
          <TerminPlatnosci><TerminOpis><Ilosc>14</Ilosc><Jednostka>dni</Jednostka>
          <ZdarzeniePoczatkowe>invoice</ZdarzeniePoczatkowe></TerminOpis></TerminPlatnosci>
          <RachunekBankowy><NrRB>12345678901234567890123456</NrRB><NazwaBanku>Bank</NazwaBanku></RachunekBankowy>
        </Platnosc>
      </Fa><Stopka><Rejestry><KRS>1234567890</KRS><REGON>12345</REGON><BDO>54321</BDO></Rejestry></Stopka>
    </Faktura>"""
    view = parse_invoice(xml)
    assert view.seller.other_id == "ID: US 123"
    assert view.seller.address == ["Street", "US"]
    assert view.seller.contact == ["a@example.test", "12345"]
    assert view.buyer.other_id == "bez identyfikatora podatkowego"
    assert view.third_parties[0].other_id == "Id. wewnętrzny: X"
    assert view.third_parties[0].role == "Custom role"
    assert view.net_total == 100 and view.vat_total == 23 and view.rates[0].gross == 123
    assert "Samofakturowanie" in view.annotations and "Procedura marży" in view.annotations
    assert view.due_dates == ["14 dni od invoice"] and view.payment_form == "Offset"
    assert view.footer == ["KRS: 1234567890, REGON: 12345, BDO: 54321"]
    draft = parse_draft(xml)
    assert draft.seller.nr_id == "123" and draft.seller.kraj == "US"
    assert draft.buyer.brak_id
    assert draft.third_parties[0].opis_roli == "Custom role"
    assert draft.third_parties[0].udzial == 50
    assert draft.lines[0].quantity == 1 and draft.lines[0].unit_price == 10


@pytest.mark.parametrize("rate,price,expected", [("zw", "123", "123"), ("8", "108", "100"), ("5", "105", "100")])
def test_gross_price_conversion_respects_each_rate(rate, price, expected):
    draft = parse_draft(document(f"<FaWiersz><P_9B>{price}</P_9B><P_12>{rate}</P_12></FaWiersz>"))
    assert draft.lines[0].unit_price == Decimal(expected)


def test_xml_validation_reports_malformed_and_wrong_namespace():
    from ksefik.invoice import is_fa3

    assert validate_xml(b"not xml")
    assert validate_xml(b"<other/>")
    assert not is_fa3(b"<other/>")
    assert not is_fa3(b"not xml")
