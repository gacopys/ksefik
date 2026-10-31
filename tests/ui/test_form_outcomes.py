"""Operacje w KSeF z formularza: błąd nie zamyka okna ani nie gubi danych, a wynik zostaje pod listą (UX-19)."""

import asyncio

from textual.widgets import Button, Input

from ksefik.client import KsefError, OperationPending
from ksefik.ui import account, permissions
from ksefik.ui.common import ConfirmScreen, FormScreen


def fill_person(form: FormScreen) -> None:
    form.query_one("#f-value", Input).value = "44051401458"
    form.query_one("#f-first", Input).value = "Jan"
    form.query_one("#f-last", Input).value = "Kowalski"
    form.query_one("#f-description", Input).value = "Księgowa"


def text(widget) -> str:
    return str(widget.render())


async def submit_reviewed(app, pilot, form: FormScreen) -> None:
    """Zatwierdza formularz nadania i jego przegląd (kto, co, w czyim imieniu)."""
    form.action_submit()
    await pilot.pause()
    review = app.screen
    assert isinstance(review, ConfirmScreen) and "Kowalski" in text(review.query_one(".dialog-text"))
    review.query_one("#confirm").press()
    await pilot.pause()


async def open_person_form(app, pilot) -> tuple[permissions.PermissionsScreen, FormScreen]:
    screen = permissions.PermissionsScreen()
    await app.push_screen(screen)
    await pilot.pause()
    screen._grant_type("person")
    await pilot.pause()
    form = app.screen
    assert isinstance(form, FormScreen)
    fill_person(form)
    return screen, form


def test_rejected_grant_keeps_the_form_and_its_values(seeded):
    app, fake, _ = seeded
    fake.grant_person_permissions.side_effect = [
        KsefError("Brak uprawnień do nadawania uprawnień.", http_status=403),
        "OP-2",
    ]
    fake.wait_for_permission_operation.return_value = {"code": 200}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen, form = await open_person_form(app, pilot)
            await submit_reviewed(app, pilot, form)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is form and not form.busy
            assert "Brak uprawnień do nadawania" in text(form.query_one("#form-error"))
            assert "Wpisane dane są zachowane" in text(form.query_one("#form-error"))
            assert form.query_one("#f-first", Input).value == "Jan"
            assert form.query_one("#submit", Button).has_focus and not form.query_one("#submit", Button).disabled
            assert "Brak uprawnień do nadawania" in text(screen.query_one("#op-status"))
            assert not app._notifications  # błąd jest w formularzu - bez dodatkowego powiadomienia
            await submit_reviewed(app, pilot, form)  # druga próba (np. po nadaniu uprawnień w KSeF)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert fake.grant_person_permissions.await_count == 2
            assert "Uprawnienia nadane" in text(screen.query_one("#op-status"))

    asyncio.run(scenario())


def test_form_is_locked_while_ksef_answers(seeded):
    app, fake, _ = seeded
    release = asyncio.Event()

    async def slow_grant(*args):
        await release.wait()
        return "OP-1"

    fake.grant_person_permissions.side_effect = slow_grant
    fake.wait_for_permission_operation.return_value = {"code": 200}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen, form = await open_person_form(app, pilot)
            await submit_reviewed(app, pilot, form)
            assert form.busy and "Wysyłam do KSeF" in text(form.query_one("#form-error"))
            assert form.query_one("#submit", Button).disabled and form.query_one("#cancel", Button).disabled
            assert str(form.query_one("#submit", Button).label) == "Czekam na KSeF…"
            await pilot.press("escape")  # nie da się zamknąć w trakcie - wniosek mógł już dotrzeć do KSeF
            form.action_submit()
            await pilot.pause()
            assert app.screen is form
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen and fake.grant_person_permissions.await_count == 1

    asyncio.run(scenario())


def test_pending_grant_closes_the_form_and_can_be_checked_later(seeded):
    app, fake, _ = seeded
    fake.grant_person_permissions.return_value = "OP-9"
    fake.wait_for_permission_operation.side_effect = OperationPending("OP-9")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen, form = await open_person_form(app, pilot)
            await submit_reviewed(app, pilot, form)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen
            assert "OP-9" in text(screen.query_one("#op-status"))
            assert screen.check_action("check_operations", ()) is True

    asyncio.run(scenario())


def test_token_form_stays_open_when_login_is_cancelled(seeded):
    app, fake, _ = seeded

    async def no_client():
        return None

    app.get_client = no_client  # użytkownik zamknął okno logowania

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TokensScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_new()
            await pilot.pause()
            form = app.screen
            form.query_one("#f-description", Input).value = "Program księgowy"
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is form and "Nie połączono z KSeF" in text(form.query_one("#form-error"))
            assert form.query_one("#f-description", Input).value == "Program księgowy"
            fake.generate_token.assert_not_awaited()

    asyncio.run(scenario())


def test_failed_test_data_operation_keeps_the_form(seeded, monkeypatch):
    app, fake, _ = seeded
    monkeypatch.setattr(app, "_profile", app.profile.with_env("test"))  # dane testowe tylko na testowym
    fake.testdata.side_effect = [KsefError("Limit poza zakresem."), None]

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TestDataScreen()
            await app.push_screen(screen)
            await pilot.pause()
            options = screen.query_one("#operations")
            options.highlighted = next(n for n, op in enumerate(screen.operations) if op[0] == "Ustaw limity sesji")
            options.action_select()
            await pilot.pause()
            form = app.screen
            form.query_one("#f-count", Input).value = "777"
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is form and "Limit poza zakresem" in text(form.query_one("#form-error"))
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert app.screen is screen and fake.testdata.await_count == 2

    asyncio.run(scenario())
