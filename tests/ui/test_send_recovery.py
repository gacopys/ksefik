"""Wysyłka przerwana w trakcie (zamknięcie aplikacji, zerwane połączenie) jest rozstrzygana po ponownym uruchomieniu."""

import asyncio
from datetime import date
from decimal import Decimal

from textual.widgets import Button

from ksefik.client import KsefError, OutcomeUnknown
from ksefik.invoice import build_xml, new_draft
from ksefik.store import Store
from ksefik.ui.invoice_screens import SendScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen
from ui.harness import HarnessApp, fake_client, make_profile


def test_connection_lost_after_session_opened_is_unknown_not_rejected(seeded):
    app, fake, store = seeded
    draft = new_draft(app.profile, date.today(), store.reserved_numbers(), net=Decimal(100))

    async def send(xml, progress, on_refs):
        on_refs(session_ref="S")
        raise KsefError("KSeF nie odpowiedział w wyznaczonym czasie.")

    fake.send_invoice.side_effect = send

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(MainScreen())
            summary = SummaryScreen(draft)
            await app.push_screen(summary)
            await pilot.pause()
            summary.action_send()
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, SendScreen) and screen.outcome == "unknown"
            assert store.sent_entry(screen.sent_id)["status"] == "unknown"
            await pilot.press("escape")  # zamknięcie wyniku nie wraca do podsumowania z aktywnym „Wyślij”
            await pilot.pause()
            assert isinstance(app.screen, MainScreen) and summary not in app.screen_stack

    asyncio.run(scenario())


def test_restart_reconciles_interrupted_send_and_lists_the_invoice(isolated):
    profile = make_profile(isolated)
    db = isolated / "ksef.db"
    draft = new_draft(profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)

    # Pierwsze uruchomienie: połączenie zerwane w trakcie przesyłania, użytkownik zamyka aplikację.
    store = Store(db)
    first = HarnessApp(profile, profile_path=profile.path, store=store)
    fake = fake_client()
    first.client = fake

    async def interrupted(xml, progress, on_refs):
        on_refs(session_ref="S")
        raise OutcomeUnknown("S")

    fake.send_invoice.side_effect = interrupted

    async def send_and_quit():
        async with first.run_test(size=(80, 24)) as pilot:
            first.get_client = lambda: _value(fake)
            await first.push_screen(SendScreen(draft, xml))
            await pilot.pause()
            await first.workers.wait_for_complete()

    asyncio.run(send_and_quit())
    store.close()

    # Drugie uruchomienie: ten sam plik bazy, KSeF zdążył przyjąć fakturę.
    store = Store(db)
    assert [r["status"] for r in store.unresolved_sent()] == ["unknown"]
    second = HarnessApp(profile, profile_path=profile.path, store=store)
    fake = fake_client()
    second.client = fake
    fake.session_invoices.return_value = [
        {"referenceNumber": "I", "invoiceHash": store.unresolved_sent()[0]["xml_hash"]}
    ]
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}

    async def restart():
        async with second.run_test(size=(80, 24)) as pilot:
            second._start()
            await pilot.pause()
            await second.workers.wait_for_complete()
            await pilot.pause()
            main = second.screen
            assert isinstance(main, MainScreen)
            assert store.find("K") is not None and store.unresolved_sent() == []
            assert main.query_one("#invoices").row_count == 1  # od razu na liście, bez synchronizacji

    asyncio.run(restart())
    store.close()


def test_not_submitted_returns_to_a_sendable_summary(seeded):
    from ksefik.client import NotSubmitted

    app, fake, store = seeded
    draft = new_draft(app.profile, date.today(), store.reserved_numbers(), net=Decimal(100))
    fake.send_invoice.side_effect = NotSubmitted("Brak połączenia z KSeF. Sprawdź internet.")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(MainScreen())
            summary = SummaryScreen(draft)
            await app.push_screen(summary)
            await pilot.pause()
            summary.action_send()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen.outcome == "not_submitted"
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is summary and not summary.query_one("#send", Button).disabled
            assert draft.number not in store.reserved_numbers()

    asyncio.run(scenario())


async def _value(value):
    return value
