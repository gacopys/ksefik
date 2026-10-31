"""Wysyłka plików XML: każdy plik ma wpis w historii przed połączeniem, nic nie jest wysyłane dwa razy."""

import asyncio
from datetime import date
from decimal import Decimal

from textual.widgets import Button, Input, Select

from ksefik.client import NotSubmitted, RateLimitError
from ksefik.invoice import build_xml, new_draft
from ksefik.store import Store
from ksefik.ui.import_screen import ImportScreen
from ui.harness import HarnessApp, fake_client, make_profile


def write_invoices(profile, folder, names):
    folder.mkdir(parents=True, exist_ok=True)
    draft = new_draft(profile, date.today(), [], net=Decimal(100))
    for n, name in enumerate(names, 1):
        (folder / name).write_bytes(build_xml(draft.with_changes(number=f"IMP/{n}/2026")))
    return folder


async def open_import(app, pilot, folder):
    screen = ImportScreen()
    await app.push_screen(screen)
    await pilot.pause()
    screen.query_one("#import-path", Input).value = str(folder)
    await screen._load().wait()
    await pilot.pause()
    return screen


def test_app_closed_mid_import_never_offers_uploaded_files_again(isolated):
    profile = make_profile(isolated)
    folder = write_invoices(profile, isolated / "faktury", ["a.xml", "b.xml", "c.xml"])
    db = isolated / "ksef.db"

    store = Store(db)
    app = HarnessApp(profile, profile_path=profile.path, store=store)
    fake = fake_client()
    app.client = fake
    hang = asyncio.Event()

    async def uploading(payload, progress, *, offline, on_session, on_refs):
        on_session("S")
        on_refs(0, "I-a")  # pierwszy plik jest już w KSeF, przy drugim aplikacja zostaje zamknięta
        await hang.wait()

    fake.send_invoices_online.side_effect = uploading

    async def first_run():
        async with app.run_test(size=(80, 24)) as pilot:
            app.get_client = lambda: _value(fake)
            screen = await open_import(app, pilot, folder)
            assert [f.ready for f in screen.files] == [True, True, True]
            screen.send()
            await pilot.pause()
            assert screen.sending
            for selector in ("#import-path", "#load", "#mode", "#offline"):
                assert screen.query_one(selector).disabled, f"{selector} nie jest zablokowane w trakcie wysyłki"
            states = {r["file_name"]: r["status"] for r in store.import_history()}
            assert states == {"a.xml": "processing", "b.xml": "sending", "c.xml": "sending"}

    asyncio.run(first_run())
    store.close()

    store = Store(db)
    app = HarnessApp(profile, profile_path=profile.path, store=store)
    app.client = fake_client()

    async def second_run():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            # Bez wczytanych plików ekran pokazuje poprzednie wysyłki z historii.
            assert "Poprzednie wysyłki plików" in str(screen.query_one("#table-title").render())
            assert len(screen.files) == 3 and screen.query_one("#recheck", Button).display
            screen.query_one("#import-path", Input).value = str(folder)
            await screen._load().wait()
            await pilot.pause()
            assert not any(f.ready for f in screen.files)
            assert screen.query_one("#send", Button).disabled

    asyncio.run(second_run())
    store.close()


def test_rate_limit_mid_import_keeps_uploaded_file_and_frees_the_rest(seeded, isolated):
    app, fake, store = seeded
    folder = write_invoices(app.profile, isolated / "faktury", ["a.xml", "b.xml"])
    limit = NotSubmitted.wrap(RateLimitError("Przekroczono limit zapytań."))
    fake.send_invoices_online.return_value = ("S", [("a.xml", "I-a", None), ("b.xml", None, limit)])
    fake.wait_for_invoice.return_value = {"ksefNumber": "K-a"}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await open_import(app, pilot, folder)
            await screen.send().wait()
            a, b = screen.files
            assert (a.state, a.ksef_number, b.state) == ("accepted", "K-a", "not_submitted")
            assert not a.ready and b.ready  # nie wysłany plik można wysłać ponownie
            assert store.find("K-a") is not None

    asyncio.run(scenario())


def test_uppercase_extension_is_loaded_and_duplicate_names_block_batch(seeded, isolated):
    app, fake, store = seeded
    write_invoices(app.profile, isolated / "x" / "one", ["FAKTURA.XML"])
    write_invoices(app.profile, isolated / "x" / "two", ["FAKTURA.XML"])

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await open_import(app, pilot, isolated / "x" / "one")
            assert [f.path.name for f in screen.files] == ["FAKTURA.XML"] and screen.files[0].ready
            screen.query_one("#import-path", Input).value = str(isolated / "x" / "*" / "*.XML")
            await screen._load().wait()
            screen.query_one("#mode", Select).value = "batch"
            await pilot.pause()
            await screen.send().wait()
            fake.send_batch.assert_not_awaited()
            assert store.import_history() == []  # nic nie zapisano - wysyłka nie ruszyła

    asyncio.run(scenario())


def test_recheck_resolves_files_waiting_for_a_result(seeded, isolated):
    app, fake, store = seeded
    folder = write_invoices(app.profile, isolated / "faktury", ["a.xml"])
    xml = (folder / "a.xml").read_bytes()
    store.record_sent(
        invoice_number="IMP/1/2026", status="processing", xml=xml, session_ref="S", invoice_ref="I", source="import"
    )
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = await open_import(app, pilot, folder)
            assert screen.files[0].unresolved and not screen.files[0].ready
            assert screen.query_one("#recheck", Button).display
            await screen.recheck().wait()
            assert screen.files[0].state == "accepted" and store.find("K") is not None
            assert not screen.query_one("#recheck", Button).display

    asyncio.run(scenario())


async def _value(value):
    return value
