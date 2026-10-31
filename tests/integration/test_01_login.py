"""Logowanie: kreator profilu, token KSeF (także błędny), certyfikat z hasłem do klucza, wylogowanie."""

import asyncio

from helpers import (
    choose,
    fill_form,
    running_app,
    set_value,
    wait_items,
    wait_notification,
    wait_screen,
    wait_until,
    write_profile,
)
from ksef_env import KEY_PASSWORD

from ksefik.profile import load_profile
from ksefik.tokenstore import load_token, token_file
from ksefik.ui.account import AuthSessionsScreen
from ksefik.ui.common import ChoiceScreen, FormScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.setup_screen import PasswordScreen, TokenScreen


def test_first_run_wizard_then_token(isolated_app, company):
    """Pierwsze uruchomienie: kreator zapisuje profil, potem token - błędny jest odrzucany, poprawny zapisywany."""
    path = isolated_app / "nowy" / "profile.toml"

    async def scenario():
        async with running_app(path, env_override="test") as (app, pilot):
            await wait_screen(pilot, FormScreen)
            await fill_form(
                pilot,
                {
                    "env": "test",
                    "nip": company.nip,
                    "name": company.name,
                    "l1": "ul. Testowa 1",
                    "l2": "00-001 Warszawa",
                },
            )
            await fill_form(  # krok 2 kreatora: ustawienia faktur
                pilot,
                {
                    "account": "61 1090 1014 0000 0712 1981 2874",
                    "numbering": "FV/{nr}/{mm}/{rrrr}",
                    "dates": "dzisiaj",
                    "days": "21",
                },
            )
            token_screen = await wait_screen(pilot, TokenScreen)
            assert path.exists()

            set_value(token_screen, "#token", "to-nie-jest-token")
            await pilot.press("enter")
            status = token_screen.query_one("#setup-status")
            button = token_screen.query_one("#connect")
            await pilot.pause(0.5)
            await wait_until(pilot, lambda: not button.disabled, "koniec próby logowania błędnym tokenem")
            message = str(status.render())
            assert "Nie udało się zalogować" in message or "Logowanie odrzucone" in message, message
            assert isinstance(app.screen, TokenScreen)
            assert not token_file("test", company.nip).exists()

            set_value(token_screen, "#token", company.token)
            await pilot.press("enter")
            await wait_screen(pilot, MainScreen)
            await wait_notification(pilot, "Połączono z KSeF")

    asyncio.run(scenario())
    profile = load_profile(path)
    assert profile.env == "test" and profile.seller.nip == company.nip
    assert profile.invoice.termin_platnosci_dni == 21
    assert profile.accounts[0].number == "61109010140000071219812874"
    assert token_file("test", company.nip).exists()  # pęk kluczy wyłączony - token w pliku 0600
    assert load_token("test", company.nip) == company.token
    assert oct(token_file("test", company.nip).stat().st_mode & 0o777) == "0o600"


async def wait_until_text(pilot, widget, text: str, timeout: float = 60) -> None:
    from helpers import wait_until

    await wait_until(pilot, lambda: text in str(widget.render()), f"tekst „{text}”", timeout)


def test_certificate_login_with_password(isolated_app, company, partner):
    """Logowanie pieczęcią (XAdES): zły hasło do klucza jest odrzucane, poprawne loguje do KSeF."""
    auth = f'metoda = "certyfikat"\ncertyfikat = "{company.cert_path}"\nklucz = "{company.key_path}"\n'
    path = write_profile(isolated_app / "profile.toml", company, partner, auth=auth)

    async def scenario():
        async with running_app(path) as (app, pilot):
            screen = await wait_screen(pilot, PasswordScreen)
            set_value(screen, "#password", "zle-haslo")
            await pilot.press("enter")
            await wait_until_text(pilot, screen.query_one("#setup-status"), "Niepoprawne hasło")
            set_value(screen, "#password", KEY_PASSWORD)
            await pilot.press("enter")
            await wait_screen(pilot, MainScreen)
            # Lista sesji logowania potwierdza, że XAdES zadziałał (metoda: pieczęć kwalifikowana).
            sessions = AuthSessionsScreen()
            await app.push_screen(sessions)
            items = await wait_items(pilot, sessions)
            current = [i for i in items if i.get("isCurrent")]
            assert current and current[0]["authenticationMethod"] == "QualifiedSeal"

    asyncio.run(scenario())


def test_certificate_password_from_environment(isolated_app, company, partner, monkeypatch):
    auth = f'metoda = "certyfikat"\ncertyfikat = "{company.cert_path}"\nklucz = "{company.key_path}"\n'
    path = write_profile(isolated_app / "profile.toml", company, partner, auth=auth)
    monkeypatch.setenv("KSEF_KEY_PASSWORD", KEY_PASSWORD)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            assert app.client is not None
            await app.client.ensure_auth()
            assert app.client.auth_reference

    asyncio.run(scenario())


def test_logout_revokes_session_and_next_action_logs_in_again(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            await app.client.ensure_auth()
            first = app.client.auth_reference
            await pilot.press("ctrl+p")
            await pilot.pause(0.3)
            await pilot.press(*"Wyloguj")
            await pilot.pause(0.5)
            await pilot.press("enter")
            await wait_screen(pilot, ChoiceScreen)
            await choose(pilot, "Zakończ bieżącą sesję")
            await wait_notification(pilot, "Sesja w KSeF została unieważniona")
            # Następna operacja loguje się od nowa (nowa sesja uwierzytelnienia).
            sessions = AuthSessionsScreen()
            await app.push_screen(sessions)
            items = await wait_items(pilot, sessions)
            current = [i["referenceNumber"] for i in items if i.get("isCurrent")]
            assert current and current[0] != first
            assert first not in [i["referenceNumber"] for i in items]

    asyncio.run(scenario())
