import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal

from textual.widgets import DataTable, Static

from ksefik.fa_parser import InvoiceView, LineView, PartyView, RateView, parse_invoice
from ksefik.invoice import Line, Payment, build_xml, new_draft, validate_xml
from ksefik.profile import BankAccount, Party, Registers, ThirdParty
from ksefik.ui.widgets import InvoiceDocument


def test_detailed_invoice_roundtrip_preserves_third_parties_contacts_and_registers(profile):
    draft = new_draft(profile, date(2026, 10, 23), [], net=Decimal(100))
    draft = replace(
        draft,
        seller=replace(draft.seller, email="seller@example.test", telefon="123456789"),
        buyer=Party(nazwa="Private buyer", brak_id=True, adres_l1="Street", nr_klienta="C1"),
        third_parties=(ThirdParty(Party(nazwa="Other", brak_id=True), opis_roli="Custom", udzial=Decimal(50)),),
        registers=Registers(krs="1234567890", regon="123456785", bdo="123456789"),
        lines=(Line("Goods", unit_price=Decimal(100), vat_rate="zw", pkwiu="01.11", cn="1234", index="I"),),
        exemption_basis="dyrektywa UE",
        self_billing=True,
        payment=Payment(
            form="inna",
            other_description="Offset",
            due_date=date(2026, 11, 1),
            accounts=(BankAccount("61109010140000071219812874", "Bank", "ABCDEFGH", "Account"),),
        ),
    )
    xml = build_xml(draft)
    assert validate_xml(xml) == []
    view = parse_invoice(xml)
    assert view.third_parties[0].role == "Custom"
    assert "seller@example.test" in view.seller.contact
    assert "Samofakturowanie" in view.annotations
    assert "KRS: 1234567890" in view.footer[-1]


def test_document_renders_gross_prices_correction_refund_and_all_extra_sections(ui):
    app, _, _ = ui
    view = InvoiceView(
        number="Correction",
        kind_code="KOR",
        currency="EUR",
        issue_date=date(2026, 10, 23),
        sale_period=(date(2026, 10, 1), date(2026, 10, 23)),
        issue_place="Warsaw",
        total=Decimal(-123),
        seller=PartyView(name="Seller", nip="5438539502", contact=["email"]),
        buyer=PartyView(name="Buyer", other_id="VAT UE DE123", address=["Street"]),
        third_parties=[PartyView(name="Third", role="Recipient")],
        corrected=["FV/1"],
        correction_reason="Discount",
        orders=["Order: 123"],
        lines=[LineView("1", "Item", "szt.", Decimal(1), None, Decimal(123), None, Decimal(123), "23", before=True)],
        rates=[RateView("23%", Decimal(-100), Decimal(-23))],
        payment_form="Transfer",
        due_dates=["2026-11-01", "14 days"],
        paid_on=date(2026, 10, 23),
        bank_accounts=[("61109010140000071219812874", "Bank")],
        annotations=["Annotation"],
        notes=[("Key", "Value")],
        footer=["Footer"],
    )

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            document = InvoiceDocument(view, ksef_number="K", verify_url="https://example.test")
            await app.screen.mount(document)
            await pilot.pause()
            assert "Do zwrotu" in str(document.query_one(".to-pay", Static).content)
            assert "123,00 EUR" in str(document.query_one(".to-pay", Static).content)
            assert len(document.query(DataTable)) == 2
            assert any("Correction" in str(widget.content) for widget in document.query(Static))

    asyncio.run(scenario())
