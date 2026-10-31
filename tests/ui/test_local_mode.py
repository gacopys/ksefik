"""Praca lokalna (bez połączenia z KSeF) i czytelny stan połączenia w pasku u góry."""

import asyncio

from textual.widgets import Checkbox, Input

from ksefik.ui import app as app_module
from ksefik.ui.account import SessionsScreen
from ksefik.ui.invoice_screens import InvoiceScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.setup_screen import TokenScreen
from ui.harness import HarnessApp, make_profile, seed_store


def badge(app) -> str:
    return str(app.screen.query_one("#connection").render())


def test_without_a_token_the_cached_invoices_work_locally(isolated, monkeypatch):
    from ksefik.store import Store

    monkeypatch.setattr(app_module, "load_token", lambda *args: None)
    profile = make_profile(isolated)
    store = Store(isolated / "ksef.db")
    seed_store(store, profile)
    app = HarnessApp(profile, profile_path=profile.path, store=store, start=True)
    prompts = []

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            assert isinstance(app.screen, TokenScreen)
            await pilot.click("#cancel")  # „Pracuj lokalnie”
            await pilot.pause()
            await app.workers.wait_for_complete()
            main = app.screen
            assert isinstance(main, MainScreen) and app.connection == "local"
            assert "tryb lokalny" in badge(app)
            assert "tryb lokalny" in str(main.query_one("#sync-status").render())
            assert main.query_one("#invoices").row_count > 0  # lista z lokalnej bazy, bez pytania o logowanie
            await pilot.press("slash", *"przykladowo")  # bez polskich znaków
            await pilot.pause(0.3)
            assert main.query_one("#invoices").row_count >= 1
            await pilot.press("enter", "enter")
            await pilot.pause()
            assert isinstance(app.screen, InvoiceScreen)
            await pilot.press("escape")
            # Lista z KSeF: pytanie o token, a po rezygnacji jasny komunikat zamiast „Wczytuję…”.
            app.push_screen(SessionsScreen())
            await pilot.pause()
            assert isinstance(app.screen, TokenScreen) and not app.screen.first_run
            prompts.append(app.screen)
            await pilot.press("escape")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert "Wymaga połączenia z KSeF" in str(app.screen.query_one("#list-empty").render())

    asyncio.run(scenario())
    store.close()
    assert len(prompts) == 1


def test_rejected_credentials_switch_the_badge_and_ask_again(seeded):
    from ksefik.client import TokenCredentials

    app, fake, _ = seeded
    real = app_module.KsefApp.make_client(app, TokenCredentials("unieważniony"))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            app.client = real
            assert "połączono" in badge(app)
            real.on_credentials_rejected()  # tak woła klient, gdy KSeF odrzuci logowanie
            await pilot.pause()
            assert app.connection == "login" and app.client is None
            assert "zaloguj się" in badge(app)
            await app.workers.wait_for_complete()

    asyncio.run(scenario())


def test_token_screen_explains_environment_variable_and_can_show_the_token(seeded, monkeypatch):
    app, _, _ = seeded
    monkeypatch.setenv("KSEF_TOKEN", "ze-zmiennej")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = TokenScreen(first_run=False)
            await app.push_screen(screen)
            await pilot.pause()
            assert "KSEF_TOKEN" in "".join(str(w.render()) for w in screen.query(".dialog-text"))
            token = screen.query_one("#token", Input)
            assert token.password
            screen.query_one("#show-token", Checkbox).value = True
            await pilot.pause()
            assert not token.password

    asyncio.run(scenario())
