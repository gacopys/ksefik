"""Pozostałe P2 (WP17): import sprawdzany w tle z przerwaniem, identyfikator zbiorczy z listy faktur,
bezpieczne dane testowe, stronicowanie sesji i częściowe UPO sesji (UX-30, UX-31, UX-32)."""

import asyncio
import threading
from datetime import date
from decimal import Decimal

import pytest
from textual.widgets import Input, OptionList, SelectionList

from ksefik.client import KsefClient, KsefError, TokenCredentials
from ksefik.config import ENVIRONMENTS
from ksefik.invoice import build_xml, new_draft
from ksefik.ui import account, import_screen
from ksefik.ui.common import ConfirmScreen, FormScreen
from ksefik.ui.import_screen import ImportScreen


def text(widget) -> str:
    return str(widget.render())


def test_import_checks_files_in_the_background_and_can_be_stopped(seeded, isolated, monkeypatch):
    app, _, _ = seeded
    folder = isolated / "xml"
    folder.mkdir()
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    for n in range(30):
        (folder / f"f{n:02}.xml").write_bytes(build_xml(draft.with_changes(number=f"IMP/{n}/2026")))
    release = threading.Event()
    real = import_screen.load_file

    def slow(path, nip):
        if path.name == "f02.xml":
            release.wait(5)  # trzeci plik „sprawdza się” długo - w tym czasie użytkownik przerywa
        return real(path, nip)

    monkeypatch.setattr(import_screen, "load_file", slow)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#import-path", Input).value = str(folder)
            worker = screen._load()
            for _ in range(50):
                await pilot.pause(0.02)
                if "3 z 30" in text(screen.query_one("#import-status")):
                    break
            assert screen.loading and "Sprawdzam pliki: 3 z 30" in text(screen.query_one("#import-status"))
            assert screen.query_one("#load").disabled and screen.query_one("#send").disabled
            await pilot.press("escape")  # przerywa sprawdzanie, nie zamyka ekranu
            release.set()
            for _ in range(100):  # przerwany wątek kończy bieżący plik i oddaje to, co sprawdził
                await pilot.pause(0.02)
                if not screen.loading:
                    break
            assert worker.is_cancelled
            assert app.screen is screen and not screen.loading
            assert len(screen.files) == 3
            assert "Przerwano - sprawdzono 3 z 30" in text(screen.query_one("#import-status"))

    asyncio.run(scenario())


def test_missing_files_are_explained_in_place(seeded, isolated):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#import-path", Input).value = str(isolated / "brak" / "*.xml")
            await screen._load().wait()
            await pilot.pause()
            assert "Nie znaleziono plików XML" in text(screen.query_one("#import-status"))

    asyncio.run(scenario())


def test_collective_identifier_is_built_from_the_invoice_list(seeded):
    app, fake, store = seeded
    fake.query_collective_identifiers.return_value = []
    fake.generate_collective_identifier.return_value = "ZB-1"

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = account.CollectiveScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            screen.action_new()
            await pilot.pause()
            form = app.screen
            assert isinstance(form, FormScreen)
            choices = form.query_one("#f-chosen", SelectionList)
            assert choices.option_count > 10  # faktury z lokalnej bazy
            first, second = (choices.get_option_at_index(i).value for i in (0, 1))
            choices.select(first)
            choices.select(second)
            form.query_one("#f-numbers").load_text(first)  # ten sam numer wpisany ręcznie - liczy się raz
            form.action_submit()
            await pilot.pause()
            review = app.screen
            assert isinstance(review, ConfirmScreen)
            assert "Faktur: 2" in text(review.query_one(".dialog-text")) and "razem" in text(
                review.query_one(".dialog-text")
            )
            review._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            fake.generate_collective_identifier.assert_awaited_once_with(
                [{"ksefNumber": first}, {"ksefNumber": second}], None
            )
            assert "ZB-1" in text(screen.query_one("#op-status"))  # wynik zostaje pod listą

    asyncio.run(scenario())


