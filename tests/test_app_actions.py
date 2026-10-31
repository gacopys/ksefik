import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Input, OptionList

from ksefik.client import KsefError, TokenCredentials
from ksefik.ui.account import AccountScreen
from ksefik.ui.common import ChoiceScreen
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import QuickInvoiceScreen, SummaryScreen
from ksefik.ui.setup_screen import TokenScreen


def test_new_invoice_edit_summary_navigation_returns_selected_invoice(ui, monkeypatch):
    app, _, _ = ui

    async def scenario():
        async with app.run_test(size=(160, 65)) as pilot:
            app.start_new_invoice()
            await pilot.pause()
            assert isinstance(app.screen, QuickInvoiceScreen)
            app.screen.query_one("#amount", Input).value = "100"
            await pilot.pause()
            app.screen._next()
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen)
            app.screen.action_edit()
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            app.screen.query_one("#notes", Input).value = "Updated"
            app.screen.action_save()
            await pilot.pause()
            assert isinstance(app.screen, SummaryScreen) and app.screen.draft.notes == "Updated"
            app.screen.action_close()
            await pilot.pause()
            assert not isinstance(app.screen, SummaryScreen)
            (kept,) = app.store.drafts()  # zamknięte podsumowanie zostawia szkic
            assert kept["buyer_name"] and kept["gross"] == 123

    asyncio.run(scenario())


def test_new_invoice_without_catalog_opens_editor(ui, monkeypatch):
    app, _, _ = ui
    app._profile = replace(app.profile, contractors=(), items=())

    async def scenario():
        async with app.run_test(size=(160, 65)) as pilot:
            app.start_new_invoice()
            await pilot.pause()
            assert isinstance(app.screen, EditScreen)
            assert app.screen.draft.buyer.nazwa == ""
            app.screen.action_close()
            await pilot.pause()
            assert not isinstance(app.screen, EditScreen)
            assert app.store.drafts() == []  # nic nie zmieniono - nie ma czego zapisywać

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_logout_end_session_revokes_only_the_session(ui, failure):
    app, fake, _ = ui
    app.client = fake
    if failure:
        fake.revoke_current_session.side_effect = KsefError("offline")

    async def scenario():
        async with app.run_test() as pilot:
            app.logout()
            await pilot.pause()
            assert isinstance(app.screen, ChoiceScreen)
            fake.revoke_current_session.assert_not_awaited()
            app.screen.dismiss("session")
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.revoke_current_session.assert_awaited_once()

    asyncio.run(scenario())


def test_change_token_swaps_client_and_closes_old_connection(ui):
    app, fake, _ = ui
    old = AsyncMock()
    app.client = old

    async def scenario():
        async with app.run_test() as pilot:
            app.change_token()
            await pilot.pause()
            assert isinstance(app.screen, TokenScreen)
            app.screen.dismiss(fake)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.client is fake
            old.close.assert_awaited_once()

    asyncio.run(scenario())


def test_make_client_applies_profile_context_and_security_settings(ui):
    app, _, _ = ui
    app._profile = replace(
        app.profile,
        auth=replace(
            app.profile.auth,
            kontekst="id_wewnetrzny",
            identyfikator="5438539502-00001",
            dozwolone_ip=("10.0.0.1",),
            weryfikuj_certyfikat=True,
        ),
    )

    async def scenario():
        client = app.make_client(TokenCredentials("secret"))
        try:
            assert client.context_type == "InternalId" and client.nip == "5438539502-00001"
            assert client.allowed_ips == {"ip4Addresses": ["10.0.0.1"]}
            assert client.verify_certificate_chain
        finally:
            await client.close()

    asyncio.run(scenario())


def test_account_navigation_hides_testdata_in_production_and_opens_sections(ui):
    app, _, _ = ui

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = AccountScreen()
            await app.push_screen(screen)
            await pilot.pause()
            options = screen.query_one("#sections", OptionList)
            assert options.option_count > 5
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert app.screen is not screen

    asyncio.run(scenario())


def test_external_editor_failure_is_reported_without_reloading(ui, monkeypatch):
    from textual.app import SuspendNotSupported

    app, _, _ = ui
    reload = Mock()
    monkeypatch.setattr(app, "reload_profile", reload)
    monkeypatch.setattr(app, "suspend", Mock(side_effect=SuspendNotSupported()))

    async def scenario():
        async with app.run_test():
            app.edit_profile()
            reload.assert_not_called()

    asyncio.run(scenario())
