import asyncio
from dataclasses import replace
from unittest.mock import Mock

import pytest

import ksefik
from ksefik import config
from ksefik.profile import AuthSettings, ProfileError, load_profile
from ksefik.ui import app as app_module
from ksefik.ui.app import KsefApp
from ksefik.ui.common import FormScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.setup_screen import PasswordScreen, TokenScreen, profile_from_wizard
from ksefik.xades import CredentialsError, PasswordRequired


def test_config_environment_overrides_expand_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("KSEFIK_PROFILE", raising=False)
    assert config.default_profile_path() == config.PROFILE_PATH
    monkeypatch.setenv("KSEFIK_PROFILE", "~/company.toml")
    assert config.default_profile_path() == config.Path("~/company.toml").expanduser()
    monkeypatch.setenv("XDG_TEST_HOME", str(tmp_path / "custom"))
    assert config._xdg("XDG_TEST_HOME", "fallback") == tmp_path / "custom"
    monkeypatch.delenv("XDG_TEST_HOME")
    assert config._xdg("XDG_TEST_HOME", "fallback") == tmp_path / "fallback"


@pytest.mark.parametrize("case", ["existing", "missing", "invalid"])
def test_cli_loads_selected_profile_applies_environment_or_reports_error(example_path, tmp_path, monkeypatch, case):
    app_factory = Mock()
    monkeypatch.setattr(app_module, "KsefApp", app_factory)
    path = example_path
    if case == "missing":
        path = tmp_path / "new.toml"
    elif case == "invalid":
        path.write_text("[broken")
    monkeypatch.setattr("sys.argv", ["ksefik", "--profile", str(path), "--env", "test"])
    if case == "invalid":
        with pytest.raises(SystemExit, match="Błąd składni"):
            ksefik.main()
        app_factory.assert_not_called()
    else:
        ksefik.main()
        profile = app_factory.call_args.args[0]
        assert profile is None if case == "missing" else profile.env == "test"
        assert app_factory.call_args.kwargs["profile_path"] == path
        app_factory.return_value.run.assert_called_once()


@pytest.mark.parametrize("valid", [False, True])
def test_wizard_saves_valid_profile_and_cleans_up_invalid_one(tmp_path, valid):
    values = {
        "env": "test",
        "name": 'Test "Company"',
        "nip": "5438539502" if valid else "123",
        "l1": "Street 1",
        "l2": None,
        "account": "61 1090 1014 0000 0712 1981 2874",
        "numbering": "FV/{nr}/{rrrr}",
        "dates": "dzisiaj_za_miesiac",
        "days": "30",
    }
    path = tmp_path / "profile.toml"
    if valid:
        profile = profile_from_wizard(path, values)
        assert profile.seller.nazwa == 'Test "Company"'
        assert profile.invoice.dzien_graniczny == 10 and profile.invoice.termin_platnosci_dni == 30
        assert profile == load_profile(path)
    else:
        with pytest.raises(ProfileError):
            profile_from_wizard(path, values)
        assert not path.exists()


@pytest.mark.parametrize("mode", ["stored-token", "no-token", "no-profile", "encrypted-key", "invalid-key"])
def test_real_app_startup_selects_main_login_or_setup(ui, monkeypatch, mode):
    harness, fake, store = ui
    profile = harness.profile
    if mode == "no-profile":
        profile = None
    elif mode in ("encrypted-key", "invalid-key"):
        profile = replace(profile, auth=AuthSettings(metoda="certyfikat", certyfikat="missing.pem"))
        monkeypatch.setattr(
            app_module,
            "load_signer",
            Mock(side_effect=PasswordRequired() if mode == "encrypted-key" else CredentialsError("invalid cert")),
        )
    monkeypatch.setattr(app_module, "load_token", lambda *args: "TOKEN" if mode == "stored-token" else None)
    app = KsefApp(profile, profile_path=harness.profile_path, store=store)
    monkeypatch.setattr(app, "make_client", lambda credentials: fake)
    monkeypatch.setattr(app, "reconcile_all", Mock())
    # Avoid automatic remote synchronization when the main screen mounts.
    monkeypatch.setattr(MainScreen, "action_sync", Mock())

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            await pilot.pause()
            expected = {
                "stored-token": MainScreen,
                "no-token": TokenScreen,
                "no-profile": FormScreen,
                "encrypted-key": PasswordScreen,
                "invalid-key": MainScreen,
            }[mode]
            assert isinstance(app.screen, expected)
            if mode == "stored-token":
                assert app.client is fake
                commands = [command.title for command in app.get_system_commands(app.screen)]
                assert "Nowa faktura" in commands and "Edytuj profil" in commands
            elif mode == "no-profile":
                app.screen.action_cancel()
                await pilot.pause()
                assert app.return_value is None

    asyncio.run(scenario())


@pytest.mark.parametrize("success", [False, True])
def test_token_screen_authenticates_before_saving_and_closes_failed_client(ui, monkeypatch, success):
    from ksefik.client import KsefError
    from ksefik.ui import setup_screen

    app, fake, _ = ui
    saved = Mock(return_value="keyring")
    monkeypatch.setattr(setup_screen, "save_token", saved)
    monkeypatch.setattr(app, "make_client", lambda credentials: fake)
    if not success:
        fake.ensure_auth.side_effect = KsefError("invalid token")

    async def scenario():
        results = []
        async with app.run_test() as pilot:
            screen = TokenScreen(first_run=False)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen._connect_pressed()
            fake.ensure_auth.assert_not_awaited()
            await screen.connect("secret").wait()
            await pilot.pause()
            if success:
                assert results == [fake]
                saved.assert_called_once_with(app.profile.env, app.profile.context_id, "secret")
            else:
                saved.assert_not_called()
                fake.close.assert_awaited_once()
                screen.action_cancel()
                await pilot.pause()
                assert results == [None]

    asyncio.run(scenario())


def test_reload_profile_updates_valid_settings_but_asks_before_identity_change_and_skips_invalid_file(ui):
    app, _, _ = ui

    async def scenario():
        async with app.run_test() as pilot:
            from ksefik.profile import save_new_profile
            from ksefik.ui.common import ConfirmScreen

            original = app.profile.path.read_text()
            save_new_profile(replace(app.profile, invoice=replace(app.profile.invoice, miejsce_wystawienia="Kraków")))
            app.reload_profile()
            assert app.profile.invoice.miejsce_wystawienia == "Kraków"
            app.profile.path.write_text(original.replace('srodowisko = "prod"', 'srodowisko = "test"'))
            app.reload_profile()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)  # inne środowisko - tylko po zgodzie
            app.screen.dismiss(False)
            await pilot.pause()
            assert app.profile.env == "prod"
            app.profile.path.write_text("[broken")
            app.reload_profile()
            assert app.profile.invoice.miejsce_wystawienia == "Kraków"

    asyncio.run(scenario())
