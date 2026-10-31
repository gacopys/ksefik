"""Wylogowanie i tokeny: każda opcja robi dokładnie to, co opisuje (AT-29), a nowy token nie ginie
i nie jest używany przed aktywacją w KSeF (AT-25)."""

import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

from textual.widgets import Button, OptionList

from ksefik.client import KsefError, TokenCredentials
from ksefik.profile import AuthSettings
from ksefik.ui import account
from ksefik.ui import app as app_module
from ksefik.ui.common import ChoiceScreen, ConfirmScreen


def choice_labels(screen: ChoiceScreen) -> list[str]:
    return [label for label, _, _ in screen.choices]


def messages(app) -> str:
    return "\n".join(n.message for n in app._notifications)


async def choose(app, pilot, value: str) -> None:
    app.logout()
    await pilot.pause()
    menu = app.screen
    assert isinstance(menu, ChoiceScreen)
    options = menu.query_one(OptionList)
    options.highlighted = [v for _, _, v in menu.choices].index(value)
    options.action_select()
    await pilot.pause()


def real_login(app, monkeypatch, saved: str | None = "SAVED"):
    """Prawdziwe get_client (bez skrótu z harness) i zapisany token; zwraca listę danych logowania nowych klientów."""
    del app.get_client
    monkeypatch.setattr(app_module, "load_token", lambda env, nip: saved)
    made = []

    def make_client(credentials):
        made.append(credentials)
        client = AsyncMock()
        client.on_credentials_rejected = None
        return client

    app.make_client = make_client
    return made


def test_work_locally_keeps_the_token_and_reconnects_with_it(seeded, monkeypatch):
    app, fake, _ = seeded
    made = real_login(app, monkeypatch)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "local")
            await app.workers.wait_for_complete()
            assert app.client is None and app.connection == "local"
            fake.close.assert_awaited_once()
            fake.revoke_current_session.assert_not_awaited()
            client = await app.get_client()  # np. Synchronizuj - łączy się zapisanym tokenem, bez pytania
            assert client is app.client and app.connection == "online"
            assert [c.token for c in made if isinstance(c, TokenCredentials)] == ["SAVED"]

    asyncio.run(scenario())


def test_after_rejected_credentials_a_new_token_is_requested(seeded, monkeypatch):
    from ksefik.ui.setup_screen import TokenScreen

    app, _, _ = seeded
    made = real_login(app, monkeypatch)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            app.login_failed()  # KSeF odrzucił zapisany token
            asyncio.get_running_loop().create_task(app.get_client())
            await pilot.pause()
            assert isinstance(app.screen, TokenScreen) and made == []

    asyncio.run(scenario())


def test_end_session_revokes_only_the_session(seeded):
    app, fake, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "session")
            await app.workers.wait_for_complete()
            fake.revoke_current_session.assert_awaited_once()
            fake.close.assert_not_awaited()
            assert app.client is fake and app.connection == "online"
            assert "Sesja w KSeF została unieważniona" in messages(app)

    asyncio.run(scenario())


def test_forget_token_asks_then_deletes_it_and_disconnects(seeded, monkeypatch):
    app, fake, _ = seeded
    deleted = []
    monkeypatch.setattr(app_module, "delete_token", lambda env, nip: deleted.append((env, nip)))
    monkeypatch.setenv("KSEF_TOKEN", "from-env")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            app.logout()
            await pilot.pause()
            forget = next(d for _, d, v in app.screen.choices if v == "forget")
            assert "KSEF_TOKEN" in forget  # zmienna nadal podaje token - mówimy o tym przed wyborem
            app.screen.dismiss("forget")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and deleted == []
            assert app.screen.query_one("#cancel", Button).has_focus  # Enter nie usuwa
            await pilot.click("#confirm")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert deleted == [(app.profile.env, app.profile.context_id)]
            assert app.client is None and app.connection == "login"
            fake.close.assert_awaited_once()
            fake.revoke_current_session.assert_not_awaited()
            assert "KSEF_TOKEN" in messages(app)

    asyncio.run(scenario())


def test_revoke_token_opens_the_token_list(seeded):
    app, fake, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await choose(app, pilot, "revoke")
            assert isinstance(app.screen, account.TokensScreen)
            fake.revoke_current_session.assert_not_awaited()

    asyncio.run(scenario())


def test_certificate_login_has_no_token_options(seeded, monkeypatch):
    app, _, _ = seeded
    monkeypatch.setattr(app, "_profile", replace(app.profile, auth=AuthSettings(metoda="certyfikat")))

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            app.logout()
            await pilot.pause()
            assert choice_labels(app.screen) == ["Pracuj lokalnie", "Zakończ bieżącą sesję"]

    asyncio.run(scenario())


def test_new_token_is_usable_only_after_activation(seeded, monkeypatch):
    app, fake, _ = seeded
    release = asyncio.Event()
    saved = []
    monkeypatch.setattr(account, "save_token", lambda env, nip, token: saved.append(token) or "pęk kluczy")

    async def activation(reference):
        await release.wait()
        return {"status": "Active"}

    fake.wait_for_token.side_effect = activation

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TokenCreatedScreen("SECRET", "Program księgowy", "T-1")
            await app.push_screen(screen)
            await pilot.pause()
            use = screen.query_one("#use", Button)
            assert use.disabled and "aktywuje token" in str(screen.query_one("#token-state").render())
            screen._use()
            assert saved == []
            release.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert not use.disabled and "Token jest aktywny" in str(screen.query_one("#token-state").render())
            await pilot.click("#use")
            await pilot.pause()
            assert saved == ["SECRET"] and app.screen is not screen

    asyncio.run(scenario())


def test_failed_activation_explains_and_keeps_use_disabled(seeded):
    app, fake, _ = seeded
    fake.wait_for_token.return_value = {"status": "Failed", "statusDetails": ["Brak uprawnień autora."]}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TokenCreatedScreen("SECRET", "Program", "T-1")
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Brak uprawnień autora" in str(screen.query_one("#token-state").render())
            assert screen.query_one("#use", Button).disabled

    asyncio.run(scenario())


def test_one_time_token_is_not_closed_by_accident(seeded):
    app, fake, _ = seeded
    fake.wait_for_token.side_effect = KsefError("Przekroczono czas oczekiwania.")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TokenCreatedScreen("SECRET", "Program", "T-1")
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert "Nie udało się potwierdzić aktywacji" in str(screen.query_one("#token-state").render())
            await pilot.press("escape")
            await pilot.pause()
            guard = app.screen
            assert isinstance(guard, ConfirmScreen) and str(guard.query_one("#cancel", Button).label) == "Wróć"
            await pilot.press("enter")  # domyślnie „Wróć”
            await pilot.pause()
            assert app.screen is screen
            await pilot.click("#copy")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is not screen and not isinstance(app.screen, ConfirmScreen)

    asyncio.run(scenario())
