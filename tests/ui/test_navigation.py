"""Nawigacja: Wysyłki i UPO z lokalnej historii (offline), menu Akcje, pomoc F1, synonimy w palecie
i zapis szkicu przed poleceniem z palety (UX-27)."""

import asyncio
from datetime import date
from decimal import Decimal

from textual.widgets import DataTable, OptionList

from ksefik.client import InvoiceRejected
from ksefik.invoice import build_xml, new_draft
from ksefik.ui.account import AccountScreen, SessionsScreen
from ksefik.ui.app import KsefCommands
from ksefik.ui.common import ChoiceScreen, HelpScreen
from ksefik.ui.editor import EditScreen
from ksefik.ui.invoice_screens import InvoiceScreen
from ksefik.ui.main_screen import MainScreen
from ksefik.ui.sent_screen import SentScreen


def ledger(app, store):
    """Trzy wysyłki: przyjęta (faktura w bazie), czekająca na wynik i odrzucona (ze szkicu)."""
    accepted = store.db.execute("SELECT * FROM invoices WHERE direction = 'issued' ORDER BY issue_date DESC").fetchone()
    store.record_sent(
        invoice_number=accepted["invoice_number"],
        status="accepted",
        ksef_number=accepted["ksef_number"],
        session_ref="S-1",
        invoice_ref="I-1",
        upo=b"<upo/>",
        upo_state="ready",
    )
    draft = new_draft(app.profile, date.today(), store.reserved_numbers(), net=Decimal(100))
    waiting = store.record_sent(
        invoice_number="FV/77/10/2026", status="processing", xml=build_xml(draft), session_ref="S-2", invoice_ref="I-2"
    )
    draft_id = app.save_draft(draft.with_changes(number="FV/78/10/2026"))
    store.record_sent(
        invoice_number="FV/78/10/2026", status="rejected", message="Błędny NIP nabywcy.", draft_id=draft_id
    )
    return accepted, waiting, draft_id


def test_sent_screen_works_offline_and_opens_what_was_sent(seeded):
    app, fake, store = seeded
    accepted, _, draft_id = ledger(app, store)
    app.work_locally()

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            await pilot.press("w")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, SentScreen)
            table = screen.query_one("#list", DataTable)
            assert table.row_count == 3
            assert "czeka na wynik" in str(table.get_row_at(1)[2])
            table.move_cursor(row=2)  # przyjęta (najstarsza)
            await pilot.pause()
            assert screen.check_action("upo", ()) and "podgląd faktury" in str(
                screen.query_one("#list-details").render()
            )
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, InvoiceScreen) and app.screen.row["ksef_number"] == accepted["ksef_number"]
            await pilot.press("escape")
            await pilot.pause()
            table.move_cursor(row=0)  # odrzucona - Enter otwiera szkic do poprawy
            await pilot.pause()
            assert not screen.check_action("upo", ())
            assert "Błędny NIP" in str(screen.query_one("#list-details").render())
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, EditScreen) or app.screen.draft_id == draft_id
            fake.assert_not_called()

    asyncio.run(scenario())


