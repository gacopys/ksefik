"""Konto KSeF: sesje i UPO, tokeny, sesje logowania, limity, Peppol, identyfikatory zbiorcze."""

import asyncio

import pytest
from helpers import (
    choose,
    draft_for,
    fill_form,
    running_app,
    send_drafts,
    set_value,
    wait_items,
    wait_loaded,
    wait_notification,
    wait_screen,
    wait_until,
    write_profile,
)
from textual.widgets import DataTable

from ksefik.invoice import build_xml
from ksefik.ui.account import (
    AccountScreen,
    AuthSessionsScreen,
    CollectiveScreen,
    LimitsScreen,
    PeppolScreen,
    SessionInvoicesScreen,
    SessionsScreen,
    TokenCreatedScreen,
    TokensScreen,
)
from ksefik.ui.common import ConfirmScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.sent_screen import SentScreen


@pytest.fixture(scope="module")
def sessions_data(company, partner):
    """Dwie faktury w sesjach interaktywnych i paczka wsadowa - przygotowane przez API."""

    async def prepare():
        async with company.client(token=True) as client:
            online = await send_drafts(
                client, [draft_for(company, partner.nip, f"KONTO/{n}", amount="50.00") for n in (1, 2)]
            )
            files = [(f"wsad-{n}.xml", build_xml(draft_for(company, partner.nip, f"KONTO/W/{n}"))) for n in (1, 2)]
            batch = await client.send_batch(files)
            await client.wait_for_session(batch)
            return {"online": [(r.session_ref, r.ksef_number) for _, _, r in online], "batch": batch}

    return asyncio.run(prepare())


def select_row(screen, predicate) -> dict:
    """Ustawia kursor listy na pierwszej pozycji spełniającej warunek."""
    index = next(i for i, item in enumerate(screen.items) if predicate(item))
    screen.query_one("#list", DataTable).move_cursor(row=index)
    return screen.items[index]


async def open_section(pilot, label: str, cls):
    if not isinstance(pilot.app.screen, AccountScreen):
        await pilot.press("k")
        await wait_screen(pilot, AccountScreen)
    await choose(pilot, label)
    return await wait_screen(pilot, cls)


def test_sessions_and_upo(isolated_app, company, partner, monkeypatch, downloads, sessions_data):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    session_ref, ksef_number = sessions_data["online"][0]

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            # Sesje w KSeF są teraz pod Wysyłkami i UPO (w), a nie w administracji konta.
            await pilot.press("w")
            await wait_screen(pilot, SentScreen)
            await pilot.press("e")
            sessions = await wait_screen(pilot, SessionsScreen)
            await wait_items(pilot, sessions, minimum=2)
            item = select_row(sessions, lambda i: i["referenceNumber"] == session_ref)
            assert item["successfulInvoiceCount"] == 1
            await pilot.press("enter")
            invoices = await wait_screen(pilot, SessionInvoicesScreen)
            items = await wait_items(pilot, invoices)
            assert items[0]["ksefNumber"] == ksef_number
            await pilot.press("u")
            await wait_notification(pilot, f"UPO_{ksef_number}.xml")
            await pilot.press("f")  # tylko błędne - w tej sesji nie ma
            await wait_until(pilot, lambda: not invoices.items, "pusta lista błędnych faktur")
            await wait_loaded(pilot, invoices)
            await pilot.press("escape")

            # Sesje wsadowe i UPO zbiorcze całej sesji.
            set_value(sessions, "#session-type", "Batch")
            await wait_until(
                pilot,
                lambda: any(i["referenceNumber"] == sessions_data["batch"] for i in sessions.items),
                "sesja wsadowa na liście",
            )
            select_row(sessions, lambda i: i["referenceNumber"] == sessions_data["batch"])
            await pilot.press("u")
            await wait_notification(pilot, f"UPO_{sessions_data['batch']}")

    asyncio.run(scenario())
    names = [p.name for p in downloads.iterdir()]
    assert f"UPO_{ksef_number}.xml" in names
    assert any(name.startswith(f"UPO_{sessions_data['batch']}") for name in names)


