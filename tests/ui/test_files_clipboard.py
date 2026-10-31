"""Pliki, schowek i link weryfikacyjny: zapis nie nadpisuje plików, błąd zapisu da się naprawić,
a kopiowanie i otwieranie linków mówią prawdę o tym, co się udało (AT-11)."""

import asyncio
import os
from datetime import date
from decimal import Decimal

import pytest
from textual.widgets import Button, Input

from ksefik.client import SendResult
from ksefik.invoice import build_xml, new_draft
from ksefik.ui import app as app_module
from ksefik.ui import common
from ksefik.ui.common import ConfirmScreen, FormScreen, ValueScreen, check_folder, save_download
from ksefik.ui.invoice_screens import InvoiceScreen, SendScreen

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root zapisze także w folderze tylko do odczytu")


def latest_issued(store):
    return store.db.execute("SELECT * FROM invoices WHERE direction = 'issued' ORDER BY issue_date DESC").fetchone()


def test_saving_never_overwrites_and_reports_errors(tmp_path):
    first = save_download("FV 1.xml", b"a", tmp_path)
    assert first.ok and first.path.name == "FV-1.xml"
    again = save_download("FV 1.xml", b"a", tmp_path)
    assert again.existing and again.path == first.path  # ten sam plik - bez drugiej kopii
    other = save_download("FV 1.xml", b"b", tmp_path)
    assert other.path.name == "FV-1 (2).xml" and first.path.read_bytes() == b"a"
    locked = tmp_path / "tylko-odczyt"
    locked.mkdir(mode=0o500)
    failed = save_download("FV.xml", b"a", locked)
    assert not failed.ok and failed.error == "brak uprawnień" and not failed.path.exists()
    assert check_folder(str(locked)) == "brak uprawnień do zapisu w tym folderze"
    assert check_folder(str(tmp_path / "brak")) == "nie ma takiego folderu"
    assert check_folder(str(tmp_path)) is None


def test_system_clipboard_reports_whether_a_program_accepted_the_text(monkeypatch):
    monkeypatch.setattr(common, "_clipboard_commands", lambda: [["false"], ["true"]])
    assert common.system_clipboard("x") is True
    monkeypatch.setattr(common, "_clipboard_commands", lambda: [["false"], ["nie-ma-takiego-programu"]])
    assert common.system_clipboard("x") is False


def test_read_only_folder_offers_another_and_remembers_it(seeded):
    app, _, store = seeded
    locked = app_module.downloads_dir()
    locked.chmod(0o500)
    elsewhere = locked.parent / "faktury"
    elsewhere.mkdir()

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = InvoiceScreen(latest_issued(store))
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            assert "Nie udało się zapisać pliku" in str(app.screen.query_one(".dialog-text").render())
            await pilot.press("enter")  # „Wybierz folder” (domyślny przycisk)
            await pilot.pause()
            form = app.screen
            assert isinstance(form, FormScreen)
            form.query_one("#f-folder", Input).value = str(locked)
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is form and "brak uprawnień" in str(form.query_one("#form-error").render())
            form.query_one("#f-folder", Input).value = str(elsewhere)
            await pilot.press("enter")
            await pilot.pause()
            assert app.screen is screen
            assert [p.suffix for p in elsewhere.iterdir()] == [".xml"]
            assert store.setting("export_dir") == str(elsewhere)
            await screen.action_upo().wait()  # kolejny zapis trafia już do wybranego folderu
            assert len(list(elsewhere.iterdir())) == 2

    store.record_sent(
        invoice_number="FV", status="accepted", ksef_number=latest_issued(store)["ksef_number"], session_ref="S"
    )
    seeded[1].upo_by_ksef_number.return_value = b"<upo/>"
    try:
        asyncio.run(scenario())
    finally:
        locked.chmod(0o700)


def test_copy_without_confirmed_clipboard_shows_text_to_select(seeded, monkeypatch, clipboard):
    app, _, store = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = InvoiceScreen(latest_issued(store))
            await app.push_screen(screen)
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()
            assert app.screen is screen and clipboard == [screen.row["ksef_number"]]
            monkeypatch.setattr(app_module, "system_clipboard", lambda text: False)
            await pilot.press("l")
            await pilot.pause()
            fallback = app.screen
            assert isinstance(fallback, ValueScreen) and fallback.value == screen.url
            assert "Shift" in str(fallback.query_one(".dialog-text").render())
            await pilot.press("escape")
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(scenario())


def test_verification_link_opens_in_browser_or_is_shown_without_one(seeded, monkeypatch, browser):
    app, _, store = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = InvoiceScreen(latest_issued(store))
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.url.startswith("https://")
            await pilot.press("o")
            await pilot.pause()
            assert browser == [screen.url] and app.screen is screen
            monkeypatch.setattr(app_module, "can_open_browser", lambda: False)  # np. przez SSH
            await pilot.press("o")
            await pilot.pause()
            assert browser == [screen.url]
            fallback = app.screen
            assert isinstance(fallback, ValueScreen) and fallback.value == screen.url
            assert fallback.query_one("#copy", Button).has_focus

    asyncio.run(scenario())


def test_accepted_send_offers_copy_link_and_a_clickable_link(seeded, clipboard, browser):
    app, fake, _ = seeded
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    fake.send_invoice.return_value = SendResult("K-1", "S", "I", None, b"<upo/>")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = SendScreen(draft, xml)
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.outcome == "accepted" and screen.url
            copy_link = screen.query_one("#copy-link", Button)
            assert copy_link.display
            await pilot.click("#copy-link")
            assert clipboard[-1] == screen.url
            await pilot.press("o")
            assert browser == [screen.url]
            result = screen.query_one("#result")
            link_row = next(
                y for y in range(result.region.height) if "invoice/" in "".join(s.text for s in result.render_line(y))
            )
            await pilot.click("#result", offset=(2, link_row))  # klik w adres - także zawinięty
            await pilot.pause()
            assert browser == [screen.url, screen.url]

    asyncio.run(scenario())
