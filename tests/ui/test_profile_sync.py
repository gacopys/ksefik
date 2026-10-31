"""Profil i synchronizacja: stan pobierania dla każdej zakładki, przerwa od KSeF, starsze faktury
i zmiany profilu bez restartu (UX-16, UX-17)."""

import asyncio
from datetime import UTC, date, datetime, timedelta

from textual.widgets import Input, Tab

from ksefik.client import RateLimitError
from ksefik.sync import SyncResult
from ksefik.ui import app as app_module
from ksefik.ui import main_screen
from ksefik.ui.common import ConfirmScreen, FormScreen
from ksefik.ui.main_screen import MainScreen


def status(screen) -> str:
    return str(screen.query_one("#sync-status").render())


def test_failed_direction_is_named_in_its_tab(seeded):
    app, _, store = seeded
    store.db.execute("DELETE FROM invoices WHERE direction = 'received'")
    store.db.commit()
    store.record_sync_attempt("Subject2", "Brak uprawnień do przeglądania faktur.", "permissions")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            assert "brak uprawnień" not in status(main)  # wystawione pobrały się dobrze
            main.query_one("#direction-tabs").active = "received"
            await pilot.pause()
            assert "otrzymane: brak uprawnień" in status(main)
            empty = str(main.query_one("#empty").render())
            assert "Nie udało się pobrać faktur" in empty and "Brak uprawnień do przeglądania" in empty

    asyncio.run(scenario())


def test_rate_limit_pauses_sync_until_the_time_ksef_asked_for(seeded, monkeypatch):
    app, _, _ = seeded
    calls = []

    async def limited(*args, **kwargs):
        calls.append(args)
        raise RateLimitError("KSeF chwilowo ogranicza liczbę zapytań.", retry_after=600)

    monkeypatch.setattr(main_screen, "sync", limited)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            until = app.sync_blocked_until
            assert until and timedelta(seconds=590) < until - datetime.now(UTC) <= timedelta(seconds=600)
            assert f"kolejna próba od {until.astimezone():%H:%M}" in status(main)
            await pilot.press("r")
            await app.workers.wait_for_complete()
            assert len(calls) == 1  # w czasie przerwy nie pytamy KSeF ponownie

    asyncio.run(scenario())


def test_older_invoices_are_fetched_for_the_chosen_period(seeded, monkeypatch):
    app, _, _ = seeded
    ranges = []

    async def backfill(client, store, date_from, date_to, progress=None, subjects=None):
        ranges.append((date_from, date_to, subjects))
        return SyncResult({"issued": 3})

    monkeypatch.setattr(main_screen, "backfill", backfill)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            main.action_backfill()
            await pilot.pause()
            form = app.screen
            assert isinstance(form, FormScreen)
            form.query_one("#f-date_from", Input).value = "01.06.2025"
            form.query_one("#f-date_to", Input).value = "01.01.2025"
            form.action_submit()
            assert app.screen is form  # „do” przed „od”
            form.query_one("#f-date_to", Input).value = "31.12.2025"
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert ranges == [(date(2025, 6, 1), date(2025, 12, 31), ["Subject1", "Subject2"])]
            assert any("Pobrano starsze faktury" in n.title for n in app._notifications)

    asyncio.run(scenario())


def test_profile_change_in_the_same_company_rebuilds_the_list(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            assert {t.id for t in main.query(Tab)} >= {"issued", "received", "all"}
            path = app.profile.path
            path.write_text(
                path.read_text().replace('synchronizuj = ["wystawione", "otrzymane"]', 'synchronizuj = ["wystawione"]')
            )
            app.reload_profile()
            await pilot.pause()
            await pilot.pause()
            assert [t.id for t in main.query(Tab)] == ["issued"]
            assert main.query_one("#invoices").row_count > 0

    asyncio.run(scenario())


def test_switching_environment_reopens_the_app_on_the_new_profile(seeded, monkeypatch):
    app, fake, store = seeded
    monkeypatch.setattr(app_module, "load_token", lambda env, nip: "TOKEN-TEST")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            path = app.profile.path
            path.write_text(path.read_text().replace('srodowisko = "prod"', 'srodowisko = "test"'))
            app.reload_profile()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "Produkcja → Testowe" in str(app.screen.query_one(".dialog-text").render())
            await pilot.press("enter")  # „Przełącz teraz”
            await pilot.pause()
            await pilot.pause()
            assert app.profile.env == "test" and app.store is not store
            assert app.store.path.parent == app.profile.data_dir
            assert isinstance(app.screen, MainScreen) and app.connection == "online"
            assert "TESTOWE" in str(app.screen.query_one("#env-badge").render())
            fake.close.assert_awaited()

    asyncio.run(scenario())


def test_failed_profile_write_is_reported_not_raised(seeded, monkeypatch):
    from ksefik.invoice import new_draft
    from ksefik.ui import editor

    app, _, _ = seeded

    def read_only(path, block):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(editor, "append_block", read_only)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            app.open_editor(new_draft(app.profile, date.today(), []))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#buyer_id", Input).value = "5260250274"  # kontrahent spoza profilu
            screen.query_one("#buyer_name", Input).value = "Nowy Kontrahent Sp. z o.o."
            screen.query_one("#buyer_l1", Input).value = "ul. Nowa 1"
            screen.action_save_contractor()
            await pilot.pause()
            assert any("Nie udało się zapisać profilu: brak uprawnień" in n.message for n in app._notifications)

    asyncio.run(scenario())