def test_test_data_checks_ranges_and_confirms_destructive_operations(seeded, monkeypatch):
    app, fake, _ = seeded
    monkeypatch.setattr(app, "_profile", app.profile.with_env("test"))

    async def choose(screen, pilot, title):
        options = screen.query_one("#operations", OptionList)
        options.highlighted = next(n for n, op in enumerate(screen.operations) if op[0] == title)
        options.action_select()
        await pilot.pause()

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = account.TestDataScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await choose(screen, pilot, "Ustaw limity sesji")
            form = app.screen
            form.query_one("#f-size", Input).value = "9"
            form.action_submit()
            assert "liczba od 0 do 5" in text(form.query_one("#form-error"))
            form.dismiss(None)
            await pilot.pause()
            await choose(screen, pilot, "Przywróć domyślne limity API")
            assert isinstance(app.screen, ConfirmScreen)  # bez pól, ale zmienia stan - pytamy
            await pilot.press("enter")  # domyślnie Anuluj
            await pilot.pause()
            fake.testdata.assert_not_awaited()
            await choose(screen, pilot, "Usuń osobę fizyczną")
            form = app.screen
            form.query_one("#f-nip", Input).value = "5260250274"
            form.action_submit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen) and "zostanie usunięta" in text(
                app.screen.query_one(".dialog-text")
            )

    asyncio.run(scenario())


def test_test_data_is_refused_outside_the_test_environment(seeded):
    app, fake, _ = seeded  # profil produkcyjny

    async def scenario():
        async with app.run_test(size=(80, 24)):
            screen = account.TestDataScreen.__new__(account.TestDataScreen)
            problem = await account.TestDataScreen._run(screen, "x", ("rate-limits", None, "DELETE"))
            assert "tylko na środowisku testowym" in problem
            fake.testdata.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("env", ["prod", "demo"])
def test_client_never_sends_test_data_to_production_or_demo(env):
    client = KsefClient(ENVIRONMENTS[env]["api"], "5438539502", TokenCredentials("t"))

    async def scenario():
        with pytest.raises(KsefError, match="tylko na środowisku testowym"):
            await client.testdata("rate-limits", None, method="DELETE")
        await client.close()

    asyncio.run(scenario())


def session(n: int) -> dict:
    return {
        "referenceNumber": f"S-{n}",
        "dateCreated": "2026-10-20T10:00:00Z",
        "dateUpdated": "2026-10-20T10:01:00Z",
        "status": {"code": 200, "description": "Sesja przetworzona"},
        "successfulInvoiceCount": 1,
        "totalInvoiceCount": 1,
        "failedInvoiceCount": 0,
    }


def test_sessions_beyond_the_first_page_can_be_loaded(seeded):
    app, fake, _ = seeded
    fake.list_sessions.side_effect = lambda kind, created_from=None, limit=None: [session(n) for n in range(limit)][
        : min(limit, 700)
    ]

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = account.SessionsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(screen.items) == 500 and screen.truncated
            assert "m wczytuje starsze" in text(screen.query_one("#op-status"))
            await pilot.press("m")
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert len(screen.items) == 700 and not screen.truncated
            assert fake.list_sessions.call_args.kwargs["limit"] == 1001

    asyncio.run(scenario())


def test_partial_session_upo_saves_what_it_can_and_says_what_is_missing(seeded, isolated):
    app, fake, _ = seeded
    fake.list_sessions.return_value = [session(1)]
    fake.session_status.return_value = {"upo": {"pages": [{"referenceNumber": f"P{n}"} for n in range(3)]}}

    async def page(reference, page_ref):
        if page_ref == "P1":
            raise KsefError("UPO jeszcze się generuje.")
        return f"<upo {page_ref}/>".encode()

    fake.session_upo.side_effect = page

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = account.SessionsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            await screen.action_session_upo().wait()
            await pilot.pause()
            saved = sorted(p.name for p in (isolated / "Pobrane").glob("UPO_*.xml"))
            assert saved == ["UPO_S-1_P0.xml", "UPO_S-1_P2.xml"]
            assert "pobrano 2 z 3 stron" in text(screen.query_one("#op-status"))

    asyncio.run(scenario())
