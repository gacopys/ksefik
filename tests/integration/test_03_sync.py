"""Synchronizacja z KSeF: faktury wystawione, otrzymane i „jako podmiot 3”, wyszukiwanie, pobieranie po numerze."""

import asyncio

import pytest
from helpers import (
    draft_for,
    fill_form,
    running_app,
    send_drafts,
    set_value,
    wait_notification,
    wait_screen,
    wait_stored,
    wait_until,
    write_profile,
)
from ksef_env import issue_token, make_company, random_nip

from ksefik.ui.invoice_screens import InvoiceScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.widgets import InvoiceDocument


@pytest.fixture(scope="module")
def own_company(it_home):
    """Osobna firma dla testów synchronizacji - inne testy wystawiają faktury w imieniu wspólnej firmy."""
    company = make_company("Firma Synchronizacja", it_home / "pieczecie")
    company.token = asyncio.run(issue_token(company))
    return company


@pytest.fixture
def company(own_company):
    return own_company


@pytest.fixture(scope="module")
def remote_invoices(own_company, partner):
    company = own_company
    """Faktury przygotowane przez API: wystawiona przez firmę, otrzymana od kontrahenta i z firmą jako podmiotem 3."""

    async def prepare():
        async with company.client(token=True) as own, partner.client() as other:
            issued = await send_drafts(own, [draft_for(company, partner.nip, "SYNC/WYST/1", amount="111.00")])
            received = await send_drafts(
                other,
                [
                    draft_for(partner, company.nip, "SYNC/OTRZ/1", amount="222.00"),
                    draft_for(partner, random_nip(), "SYNC/P3/1", amount="333.00", third=company),
                ],
            )
            await wait_stored(own, issued)
            await wait_stored(other, received)
            return {d.number: r.ksef_number for d, _, r in issued + received}

    return asyncio.run(prepare())


def test_sync_all_directions_search_and_preview(isolated_app, company, partner, monkeypatch, remote_invoices):
    sync = '["wystawione", "otrzymane", "podmiot3"]'
    path = write_profile(isolated_app / "profile.toml", company, partner, sync=sync)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path, sync=True) as (app, pilot):
            main = await wait_screen(pilot, MainScreen)
            # Pusta baza - synchronizacja rusza sama po starcie.
            message = await wait_notification(pilot, "Nowe faktury", timeout=300)
            assert "wystawione: 1" in message and "otrzymane: 1" in message and "jako podmiot 3: 1" in message
            await wait_until(pilot, lambda: not main._syncing, "koniec synchronizacji")

            seen = {}
            for key, direction in (("1", "issued"), ("2", "received"), ("3", "third")):
                await pilot.press(key)
                await pilot.pause(0.3)
                assert main.direction == direction
                seen[direction] = [row.invoice_number for row in main._rows]
            assert seen == {"issued": ["SYNC/WYST/1"], "received": ["SYNC/OTRZ/1"], "third": ["SYNC/P3/1"]}

            # Zakładka „Wszystkie” i wyszukiwanie pełnotekstowe (bez polskich znaków, po fragmencie nazwy).
            await pilot.press("4")
            await pilot.pause(0.3)
            assert len(main._rows) == 3
            await pilot.press("slash")
            await pilot.press(*"kontrahent testowy")
            await wait_until(pilot, lambda: len(main._rows) == 3, "wyniki wyszukiwania po nazwie kontrahenta")
            set_value(main, "#search", "SYNC/OTRZ")
            await wait_until(
                pilot, lambda: [r.invoice_number for r in main._rows] == ["SYNC/OTRZ/1"], "wynik „SYNC/OTRZ”"
            )

            # Podgląd faktury otrzymanej - pełna treść z eksportu.
            await pilot.press("escape")
            await pilot.press("enter")
            preview = await wait_screen(pilot, InvoiceScreen)
            view = preview.query_one(InvoiceDocument).view
            assert view.seller.nip == partner.nip and view.buyer.nip == company.nip
            assert str(view.total) == "273.06"
            await pilot.press("escape")

            # Kolejna synchronizacja pobiera tylko przyrost - nic nowego.
            await pilot.press("r")
            await wait_notification(pilot, "Brak nowych faktur", timeout=300)

    asyncio.run(scenario())


def test_fetch_by_ksef_number_and_missing_content(isolated_app, company, partner, monkeypatch, remote_invoices):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path) as (app, pilot):
            main = await wait_screen(pilot, MainScreen)
            # Faktura, której jeszcze nie ma w bazie - pobranie po numerze KSeF (g).
            await pilot.press("g")
            await fill_form(pilot, {"ksef": remote_invoices["SYNC/OTRZ/1"]})
            preview = await wait_screen(pilot, InvoiceScreen)
            assert preview.row["direction"] == "received"
            assert preview.query_one(InvoiceDocument).view.number == "SYNC/OTRZ/1"
            await pilot.press("escape")

            # Same metadane (bez treści) - podgląd pozwala dociągnąć XML klawiszem g.
            ksef = remote_invoices["SYNC/WYST/1"]
            app.store.upsert_metadata(
                "issued",
                [
                    {
                        "ksefNumber": ksef,
                        "invoiceNumber": "SYNC/WYST/1",
                        "issueDate": "2026-01-01",
                        "seller": {"nip": company.nip, "name": company.name},
                        "buyer": {"identifier": {"type": "Nip", "value": partner.nip}, "name": partner.name},
                        "netAmount": 111.0,
                        "vatAmount": 25.53,
                        "grossAmount": 136.53,
                        "currency": "PLN",
                    }
                ],
                {},
            )
            main.reload(select=f"issued|{ksef}")
            await pilot.pause(0.3)
            await pilot.press("enter")
            preview = await wait_screen(pilot, InvoiceScreen)
            assert not preview.row["xml"]
            await pilot.press("g")
            await wait_until(pilot, lambda: app.screen.row["xml"], "pobranie treści faktury")
            lines = app.screen.query_one(InvoiceDocument).view.lines
            assert [line.name for line in lines] == ["Usługa testowa"]

    asyncio.run(scenario())
