import asyncio
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest
from textual.widgets import Checkbox, Input, Select, TextArea

from ksefik.client import KsefError, RateLimitError
from ksefik.invoice import Line, new_draft
from ksefik.profile import Party
from ksefik.sync import SyncResult
from ksefik.ui import account, main_screen
from ksefik.ui.common import ConfirmScreen
from ksefik.ui.editor import EditScreen, LineScreen
from ksefik.ui.import_screen import fetch_invoice_dialog
from ksefik.ui.main_screen import MainScreen


def test_editor_buyer_changes_currency_payment_period_and_contractors(ui):
    app, _, _ = ui

    async def scenario():
        results = []
        async with app.run_test(size=(160, 65)) as pilot:
            draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
            screen = EditScreen(draft)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            eu = app.profile.contractors[1]
            screen.query_one("#buyer_pick", Select).value = eu.party.key
            await pilot.pause()
            assert screen._buyer().vat_ue == eu.party.vat_ue
            assert screen.query_one("#currency", Input).value == "EUR"
            screen.query_one("#sale_mode", Select).value = "period"
            screen.query_one("#period_from", Input).value = "01.10.2026"
            screen.query_one("#period_to", Input).value = "31.10.2026"
            screen.query_one("#exchange_rate", Input).value = "4,25"
            screen.query_one("#paid", Checkbox).value = True
            await pilot.pause()
            assert screen.query_one("#paid_date", Input).value
            assert not screen.query_one("#row-due_date").display
            assert screen.query_one("#row-period_from").display
            screen.action_save()
            await pilot.pause()
            saved = results[0].draft
            assert saved.currency == "EUR" and saved.exchange_rate == Decimal("4.25")
            assert saved.period == (date(2026, 10, 1), date(2026, 10, 31))
            assert saved.payment.paid and saved.payment.due_date is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "id_type,identifier,country",
    [("nip", "bad", "PL"), ("vat_ue", "US123", "US"), ("nr_id", "", "US"), ("brak", "", "PL")],
)
def test_editor_validates_buyer_identifiers_before_saving(ui, id_type, identifier, country):
    app, _, _ = ui

    async def scenario():
        results = []
        async with app.run_test(size=(140, 60)) as pilot:
            screen = EditScreen(new_draft(app.profile, date.today(), [], net=Decimal(100)))
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.query_one("#buyer_id_type", Select).value = id_type
            screen.query_one("#buyer_id", Input).value = identifier
            screen.query_one("#buyer_country", Input).value = country
            await pilot.pause()
            screen.action_save()
            await pilot.pause()
            if id_type == "brak":
                assert results[0].draft.buyer.brak_id
            else:
                assert results == [] and app.screen is screen

    asyncio.run(scenario())


def test_editor_add_edit_remove_and_store_new_catalog_item(ui):
    app, _, _ = ui

    async def scenario():
        async with app.run_test(size=(160, 65)) as pilot:
            screen = EditScreen(new_draft(app.profile, date.today(), [], net=Decimal(100)))
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_add_line()
            await pilot.pause()
            line = app.screen
            assert isinstance(line, LineScreen)
            line.query_one("#name", Input).value = "Unique new item"
            line.query_one("#price", Input).value = "150"
            await pilot.pause()
            line._to_catalog()
            assert app.profile.item("Unique new item").cena_netto == Decimal(150)
            line._to_catalog()
            assert len([item for item in app.profile.items if item.nazwa == "Unique new item"]) == 1
            line.action_save()
            await pilot.pause()
            assert len(screen.lines) == 2
            screen.action_edit_line()
            await pilot.pause()
            app.screen.query_one("#quantity", Input).value = "2"
            await pilot.pause()
            app.screen.action_save()
            await pilot.pause()
            assert screen.lines[1].net == Decimal(300)
            screen.action_remove_line()
            assert len(screen.lines) == 1
            screen.lines = [Line("Annex", unit_price=Decimal(20000), annex15=True)]
            screen._refresh_lines()
            screen._auto_annotations()
            assert screen.query_one("#mpp", Checkbox).value
            screen.lines = [Line("EU", unit_price=Decimal(100), vat_rate="np II")]
            screen._auto_annotations()
            assert screen.query_one("#reverse", Checkbox).value

    asyncio.run(scenario())


def test_editor_saves_new_contractor_without_duplicating_existing(ui):
    app, _, _ = ui

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
            screen = EditScreen(draft)
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#buyer_id_type", Select).value = "brak"
            screen.query_one("#buyer_name", Input).value = "New private buyer"
            await pilot.pause()
            screen.action_save_contractor()
            assert app.profile.contractor(Party(nazwa="New private buyer", brak_id=True))
            count = len(app.profile.contractors)
            screen.action_save_contractor()
            assert len(app.profile.contractors) == count

    asyncio.run(scenario())


