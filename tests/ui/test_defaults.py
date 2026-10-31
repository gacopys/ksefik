"""Wartości domyślne: zmiana kontrahenta ustawia jego dane, ale nie nadpisuje ręcznych zmian i nie zostawia starych."""

import asyncio
from dataclasses import replace
from datetime import date
from decimal import Decimal

from textual.widgets import Checkbox, Input, Select, Static

from ksefik.fa_parser import parse_draft, parse_invoice
from ksefik.invoice import as_new, build_xml, check_draft, new_draft
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import QuickInvoiceScreen
from ksefik.ui.widgets import InvoiceDocument


def contractor(app, name):
    return next(c for c in app.profile.contractors if c.party.nazwa.startswith(name))


def test_quick_invoice_follows_the_buyer_but_keeps_manual_choices(seeded):
    app, _, _ = seeded
    eu, local = contractor(app, "Beispiel"), contractor(app, "Przykładowa")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = QuickInvoiceScreen()
            await app.push_screen(screen)
            await pilot.pause()
            buyer, item = screen.query_one("#buyer", Select), screen.query_one("#item", Select)
            amount = screen.query_one("#amount", Input)
            buyer.value = eu.party.key
            await pilot.pause()
            assert item.value == eu.pozycja and screen._draft().currency == "EUR"
            assert "waluta EUR" in str(screen.query_one("#changed", Static).render())
            # Ręczny wybór pozycji z ceną z katalogu: kwota przychodzi z katalogu, dopóki jej nie zmienisz.
            item.focus()
            item.value = "Konsultacje"
            await pilot.pause()
            assert amount.value == "250,00"
            buyer.value = local.party.key
            await pilot.pause()
            assert item.value == "Konsultacje" and screen._draft().currency == "PLN"
            amount.focus()
            amount.value = "999"  # wpisane ręcznie
            await pilot.pause()
            buyer.value = eu.party.key
            await pilot.pause()
            assert amount.value == "999" and item.value == "Konsultacje"
            provenance = str(screen.query_one("#provenance", Static).render())
            assert "Z profilu" in provenance and "rachunek" in provenance and "EUR" in provenance

    asyncio.run(scenario())


def test_editor_buyer_switch_clears_what_belonged_to_the_previous_buyer(seeded):
    app, _, _ = seeded
    gmina = contractor(app, "Gmina")

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            draft = new_draft(app.profile, date.today(), [], buyer=gmina.party, net=Decimal(100))
            assert draft.third_parties  # JST z odbiorcą (podmiot 3)
            screen = EditScreen(draft)
            await app.push_screen(screen)
            await pilot.pause()
            eu = contractor(app, "Beispiel")
            screen.query_one("#buyer_pick", Select).value = eu.party.key
            await pilot.pause()
            assert screen.draft.third_parties == () and not str(screen.query_one("#third-parties").render())
            assert screen.query_one("#currency", Input).value == "EUR"
            account = screen.query_one("#account", Select).value
            assert next(a for a in app.profile.accounts if a.number == account).waluta == "EUR"
            # Waluta zmieniona ręcznie zostaje przy kolejnej zmianie nabywcy.
            currency = screen.query_one("#currency", Input)
            currency.focus()
            currency.value = "USD"
            await pilot.pause()
            screen.query_one("#buyer_pick", Select).value = contractor(app, "Przykładowa").party.key
            await pilot.pause()
            assert currency.value == "USD" and not screen.query_one("#related", Checkbox).value

    asyncio.run(scenario())


def test_copy_of_a_foreign_invoice_uses_the_matching_account_and_asks_to_check_the_rate(profile):
    eu = next(c for c in profile.contractors if c.waluta == "EUR")
    source = new_draft(profile, date(2026, 9, 1), [], buyer=eu.party, net=Decimal(1000))
    source = source.with_changes(
        lines=(replace(source.lines[0], vat_rate="23"),), exchange_rate=Decimal("4.30"), exchange_rate_info="NBP"
    )
    copy = as_new(parse_draft(build_xml(source)), profile, date(2026, 10, 24), [])
    assert copy.payment.accounts[0].waluta == "EUR"
    warnings = [i.message for i in check_draft(copy, date(2026, 10, 24)) if not i.blocking]
    assert any(w.startswith("Sprawdź kurs: 4,3 PLN za 1 EUR") for w in warnings)
    assert not any(i.blocking for i in check_draft(copy, date(2026, 10, 24)))


def test_paid_invoice_is_not_shown_as_still_due(profile):
    from textual.app import App

    from ui.harness import ROOT

    draft = new_draft(profile, date.today(), [], net=Decimal(100))
    paid = draft.with_changes(payment=replace(draft.payment, paid=True, paid_date=date(2026, 10, 20)))

    class Preview(App):
        CSS_PATH = ROOT / "src/ksefik/ui/app.tcss"

        def __init__(self, invoice):
            super().__init__()
            self.invoice = invoice

        def compose(self):
            yield InvoiceDocument(parse_invoice(build_xml(self.invoice)))

    async def scenario(invoice, expected):
        app = Preview(invoice)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            total = str(app.query_one(".to-pay").render())
            assert expected in total
            assert ("Zapłacono 20.10.2026" in total) is (invoice is paid)

    asyncio.run(scenario(draft, "Do zapłaty"))
    asyncio.run(scenario(paid, "Wartość faktury"))
