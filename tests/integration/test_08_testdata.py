"""Dane testowe (tylko środowisko testowe): podmioty, osoby, uprawnienia, załączniki, blokada kontekstu, limity."""

import asyncio
import contextlib

from helpers import choose, fill_form, running_app, wait_notification, wait_screen, write_profile
from ksef_env import make_company, random_nip, random_pesel
from test_05_account import open_section

from ksefik.client import AuthError
from ksefik.ui import account
from ksefik.ui.common import ConfirmScreen
from ksefik.ui.main_screen import MainScreen


async def run_operation(pilot, label: str, values: dict | None = None) -> None:
    pilot.app._notifications.clear()
    await choose(pilot, label)
    if values is not None:
        await fill_form(pilot, values)  # potwierdza też opis skutku operacji usuwającej
    elif label in account.TEST_CONSEQUENCES:
        await wait_screen(pilot, ConfirmScreen)
        await pilot.click("#confirm")
    await wait_notification(pilot, "Gotowe")
    await wait_screen(pilot, account.TestDataScreen)


def test_testdata_operations(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    subject, person_nip, pesel = random_nip(), random_nip(), random_pesel()
    blocked = make_company("Blokowana", isolated_app / "pieczecie")

    async def can_login(target) -> bool:
        try:
            async with target.client() as client:
                await client.ensure_auth()
            return True
        except AuthError:
            return False

    async def scenario():
        assert await can_login(blocked)
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            await open_section(pilot, "Dane testowe", account.TestDataScreen)
            client = app.client
            try:
                await run_operation(
                    # JST i grupa VAT zwracają obecnie na środowisku testowym HTTP 503 (błąd po stronie KSeF).
                    pilot,
                    "Utwórz podmiot",
                    {"nip": subject, "type": "EnforcementAuthority", "description": "Organ z testu e2e"},
                )
                await run_operation(
                    pilot,
                    "Utwórz osobę fizyczną",
                    {"nip": person_nip, "pesel": pesel, "bailiff": False, "description": "Osoba z testu e2e"},
                )
                grant = {"context": subject, "kind": "Nip", "value": person_nip}
                await run_operation(
                    pilot,
                    "Nadaj uprawnienia testowe",
                    {**grant, "permissions": ["InvoiceRead"], "description": "Uprawnienie z testu e2e"},
                )
                await run_operation(pilot, "Odbierz uprawnienia testowe", grant)

                await run_operation(pilot, "Zezwól na faktury z załącznikiem", {"nip": company.nip})
                assert (await client.attachment_permission_status())["isAttachmentAllowed"] is True
                await run_operation(pilot, "Cofnij zgodę na załączniki", {"nip": company.nip})
                status = await client.attachment_permission_status()
                assert status["isAttachmentAllowed"] is False or status.get("revokedDate")

                await run_operation(pilot, "Zablokuj kontekst", {"type": "Nip", "value": blocked.nip})
                assert not await can_login(blocked)
                await run_operation(pilot, "Odblokuj kontekst", {"type": "Nip", "value": blocked.nip})
                assert await can_login(blocked)

                await run_operation(pilot, "Ustaw limity sesji", {"size": "1", "attachment": "2", "count": "321"})
                assert (await client.context_limits())["onlineSession"]["maxInvoices"] == 321
                await run_operation(pilot, "Przywróć domyślne limity sesji")
                assert (await client.context_limits())["onlineSession"]["maxInvoices"] != 321

                await run_operation(pilot, "Ustaw limity certyfikatów", {"enrollments": "7", "certificates": "5"})
                limits = await client.subject_limits()
                assert limits["certificate"]["maxCertificates"] == 5 and limits["enrollment"]["maxEnrollments"] == 7
                await run_operation(pilot, "Przywróć domyślne limity certyfikatów")
                assert (await client.subject_limits())["certificate"]["maxCertificates"] != 5

                default_rates = await client.rate_limits()
                await run_operation(pilot, "Ustaw produkcyjne limity API")
                production = await client.rate_limits()
                await run_operation(pilot, "Przywróć domyślne limity API")
                assert await client.rate_limits() == default_rates
                assert production != default_rates or production["global"]  # produkcyjne wczytane bez błędu

                await run_operation(pilot, "Usuń osobę fizyczną", {"nip": person_nip})
                await run_operation(pilot, "Usuń podmiot", {"nip": subject})
            finally:
                # Niezależnie od wyniku przywracamy domyślne limity firmy testowej.
                for operation in ("limits/context/session", "limits/subject/certificate", "rate-limits"):
                    with contextlib.suppress(Exception):
                        await client.testdata(operation, None, method="DELETE")

    asyncio.run(scenario())
