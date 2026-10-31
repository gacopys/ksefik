"""Profil i pozostałe ścieżki: zapis kontrahenta i pozycji, edycja profilu w edytorze, zmiana tokena,
dokończenie faktur „w przetwarzaniu” po ponownym uruchomieniu, filtry listy."""

import asyncio
import contextlib
import stat

from helpers import (
    draft_for,
    running_app,
    set_value,
    wait_notification,
    wait_screen,
    wait_until,
    write_profile,
)
from ksef_env import random_nip

from ksefik.invoice import build_xml
from ksefik.profile import load_profile, prepare_data_dir
from ksefik.store import Store
from ksefik.ui.app import KsefApp
from ksefik.ui.editor import EditScreen, LineScreen
from ksefik.ui.invoice_screens import QuickInvoiceScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.setup_screen import TokenScreen


async def palette(pilot, command: str) -> None:
    await pilot.press("ctrl+p")
    await pilot.pause(0.3)
    await pilot.press(*command)
    await pilot.pause(0.5)
    await pilot.press("enter")
    await pilot.pause(0.3)


def editor_script(folder, name: str, block: str):
    """Skrypt udający edytor: dopisuje fragment do pliku podanego jako argument."""
    script = folder / name
    script.write_text(f"#!/bin/sh\ncat >> \"$1\" <<'EOF'\n{block}\nEOF\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_save_contractor_item_and_edit_profile(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    buyer_nip = random_nip()
    # Zawieszenie terminala nie działa w trybie testowym - edytor uruchamiamy bez niego (ale naprawdę).
    monkeypatch.setattr(KsefApp, "suspend", lambda self: contextlib.nullcontext())
    good = editor_script(isolated_app, "edytor.sh", '\n[[pozycje]]\nnazwa = "Dopisana w edytorze"\nstawka_vat = "8"')
    bad = editor_script(isolated_app, "zly-edytor.sh", '\n[[pozycje]]\nnazwa = "Z literówką"\nstawka = "8"')

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)

            # Nowy nabywca i nowa pozycja zapisane z edycji faktury.
            await pilot.press("n")
            await wait_screen(pilot, QuickInvoiceScreen)
            await pilot.press("ctrl+o")
            editor = await wait_screen(pilot, EditScreen)
            set_value(editor, "#buyer_id_type", "nip")
            set_value(editor, "#buyer_id", buyer_nip)
            set_value(editor, "#buyer_name", "Nowy Klient Sp. z o.o.")
            set_value(editor, "#buyer_l1", "ul. Nowa 1")
            set_value(editor, "#buyer_l2", "00-100 Warszawa")
            await pilot.pause(0.2)
            await pilot.press("ctrl+t")
            await wait_notification(pilot, "Zapisano kontrahenta")
            await pilot.press("ctrl+n")
            line = await wait_screen(pilot, LineScreen)
            set_value(line, "#name", "Pozycja z katalogu e2e")
            set_value(line, "#price", "99,90")
            set_value(line, "#rate", "8")
            await pilot.click("#to-catalog")
            await wait_notification(pilot, "Dodano „Pozycja z katalogu e2e”")
            await pilot.press("escape")
            await pilot.press("escape")
            await wait_screen(pilot, MainScreen)
            await pilot.press("n")
            quick = await wait_screen(pilot, QuickInvoiceScreen)
            assert any(p.nip == buyer_nip for p in quick.buyers.values())
            assert "Pozycja z katalogu e2e" in quick.items
            await pilot.press("escape")

            # „Edytuj profil”: poprawna zmiana jest wczytywana od razu, błędna - odrzucana z opisem.
            monkeypatch.setenv("VISUAL", str(good))
            await palette(pilot, "Edytuj profil")
            await wait_notification(pilot, "Wczytano zmiany w profilu")
            assert app.profile.item("Dopisana w edytorze") is not None
            app._notifications.clear()
            monkeypatch.setenv("VISUAL", str(bad))
            await palette(pilot, "Edytuj profil")
            message = await wait_until(
                pilot,
                lambda: next((n for n in app._notifications if "stawka" in n.message), None),
                "opis błędu profilu",
            )
            assert "nieznane pole „stawka”" in message.message
            assert "Czy chodziło o „stawka_vat”" in message.message
            assert app.profile.item("Dopisana w edytorze") is not None  # poprzedni profil zostaje

    asyncio.run(scenario())
    text = path.read_text(encoding="utf-8")
    assert buyer_nip in text and "Pozycja z katalogu e2e" in text


