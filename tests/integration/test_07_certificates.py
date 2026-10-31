"""Certyfikaty KSeF: wniosek (logowanie i offline), zapis plików, logowanie nowym certyfikatem, unieważnienie."""

import asyncio

from helpers import (
    fill_form,
    running_app,
    wait_items,
    wait_loaded,
    wait_notification,
    wait_screen,
    wait_until,
    write_profile,
)
from ksef_env import KEY_PASSWORD
from test_05_account import open_section, select_row

from ksefik.ui.account import AuthSessionsScreen
from ksefik.ui.certificates import CertificatesScreen
from ksefik.ui.common import ConfirmScreen, FormScreen
from ksefik.ui.main_screen import MainScreen

NEW_KEY_PASSWORD = "nowe-haslo-klucza"


def certificate_auth(cert, key) -> str:
    return f'metoda = "certyfikat"\ncertyfikat = "{cert}"\nklucz = "{key}"\n'


def test_token_login_explains_certificate_requirement(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            screen = await open_section(pilot, "Certyfikaty KSeF", CertificatesScreen)
            empty = screen.query_one("#list-empty")
            await wait_until(
                pilot, lambda: "zalogowaniu podpisem" in str(empty.render()), "podpowiedź o logowaniu podpisem"
            )

    asyncio.run(scenario())


def test_enroll_login_and_revoke(isolated_app, company, partner, monkeypatch):
    seal = write_profile(
        isolated_app / "pieczec.toml", company, partner, auth=certificate_auth(company.cert_path, company.key_path)
    )
    monkeypatch.setenv("KSEF_KEY_PASSWORD", KEY_PASSWORD)
    cert_dir = isolated_app / "config" / "certyfikaty"

    async def enroll():
        async with running_app(seal) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            screen = await open_section(pilot, "Certyfikaty KSeF", CertificatesScreen)
            await wait_loaded(pilot, screen)
            assert "możesz złożyć wniosek" in str(screen.query_one("#cert-limits").render())
            before = len(screen.items)

            for name, kind in (("Laptop e2e", "Authentication"), ("Offline e2e", "Offline")):
                await pilot.press("n")
                await fill_form(pilot, {"name": name, "type": kind, "password": NEW_KEY_PASSWORD})
                await wait_notification(pilot, "gotowy", timeout=240)
                app._notifications.clear()
            items = await wait_items(pilot, screen, minimum=before + 2)
            by_name = {i["name"]: i for i in items}
            assert by_name["Laptop e2e"]["type"] == "Authentication" and by_name["Offline e2e"]["type"] == "Offline"
            assert by_name["Laptop e2e"]["status"] == "Active"

            select_row(screen, lambda i: i["name"] == "Offline e2e")
            await pilot.press("d")
            await wait_notification(pilot, "Zapisano certyfikat")
            return by_name["Laptop e2e"]["certificateSerialNumber"]

    serial = asyncio.run(enroll())
    keys = sorted(cert_dir.glob("Laptop_e2e-*.klucz.pem"))
    certs = [p for p in sorted(cert_dir.glob("Laptop_e2e-*.pem")) if not p.name.endswith(".klucz.pem")]
    assert len(keys) == 1 and len(certs) == 1
    assert oct(keys[0].stat().st_mode & 0o777) == "0o600"
    assert b"ENCRYPTED" in keys[0].read_bytes()  # klucz zaszyfrowany podanym hasłem

    # Logowanie nowo wydanym certyfikatem KSeF, a następnie jego unieważnienie.
    own = write_profile(isolated_app / "ksef-cert.toml", company, partner, auth=certificate_auth(certs[0], keys[0]))
    monkeypatch.setenv("KSEF_KEY_PASSWORD", NEW_KEY_PASSWORD)

    async def login_and_revoke():
        async with running_app(own) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            sessions = await open_section(pilot, "Sesje logowania", AuthSessionsScreen)
            items = await wait_items(pilot, sessions)
            current = next(s for s in items if s.get("isCurrent"))
            assert current["authenticationMethod"] == "InternalCertificate"
            await pilot.press("escape")

            screen = await open_section(pilot, "Certyfikaty KSeF", CertificatesScreen)
            await wait_items(pilot, screen)
            select_row(screen, lambda i: i["certificateSerialNumber"] == serial)
            await pilot.press("delete")
            await fill_form(pilot, {"reason": "Superseded"})
            await wait_screen(pilot, ConfirmScreen)
            await pilot.click("#confirm")
            await wait_notification(pilot, "Certyfikat unieważniony")
            await wait_until(
                pilot,
                lambda: any(i["certificateSerialNumber"] == serial and i["status"] == "Revoked" for i in screen.items),
                "status unieważnionego certyfikatu",
            )
            assert not isinstance(app.screen, FormScreen)

    asyncio.run(login_and_revoke())