def test_tokens_generate_use_and_revoke(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            tokens = await open_section(pilot, "Tokeny KSeF", TokensScreen)
            before = await wait_items(pilot, tokens)

            # Nowy token pokazany raz - zamykamy i unieważniamy go z listy.
            await pilot.press("n")
            await fill_form(pilot, {"description": "Token do unieważnienia (test e2e)", "permissions": ["InvoiceRead"]})
            created = await wait_screen(pilot, TokenCreatedScreen)
            assert len(created.token) > 20
            await pilot.click("#close")  # bez kopiowania - okno pyta, czy na pewno
            await wait_screen(pilot, ConfirmScreen)
            await pilot.click("#confirm")
            await wait_until(pilot, lambda: len(tokens.items) == len(before) + 1, "nowy token na liście")
            select_row(tokens, lambda t: t["description"] == "Token do unieważnienia (test e2e)")
            await pilot.press("delete")
            await wait_screen(pilot, ConfirmScreen)
            await pilot.click("#confirm")
            await wait_notification(pilot, "Token unieważniony")
            await wait_until(
                pilot,
                lambda: any(
                    t["description"].startswith("Token do unieważnienia") and t["status"] in ("Revoking", "Revoked")
                    for t in tokens.items
                ),
                "status unieważnionego tokena",
            )

            # Drugi token „używany w tej aplikacji” - klient przełącza się na niego od razu.
            await pilot.press("n")
            await fill_form(
                pilot, {"description": "Token aplikacji (test e2e)", "permissions": ["InvoiceRead", "InvoiceWrite"]}
            )
            created = await wait_screen(pilot, TokenCreatedScreen)
            # „Używaj” działa dopiero po aktywacji tokena w KSeF - okno samo na nią czeka.
            await wait_until(pilot, lambda: created.state == "active", "aktywacja nowego tokena")
            await pilot.click("#use")
            await wait_notification(pilot, "Aplikacja używa nowego tokena")
            tokens.action_reload()
            # Token bez uprawnienia CredentialsRead widzi w KSeF tylko samego siebie.
            await wait_until(
                pilot,
                lambda: [t["description"] for t in tokens.items] == ["Token aplikacji (test e2e)"],
                "lista widziana nowym tokenem",
            )

    asyncio.run(scenario())


def test_auth_sessions_limits_peppol(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def other_session() -> str:
        client = company.client(token=True)
        await client.ensure_auth()
        reference = client.auth_reference
        await client.close()
        return reference

    async def scenario():
        other = await other_session()
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            sessions = await open_section(pilot, "Sesje logowania", AuthSessionsScreen)
            await wait_until(
                pilot, lambda: any(s["referenceNumber"] == other for s in sessions.items), "druga sesja logowania"
            )
            assert any(s.get("isCurrent") for s in sessions.items)
            select_row(sessions, lambda s: s["referenceNumber"] == other)
            await pilot.press("delete")
            await wait_screen(pilot, ConfirmScreen)
            await pilot.click("#confirm")
            await wait_notification(pilot, "Sesja unieważniona")
            await wait_until(
                pilot,
                lambda: sessions.items and all(s["referenceNumber"] != other for s in sessions.items),
                "sesja usunięta z listy",
            )
            await pilot.press("escape")

            limits = await open_section(pilot, "Limity", LimitsScreen)
            summary = limits.query_one("#limits-summary")
            await wait_until(pilot, lambda: "Sesja interaktywna" in str(summary.render()), "limity sesji")
            assert limits.query_one("#rate-limits", DataTable).row_count >= 10
            await pilot.press("escape")

            peppol = await open_section(pilot, "Dostawcy Peppol", PeppolScreen)
            await wait_loaded(pilot, peppol)

    asyncio.run(scenario())


def test_collective_identifier(isolated_app, company, partner, monkeypatch, sessions_data):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    numbers = [ksef for _, ksef in sessions_data["online"]]

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            screen = await open_section(pilot, "Identyfikatory zbiorcze", CollectiveScreen)
            await wait_loaded(pilot, screen)
            await pilot.press("n")
            await fill_form(pilot, {"numbers": "\n".join(numbers), "description": "Płatność zbiorcza (test e2e)"})
            await wait_notification(pilot, "Identyfikator wygenerowany")
            items = await wait_items(pilot, screen)
            assert items[0]["invoiceCount"] == 2
            await pilot.press("enter")
            details = screen.query_one("#list-details")
            await wait_until(pilot, lambda: all(n in str(details.render()) for n in numbers), "faktury identyfikatora")

    asyncio.run(scenario())