def test_check_status_resolves_waiting_sends(seeded):
    app, fake, store = seeded
    _, waiting, _ = ledger(app, store)
    fake.wait_for_invoice.side_effect = InvoiceRejected("KSeF odrzucił fakturę: duplikat")

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = SentScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.check_action("check", ())
            await pilot.press("s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert store.sent_entry(waiting)["status"] == "rejected"
            assert not screen.check_action("check", ())
            assert "Wszystkie wysyłki mają już wynik" in str(screen.query_one("#op-status").render())

    asyncio.run(scenario())


def test_sent_screen_downloads_stored_upo_and_links_to_sessions(seeded, isolated):
    app, _, store = seeded
    ledger(app, store)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = SentScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#list", DataTable).move_cursor(row=2)
            await pilot.press("u")
            await app.workers.wait_for_complete()
            assert [p.read_bytes() for p in (isolated / "Pobrane").glob("*UPO.xml")] == [b"<upo/>"]
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(app.screen, SessionsScreen)

    asyncio.run(scenario())


def test_empty_sent_screen_explains_itself(empty):
    app, _, _ = empty

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            screen = SentScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert "Nie wysłano jeszcze żadnej faktury" in str(screen.query_one("#list-empty").render())

    asyncio.run(scenario())


def test_actions_menu_lists_everything_and_runs_the_choice(seeded):
    app, _, store = seeded
    ledger(app, store)

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            main = MainScreen()
            await app.push_screen(main)
            await pilot.pause()
            assert "1 wysyłka czeka na wynik" in str(main.query_one("#sync-status").render())
            await pilot.click("#actions")
            await pilot.pause()
            menu = app.screen
            assert isinstance(menu, ChoiceScreen)
            labels = [label for label, _, _ in menu.choices]
            assert labels[0].startswith("Nowa faktura") and any(label.startswith("Szkice") for label in labels)
            sent = next(n for n, label in enumerate(labels) if label.startswith("Wysyłki"))
            assert "Czeka na wynik z KSeF: 1" in menu.choices[sent][1]
            options = menu.query_one(OptionList)
            options.highlighted = sent
            options.action_select()
            await pilot.pause()
            assert isinstance(app.screen, SentScreen)

    asyncio.run(scenario())


def test_f1_help_lists_all_shortcuts_including_hidden_ones(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            help_screen = app.screen
            assert isinstance(help_screen, HelpScreen)
            text = str(help_screen.query_one("#help-keys").render())
            for shown in ("Nowa faktura", "Akcje", "Wysyłki"):
                assert shown in text
            assert "Pobierz po nr KSeF" in text and "Wyczyść filtry" in text  # ukryte w stopce
            assert "^p" in text and "wszystkie polecenia" in text
            assert "Cursor" not in text and "Quit" not in text  # bez angielskich skrótów wbudowanych w Textual
            assert "1 2 3 4 5" in text
            assert "Lista faktur z lokalnej bazy" in str(help_screen.query_one(".dialog-text").render())
            await pilot.press("f1")
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)

    asyncio.run(scenario())


def test_account_menu_is_administration_only(seeded):
    app, _, _ = seeded

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(AccountScreen())
            await pilot.pause()
            prompts = [str(o.prompt) for o in app.screen.query_one(OptionList)._options if o]
            assert not any("Sesje i UPO" in p or "Sesje w KSeF" in p for p in prompts)
            assert any("Tokeny KSeF" in p for p in prompts)

    asyncio.run(scenario())


def test_palette_is_polish_and_finds_commands_by_synonyms(seeded):
    app, _, _ = seeded

    async def search(query: str) -> list[str]:
        provider = KsefCommands(app.screen)
        return [str(hit.text) for hit in [h async for h in provider.search(query)]]

    async def scenario():
        async with app.run_test(size=(80, 24)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            titles = [c.title for c in app.get_system_commands(app.screen)]
            assert {"Motyw", "Zakończ", "Pomoc i skróty klawiszowe"} <= set(titles)
            assert not {"Theme", "Quit", "Keys", "Screenshot"} & set(titles)
            assert "Wysyłki i UPO" in await search("potwierdzenie")
            assert "Edytuj profil" in await search("ustawienia")
            assert "Sesje w KSeF" in await search("upo sesji")
            assert app.screen.query_one("Footer")  # stopka ma „Polecenia” zamiast „palette”
            assert any(b.description == "Polecenia" for _, b in app._bindings)

    asyncio.run(scenario())


def test_palette_command_saves_the_open_editor_first(seeded):
    app, _, store = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await app.push_screen(MainScreen())
            await pilot.pause()
            app.open_editor(new_draft(app.profile, date.today(), store.reserved_numbers(), net=Decimal(100)))
            await pilot.pause()
            editor = app.screen
            assert isinstance(editor, EditScreen)
            editor.query_one("#number").value = "FV/PALETA/1"
            command = next(c for c in app.get_system_commands(editor) if c.title == "Wysyłki i UPO")
            command.callback()  # zanim autozapis zdążył zadziałać
            await pilot.pause()
            assert isinstance(app.screen, SentScreen)
            assert [d["number"] for d in store.drafts()] == ["FV/PALETA/1"]

    asyncio.run(scenario())
