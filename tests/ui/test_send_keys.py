"""Zapisywanie i sprawdzanie faktury nigdy jej nie wysyła; wysyłka wymaga jawnego naciśnięcia przycisku."""

import asyncio
from datetime import UTC, datetime

from textual.widgets import Button

from ksefik.client import SendResult
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import SendScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen


def test_ctrl_s_twice_in_editor_never_reaches_sending(seeded):
    app, fake, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press("ctrl+o")  # „Więcej opcji” - pełna edycja
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, EditScreen)
            await pilot.press("ctrl+s", "ctrl+s")  # zapis szkicu - zostajemy w edycji
            await pilot.pause()
            assert app.screen is editor and editor.draft_id is not None
            await pilot.press("f5")  # sprawdzenie faktury
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)
            await pilot.press("ctrl+s", "ctrl+s")
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)
            assert not any(isinstance(s, SendScreen) for s in app.screen_stack)
            fake.send_invoice.assert_not_awaited()

    asyncio.run(scenario())


def test_f9_only_moves_to_the_send_button_and_enter_sends(seeded):
    app, fake, _ = seeded
    fake.send_invoice.return_value = SendResult("K", "S", "I", datetime.now(UTC), None)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            await pilot.press("n")
            await pilot.pause()
            await pilot.press(*"100", "enter")  # kwota → podsumowanie
            await pilot.pause()
            summary = app.screen
            assert isinstance(summary, SummaryScreen)
            await pilot.press("f9")
            await pilot.pause()
            assert app.focused is summary.query_one("#send", Button)
            fake.send_invoice.assert_not_awaited()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, SendScreen)
            await app.workers.wait_for_complete()
            fake.send_invoice.assert_awaited_once()

    asyncio.run(scenario())