def test_change_token_from_palette(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            old = app.client
            await palette(pilot, "Zmień token")
            screen = await wait_screen(pilot, TokenScreen)
            set_value(screen, "#token", company.token)
            await pilot.press("enter")
            await wait_notification(pilot, "Połączono z KSeF")
            assert app.client is not old and app.client.auth_reference

    asyncio.run(scenario())


def test_pending_invoice_is_resolved_on_start(isolated_app, company, partner, monkeypatch):
    """Faktura, której KSeF nie zdążył przetworzyć podczas wysyłki, jest sprawdzana przy następnym starcie."""
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    xml = build_xml(draft_for(company, partner.nip, "PENDING/1"))

    async def send_without_waiting():
        async with company.client(token=True) as client:
            from ksefik.crypto import SymmetricKey

            key = SymmetricKey.generate()
            session = (await client.open_online_session(key))["referenceNumber"]
            invoice = await client.send_to_session(session, key, xml)
            await client.close_online_session(session)
            return session, invoice

    session_ref, invoice_ref = asyncio.run(send_without_waiting())
    store = Store(prepare_data_dir(load_profile(path)) / "ksef.db")
    sent_id = store.record_sent(
        invoice_number="PENDING/1", status="processing", session_ref=session_ref, invoice_ref=invoice_ref, xml=xml
    )
    store.close()

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            message = await wait_notification(pilot, "Faktura PENDING/1 została przyjęta", timeout=180)
            row = app.store.db.execute("SELECT status, ksef_number FROM sent WHERE id = ?", (sent_id,)).fetchone()
            assert row["status"] == "accepted" and row["ksef_number"] in message

    asyncio.run(scenario())


def test_list_filters(isolated_app, company, partner, monkeypatch):
    """Filtry listy (okres, daty, kontrahent, czyszczenie) na fakturach z lokalnej bazy."""
    from datetime import date, timedelta

    from ksefik.submissions import metadata_from_xml

    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    profile = load_profile(path)
    store = Store(prepare_data_dir(profile) / "ksef.db")
    other = random_nip()
    today = date.today()
    for n, (buyer, day) in enumerate([(partner.nip, today), (other, today - timedelta(days=400))]):
        draft = draft_for(company, buyer, f"FILTR/{n}", issue_date=day, sale_date=day)
        meta = metadata_from_xml(build_xml(draft), f"{company.nip}-{day:%Y%m%d}-ABCDEF00000{n}-0{n}")
        store.upsert_metadata("issued", [meta], {meta["ksefNumber"]: build_xml(draft)})
    store.close()

    async def scenario():
        async with running_app(path) as (app, pilot):
            main = await wait_screen(pilot, MainScreen)
            numbers = lambda: [r.invoice_number for r in main._rows]  # noqa: E731
            assert numbers() == ["FILTR/0"]  # domyślnie: ten rok
            set_value(main, "#period", "all")
            await wait_until(pilot, lambda: numbers() == ["FILTR/0", "FILTR/1"], "wszystkie okresy")
            set_value(main, "#counterparty", other)
            await wait_until(pilot, lambda: numbers() == ["FILTR/1"], "filtr kontrahenta")
            await pilot.press("f")
            await wait_until(pilot, lambda: numbers() == ["FILTR/0", "FILTR/1"], "wyczyszczone filtry")
            old = today - timedelta(days=400)
            set_value(main, "#date-from", old.strftime("%d.%m.%Y"))
            set_value(main, "#date-to", old.strftime("%d.%m.%Y"))
            await wait_until(pilot, lambda: numbers() == ["FILTR/1"], "własny zakres dat")
            assert main.query_one("#period").value == "custom"

    asyncio.run(scenario())
