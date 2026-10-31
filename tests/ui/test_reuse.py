"""„Wystaw podobną” i „Korekta”: tylko dla zwykłych faktur VAT i z ostrzeżeniem, gdy część danych nie przejdzie."""

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from ksefik.fa_parser import draft_gaps
from ksefik.invoice import as_correction, build_xml, new_draft
from ksefik.submissions import metadata_from_xml
from ksefik.ui.common import ConfirmScreen
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import InvoiceScreen


def issued(store, xml, ksef="K"):
    store.upsert_metadata("issued", [metadata_from_xml(xml, ksef)], {ksef: xml})
    return store.get(ksef, "issued")


def with_extra(xml: bytes, extra: str) -> bytes:
    """Dokleja do faktury elementy, których projekt nie przenosi (np. rabat w pozycji i numer WZ)."""
    xml = xml.replace(b"<P_7>", b"<P_6A>2026-10-01</P_6A><P_7>", 1)
    return xml.replace(b"<P_15>", f"<WZ>{extra}</WZ><P_15>".encode(), 1) if extra else xml


def test_invoices_made_in_the_app_lose_nothing_when_reused(profile):
    draft = new_draft(profile, date.today(), [], net=Decimal(100))
    assert draft_gaps(build_xml(draft)) == []
    correction = as_correction(draft, "K", profile, date.today(), [])
    assert draft_gaps(build_xml(correction)) == []
    gaps = draft_gaps(with_extra(build_xml(draft), "WZ/7"))
    assert sorted(gaps) == ["daty dostawy w pozycjach", "numery dokumentów WZ"]


@pytest.mark.parametrize("kind", ["KOR", "ZAL"])
def test_correction_or_advance_invoice_cannot_become_a_vat_draft_silently(seeded, kind):
    app, _, store = seeded
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    if kind == "KOR":
        xml = build_xml(as_correction(draft, "K0", app.profile, date.today(), []))
    else:
        xml = build_xml(draft).replace(b"<RodzajFaktury>VAT</RodzajFaktury>", b"<RodzajFaktury>ZAL</RodzajFaktury>")
    row = issued(store, xml)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = InvoiceScreen(row)
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.check_action("duplicate", ()) is None and screen.check_action("correct", ()) is None
            assert "nie są jeszcze obsługiwane" in str(screen.query_one(".notice").render())
            await pilot.press("d")
            await pilot.press("k")
            await pilot.pause()
            screen.action_duplicate()  # także wywołane wprost (np. z palety poleceń)
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(scenario())


@pytest.mark.parametrize("answer", ["cancel", "confirm"])
def test_missing_data_is_listed_before_the_editor_opens(seeded, answer):
    app, _, store = seeded
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    row = issued(store, with_extra(build_xml(draft), "WZ/7"))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = InvoiceScreen(row)
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            dialog = app.screen
            assert isinstance(dialog, ConfirmScreen) and "numery dokumentów WZ" in dialog.message
            assert app.focused.id == "cancel"  # Enter nie zgadza się na utratę danych
            await pilot.press("enter" if answer == "cancel" else "tab")
            if answer == "confirm":
                await pilot.press("enter")
            await pilot.pause()
            if answer == "cancel":
                assert app.screen is screen
            else:
                assert isinstance(app.screen, EditScreen) and app.screen.draft.kind == "VAT"

    asyncio.run(scenario())
