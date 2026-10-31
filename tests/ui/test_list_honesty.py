"""Lista faktur mówi prawdę: ile jest naprawdę, co pokazano, dlaczego jest pusta i co z błędnymi datami."""

import asyncio

from textual.widgets import Input

from ksefik.store import Filters
from ksefik.submissions import metadata_from_xml
from ksefik.ui import main_screen
from ksefik.ui.main_screen import MainScreen


def status(main) -> str:
    return str(main.query_one("#status").render())


async def open_main(app, pilot):
    main = MainScreen()
    await app.push_screen(main)
    await pilot.pause()
    return main


def test_truncated_list_says_how_many_are_shown_and_loads_more(seeded, monkeypatch):
    app, _, store = seeded
    monkeypatch.setattr(main_screen, "PAGE", 5)

    async def scenario():
        async with app.run_test(size=(160, 50)) as pilot:
            main = await open_main(app, pilot)
            await pilot.press("f")  # wszystkie okresy
            await pilot.pause()
            total = store.find_invoices(Filters(direction="issued")).total
            assert main.query_one("#invoices").row_count == 5
            assert f"pokazano 5 z {total}" in status(main) and f"{total} faktur" in status(main)
            # Sumy obejmują wszystkie pasujące faktury, nie tylko pokazane.
            pln = store.find_invoices(Filters(direction="issued")).pln[2]
            assert main_screen.fmt_amount(pln) in status(main)
            await pilot.press("m")
            await pilot.pause()
            assert main.query_one("#invoices").row_count == 10

    asyncio.run(scenario())


def test_invoice_in_two_roles_is_counted_once_in_all(seeded):
    app, _, store = seeded
    issued = store.db.execute("SELECT * FROM invoices WHERE direction = 'issued' LIMIT 1").fetchone()
    meta = metadata_from_xml(issued["xml"], issued["ksef_number"])
    store.upsert_metadata("received", [meta], {issued["ksef_number"]: issued["xml"]})  # np. faktura dla siebie

    async def scenario():
        async with app.run_test(size=(160, 50)) as pilot:
            main = await open_main(app, pilot)
            await pilot.press("f")
            main.query_one("#direction-tabs").active = "all"
            await pilot.pause()
            found = store.find_invoices(Filters())
            assert found.total == found.unique + 1
            assert f"{found.unique} faktur" in status(main) and "liczona raz" in status(main)

    asyncio.run(scenario())


def test_reversed_or_malformed_dates_explain_themselves_and_keep_previous_rows(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(160, 50)) as pilot:
            main = await open_main(app, pilot)
            rows = main.query_one("#invoices").row_count
            date_from = main.query_one("#date-from", Input)
            date_from.focus()
            date_from.value = "01.12"  # w trakcie pisania: jeszcze bez błędu, lista bez zmian
            await pilot.pause()
            error = main.query_one("#filter-error")
            assert not error.display and main.query_one("#invoices").row_count == rows
            assert "(poprzednie wyniki)" in status(main)
            await pilot.press("tab")  # wyjście z pola - teraz błąd formatu
            await pilot.pause()
            assert error.display and "DD.MM.RRRR" in str(error.render())
            date_from.value = "31.12.2026"
            main.query_one("#date-to", Input).value = "01.01.2026"
            await pilot.pause()
            assert "nie może być późniejsza" in str(error.render())
            assert main.query_one("#invoices").row_count == rows
            await pilot.press("escape", "f")  # wyczyść filtry
            await pilot.pause()
            assert not error.display and "(poprzednie wyniki)" not in status(main)

    asyncio.run(scenario())


def test_empty_list_says_the_filters_hide_everything(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            main = await open_main(app, pilot)
            await pilot.press("slash", *"nieistniejacy-kontrahent")
            await pilot.pause(0.3)
            text = str(main.query_one("#empty").render())
            assert "Brak wyników dla tych filtrów" in text and "wyczyścić filtry" in text

    asyncio.run(scenario())


def test_empty_list_says_nothing_was_downloaded_yet(empty):
    app, _, _ = empty

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            app.work_locally()
            main = await open_main(app, pilot)
            assert "Nie pobrano jeszcze faktur" in str(main.query_one("#empty").render())

    asyncio.run(scenario())


def test_text_search_mentions_invoices_searched_only_by_metadata(seeded):
    app, _, store = seeded
    store.db.execute("UPDATE invoices SET xml = NULL WHERE direction = 'issued' AND rowid % 2 = 0")
    store.db.commit()

    async def scenario():
        async with app.run_test(size=(160, 50)) as pilot:
            main = await open_main(app, pilot)
            await pilot.press("slash", *"uslugi")  # bez polskich znaków
            await pilot.pause(0.3)
            assert "bez pobranej treści" in status(main)

    asyncio.run(scenario())


def test_fetch_by_number_uses_the_local_copy_without_asking_ksef(seeded):
    from ksefik.ui.import_screen import fetch_invoice
    from ksefik.ui.invoice_screens import InvoiceScreen

    app, fake, store = seeded
    known = store.db.execute("SELECT ksef_number FROM invoices WHERE direction = 'received' LIMIT 1").fetchone()[0]

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await open_main(app, pilot)
            fetch_invoice(app, known)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert isinstance(app.screen, InvoiceScreen) and app.screen.row["ksef_number"] == known
            fake.get_invoice.assert_not_awaited()

    asyncio.run(scenario())
