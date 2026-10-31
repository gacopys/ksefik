"""Szkice: wpisane dane nie giną - zamknięcie edycji, wyjście z aplikacji i ponowne uruchomienie je zachowują."""

import asyncio
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

from textual.widgets import Input, Tabs

from ksefik.client import SendResult
from ksefik.invoice import new_draft
from ksefik.store import Store
from ksefik.ui.common import ChoiceScreen, ConfirmScreen
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import SendScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen
from ui.harness import HarnessApp, fake_client, make_profile


async def new_invoice_editor(app, pilot):
    await app.push_screen(MainScreen())
    await pilot.pause()
    await pilot.press("n")
    await pilot.pause()
    await pilot.press("ctrl+o")  # więcej opcji - pełna edycja
    await pilot.pause()
    editor = app.screen
    assert isinstance(editor, EditScreen)
    return editor


def test_quit_mid_edit_and_restart_restores_the_exact_typed_values(isolated):
    profile = make_profile(isolated)
    db = isolated / "ksef.db"
    store = Store(db)
    app = HarnessApp(profile, profile_path=profile.path, store=store)
    app.client = fake_client()

    async def first_run():
        async with app.run_test(size=(120, 40)) as pilot:
            editor = await new_invoice_editor(app, pilot)
            editor.query_one("#notes", Input).value = "Projekt strony - etap 2"
            editor.query_one("#issue_date", Input).value = "31.13.20"  # w trakcie pisania - niepoprawna data
            await pilot.pause()
            await app.action_quit()  # od razu, przed automatycznym zapisem
            await pilot.pause()

    asyncio.run(first_run())
    assert app.return_code == 0
    store.close()

    store = Store(db)
    app = HarnessApp(profile, profile_path=profile.path, store=store)
    app.client = fake_client()

    async def second_run():
        async with app.run_test(size=(120, 40)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            tabs = main.query_one("#direction-tabs", Tabs)
            assert "drafts" in [tab.id for tab in tabs.query("Tab")]
            await pilot.press("4")  # Wystawione, Otrzymane, Wszystkie, Szkice
            await pilot.pause()
            assert main.direction == "drafts" and main.query_one("#invoices").row_count == 1
            main.query_one("#invoices").focus()
            await pilot.press("enter")
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, EditScreen)
            assert editor.query_one("#notes", Input).value == "Projekt strony - etap 2"
            assert editor.query_one("#issue_date", Input).value == "31.13.20"

    asyncio.run(second_run())
    store.close()


def test_closing_keeps_the_draft_and_discarding_needs_confirmation(seeded):
    app, _, store = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            editor = await new_invoice_editor(app, pilot)
            editor.query_one("#notes", Input).value = "Do dokończenia"
            await pilot.pause(editor.AUTOSAVE_DELAY + 0.3)  # zapis automatyczny
            assert editor.draft_id is not None and store.draft(editor.draft_id) is not None
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen) and len(store.drafts()) == 1
            app.open_draft(store.drafts()[0]["id"])
            await pilot.pause()
            editor = app.screen
            editor.action_discard()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and app.focused.id == "cancel"
            await pilot.press("enter")  # Anuluj
            await pilot.pause()
            assert app.screen is editor and len(store.drafts()) == 1
            editor.action_discard()
            await pilot.pause()
            app.screen._confirm()
            await pilot.pause()
            assert store.drafts() == [] and isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_summary_escape_keeps_the_draft_and_acceptance_archives_it(seeded):
    app, fake, store = seeded
    fake.send_invoice.return_value = SendResult("K", "S", "I", datetime.now(UTC), None)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            await pilot.press("n", *"250", "enter")
            await pilot.pause()
            summary = app.screen
            assert isinstance(summary, SummaryScreen)
            await pilot.press("escape")
            await pilot.pause()
            (draft,) = store.drafts()
            app.open_draft(draft["id"])  # bez pól edytora - wraca do podsumowania
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen) and app.screen.draft_id == draft["id"]
            await pilot.press("f9", "enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, SendScreen) and app.screen.outcome == "accepted"
            assert store.drafts() == [] and store.sent_record("K")["draft_id"] == draft["id"]

    asyncio.run(scenario())


def test_two_open_drafts_do_not_get_the_same_number(seeded):
    app, _, store = seeded
    first = new_draft(app.profile, date.today(), app.numbers_in_use(), net=Decimal(100))
    app.save_draft(first)
    second = new_draft(app.profile, date.today(), app.numbers_in_use(), net=Decimal(100))
    assert first.number != second.number


def test_failed_save_keeps_typed_data_and_offers_choices(seeded, monkeypatch):
    app, _, store = seeded

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database or disk is full")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            editor = await new_invoice_editor(app, pilot)
            monkeypatch.setattr(store, "save_draft", broken)
            editor.query_one("#notes", Input).value = "Ważne dane"
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen) and "disk is full" in app.screen.intro
            app.screen.action_cancel()  # zostajemy w edycji
            await pilot.pause()
            assert app.screen is editor and editor.query_one("#notes", Input).value == "Ważne dane"

    asyncio.run(scenario())