def test_token_generation_copy_use_and_revocation(ui, monkeypatch, clipboard):
    app, fake, _ = ui
    fake.list_tokens.return_value = [{"referenceNumber": "T", "status": "Active", "description": "test"}]
    fake.generate_token.return_value = {"token": "new-token", "referenceNumber": "T-NEW"}
    fake.wait_for_token.return_value = {"status": "Active"}
    saved = Mock(return_value="keyring")
    monkeypatch.setattr(account, "save_token", saved)
    monkeypatch.setattr(app, "make_client", lambda credentials: fake)

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            screen = account.TokensScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_new()
            await pilot.pause()
            form = app.screen
            form.query_one("#f-description", Input).value = "unit test token"
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert isinstance(app.screen, account.TokenCreatedScreen)
            fake.wait_for_token.assert_awaited_once_with("T-NEW")
            app.screen._copy()
            assert clipboard == ["new-token"]
            app.screen._use()
            await pilot.pause()
            saved.assert_called_once_with(app.profile.env, app.profile.context_id, "new-token")
            assert app.screen is screen
            screen.action_revoke()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            app.screen._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.revoke_token.assert_awaited_once_with("T")

    asyncio.run(scenario())


def test_collective_identifier_validates_numbers_and_copies_result(ui, monkeypatch, clipboard):
    app, fake, _ = ui
    fake.query_collective_identifiers.return_value = []
    fake.generate_collective_identifier.return_value = "C"
    numbers = ["5438539502-20261023-ABCDEF-123456-AB", "5438539502-20261023-ABCDEF-654321-BA"]

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            screen = account.CollectiveScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_new()
            await pilot.pause()
            form = app.screen
            field = form.query_one("#f-numbers", TextArea)
            field.load_text("bad")
            form.action_submit()
            assert app.screen is form
            field.load_text(numbers[0])
            form.action_submit()
            assert app.screen is form
            field.load_text(";\n".join(numbers + numbers[:1]))  # powtórzony numer liczy się raz
            form.action_submit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)  # przegląd: liczba faktur i suma
            assert "Faktur: 2" in str(app.screen.query_one(".dialog-text").render())
            app.screen._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.generate_collective_identifier.assert_awaited_once_with([{"ksefNumber": n} for n in numbers], None)
            assert clipboard == ["C"]

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "empty", "error", "rate-limit", "no-client"])
def test_main_synchronization_resets_busy_flag_on_all_outcomes(ui, monkeypatch, outcome):
    app, _, store = ui
    store.set_checkpoint("Subject1", datetime.now(UTC))
    mocked = AsyncMock(return_value=SyncResult({"issued": 1 if outcome == "success" else 0}))
    if outcome in ("error", "rate-limit"):
        mocked.side_effect = KsefError("failed") if outcome == "error" else RateLimitError("wait")
    if outcome == "no-client":
        app.get_client.return_value = None
    monkeypatch.setattr(main_screen, "sync", mocked)

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = MainScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_sync()
            screen.action_sync()
            await app.workers.wait_for_complete()
            assert not screen._syncing
            assert mocked.await_count == (0 if outcome == "no-client" else 1)
            screen.action_direction(3)
            await pilot.pause()
            assert screen.direction == "all"
            screen.query_one("#date-from", Input).value = "01.01.2020"
            await pilot.pause()
            assert screen.query_one("#period", Select).value == "custom"
            screen.action_move_selection(-1)
            screen.action_move_selection(1)

    asyncio.run(scenario())


@pytest.mark.parametrize("direction", ["issued", "received", "third", "other"])
def test_fetch_invoice_dialog_validates_and_stores_direction(ui, direction):
    from ksefik.invoice import build_xml
    from ksefik.profile import ThirdParty

    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    if direction != "issued":
        seller, buyer = draft.buyer, draft.seller if direction == "received" else draft.buyer
        draft = replace(draft, seller=seller, buyer=buyer)
    if direction == "third":  # nasza firma jako podmiot trzeci (np. płatnik)
        draft = replace(draft, third_parties=(ThirdParty(app.profile.seller, rola=6),))
    fake.get_invoice.return_value = build_xml(draft)
    number = "5438539502-20261023-ABCDEF-123456-AB"

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            fetch_invoice_dialog(app)
            await pilot.pause()
            form = app.screen
            form.query_one("#f-ksef", Input).value = "invalid"
            form.action_submit()
            assert app.screen is form
            form.query_one("#f-ksef", Input).value = f" {number.lower()} "  # wklejony z odstępami
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert store.get(number, direction)["xml"] == fake.get_invoice.return_value

    asyncio.run(scenario())
