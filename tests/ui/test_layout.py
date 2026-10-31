"""Układ w najmniejszym wspieranym oknie (80×24): przycisk zatwierdzenia i aktywne pole zawsze widać."""

import asyncio
from datetime import date
from decimal import Decimal

import pytest
from textual.widgets import Button, Input

from ksefik.invoice import new_draft
from ksefik.ui.common import ChoiceScreen, ConfirmScreen, TooSmallScreen
from ksefik.ui.editor import EditScreen, LineScreen
from ksefik.ui.invoice_screens import QuickInvoiceScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.permissions import PermissionsScreen
from ksefik.ui.setup_screen import PasswordScreen, TokenScreen, profile_wizard
from ui.harness import SIZES, assert_visible


def draft(app):
    return new_draft(app.profile, date.today(), [], net=Decimal(100))


async def person_form(app, pilot):
    screen = PermissionsScreen()
    await app.push_screen(screen)
    await pilot.pause()
    await app.workers.wait_for_complete()
    form, _ = screen._person_form()
    await app.push_screen(form)
    return form


SCREENS = {
    "kreator-1": (lambda app, pilot: app.push_screen(profile_wizard(app.profile_path)), "#submit"),
    "kreator-2": (lambda app, pilot: app.push_screen(profile_wizard(app.profile_path, step=2)), "#submit"),
    "pozycja": (lambda app, pilot: app.push_screen(LineScreen(draft(app).lines[0])), "#save"),
    "token": (lambda app, pilot: app.push_screen(TokenScreen(first_run=True)), "#connect"),
    "haslo": (lambda app, pilot: app.push_screen(PasswordScreen()), "#unlock"),
    "szybka": (lambda app, pilot: app.push_screen(QuickInvoiceScreen()), "#next"),
    "podsumowanie": (lambda app, pilot: app.push_screen(SummaryScreen(draft(app))), "#send"),
    "edycja": (lambda app, pilot: app.push_screen(EditScreen(draft(app))), "#save"),
    "uprawnienia": (person_form, "#submit"),
    "potwierdzenie": (
        lambda app, pilot: app.push_screen(
            ConfirmScreen("Długi komunikat", "\n".join(f"• wiersz {n}" for n in range(30)), confirm="Dalej")
        ),
        "#confirm",
    ),
}


@pytest.mark.parametrize("size", SIZES, ids=lambda s: f"{s[0]}x{s[1]}")
@pytest.mark.parametrize("name", list(SCREENS))
def test_primary_action_is_visible(seeded, name, size):
    app, _, _ = seeded
    open_screen, primary = SCREENS[name]

    async def scenario():
        async with app.run_test(size=size) as pilot:
            await open_screen(app, pilot)
            await pilot.pause()
            await pilot.pause()
            assert_visible(app.screen.query_one(primary, Button), f"{name}: {primary}")
            if app.focused is not None:
                assert_visible(app.focused, f"{name}: aktywne pole")

    asyncio.run(scenario())


def test_walking_a_long_form_by_keyboard_keeps_every_field_and_the_buttons_visible(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            form = await person_form(app, pilot)
            await pilot.pause()
            for _ in range(12):
                await pilot.press("tab")
                await pilot.pause()
                assert_visible(app.focused, f"pole {app.focused.id}")
                assert_visible(form.query_one("#submit", Button), "Nadaj")

    asyncio.run(scenario())


def test_long_choice_list_scrolls_inside_the_window(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            choices = [(f"Opcja {n}", "opis opcji", n) for n in range(20)]
            await app.push_screen(ChoiceScreen("Wybierz", choices))
            await pilot.pause()
            await pilot.pause()
            await pilot.press("end")
            await pilot.pause()
            options = app.screen.query_one("#choices")
            assert app.screen.query_one(".choice-dialog").region.bottom <= 24
            assert options.highlighted == 19 and options.max_scroll_y > 0

    asyncio.run(scenario())


def test_amounts_keep_their_currency_at_80_columns(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            table = main.query_one("#invoices")
            gross = [str(table.get_row_at(n)[-1]) for n in range(table.row_count)]
            assert all(g.endswith(("PLN", "EUR")) for g in gross) and any(g.endswith("EUR") for g in gross)
            assert "cur" not in [c.value for c in table.columns]

    asyncio.run(scenario())


def test_too_small_terminal_shows_a_message_and_keeps_the_work(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            editor = EditScreen(draft(app))
            await app.push_screen(editor)
            await pilot.pause()
            editor.query_one("#notes", Input).value = "Nie zgubić"
            await pilot.resize_terminal(70, 20)
            await pilot.pause()
            assert isinstance(app.screen, TooSmallScreen)
            assert "70×20" in str(app.screen.query_one("#too-small").render())
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert app.screen is editor and editor.query_one("#notes", Input).value == "Nie zgubić"

    asyncio.run(scenario())
