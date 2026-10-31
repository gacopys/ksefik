"""Wysyłka gotowych plików XML: walidacja, sesja interaktywna, paczka wsadowa (także w trybie offline)."""

import asyncio

from helpers import draft_for, running_app, set_value, wait_screen, wait_until, write_profile

from ksefik.invoice import build_xml
from ksefik.ui.import_screen import ImportScreen
from ksefik.ui.main_screen import MainScreen


def write_files(folder, company, partner, prefix: str, count: int, *, broken: bool = False) -> dict[str, bytes]:
    folder.mkdir(parents=True)
    files = {}
    for n in range(1, count + 1):
        xml = build_xml(draft_for(company, partner.nip, f"{prefix}/{n}", amount=f"{n * 100}.00"))
        (folder / f"{prefix.lower()}-{n}.xml").write_bytes(xml)
        files[f"{prefix}/{n}"] = xml
    if broken:
        (folder / "zepsuta.xml").write_text("<Faktura>niepełna", encoding="utf-8")
        (folder / "inny-schemat.xml").write_text("<?xml version='1.0'?><Inny/>", encoding="utf-8")
    return files


async def send_folder(pilot, screen, folder, *, mode: str, offline: bool = False) -> None:
    set_value(screen, "#import-path", str(folder))
    screen.query_one("#import-path").focus()  # po wysyłce fokus jest na liście wyników
    await pilot.press("enter")
    await pilot.pause(0.3)
    await wait_until(pilot, lambda: not screen.loading, "sprawdzenie plików", timeout=60)
    set_value(screen, "#mode", mode)
    set_value(screen, "#offline", offline)
    await pilot.click("#send")
    await wait_until(pilot, lambda: screen.sending, "rozpoczęcie wysyłki", timeout=30)
    await wait_until(pilot, lambda: not screen.sending, "koniec wysyłki", timeout=600)


def test_import_online_and_batch(isolated_app, company, partner, monkeypatch):
    path = write_profile(isolated_app / "profile.toml", company, partner)
    monkeypatch.setenv("KSEF_TOKEN", company.token)
    online = write_files(isolated_app / "online", company, partner, "IMP", 2, broken=True)
    batch = write_files(isolated_app / "wsad", company, partner, "WSAD", 3)

    async def scenario():
        async with running_app(path) as (app, pilot):
            await wait_screen(pilot, MainScreen)
            await pilot.press("i")
            screen = await wait_screen(pilot, ImportScreen)

            # Sesja interaktywna: dwie poprawne faktury, dwa błędne pliki pominięte po walidacji XSD.
            await send_folder(pilot, screen, isolated_app / "online", mode="online")
            by_name = {f.path.name: f for f in screen.files}
            assert not by_name["zepsuta.xml"].ok and "niepoprawny XML" in by_name["zepsuta.xml"].errors[0]
            assert not by_name["inny-schemat.xml"].ok
            accepted = {f.view.number: f.ksef_number for f in screen.files if f.ksef_number}
            assert set(accepted) == set(online), [f.status for f in screen.files]
            assert "Przyjęto 2 z 2" in str(screen.query_one("#import-status").render())

            # Paczka wsadowa (ZIP szyfrowany AES) z deklaracją trybu offline.
            await send_folder(pilot, screen, isolated_app / "wsad", mode="batch", offline=True)
            accepted_batch = {f.view.number: f.ksef_number for f in screen.files if f.ksef_number}
            assert set(accepted_batch) == set(batch), [f.status for f in screen.files]

            issued = {row["invoice_number"] for row in app.store.db.execute("SELECT invoice_number FROM invoices")}
            assert issued == set(online) | set(batch)
            records = app.store.db.execute("SELECT ksef_number, session_ref FROM sent WHERE status = 'accepted'")
            assert len([r for r in records if r[1]]) == 5
            return accepted | accepted_batch

    numbers = asyncio.run(scenario())

    async def verify():
        async with company.client(token=True) as client:
            for number, ksef in numbers.items():
                expected = online.get(number) or batch[number]
                assert await client.get_invoice(ksef) == expected

    asyncio.run(verify())
