import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from textual.app import App
from textual.widgets import Button, Checkbox, DataTable, Input, Select, Static, TextArea

from ksefik.client import AuthError, InvoiceRejected, KsefError, RateLimitError, StillProcessing
from ksefik.invoice import Line, as_correction, build_xml, new_draft
from ksefik.ui.common import ChoiceScreen, ConfirmScreen, FormField, FormScreen
from ksefik.ui.editor import EditScreen, LineScreen
from ksefik.ui.import_screen import ImportScreen, load_files
from ksefik.ui.invoice_screens import QuickInvoiceScreen, SummaryScreen
from ksefik.ui.main_screen import MainScreen


def text(widget):
    return str(widget.content)


def test_form_required_date_custom_validation_visibility_and_values():
    async def scenario():
        app = App()
        results = []
        fields = [
            FormField("name", "Name", required=True, max_length=20),
            FormField("date", "Date", "date", required=True),
            FormField("mode", "Mode", "select", "off", [("Off", "off"), ("On", "on")]),
            FormField("secret", "Secret", "password", required=True, show_if=("mode", ["on"])),
            FormField("enabled", "Enabled", "checkbox", False),
            FormField("multi", "Permissions", "multi", ["a"], [("A", "a"), ("B", "b")]),
            FormField("notes", "Notes", "area", " padded ", hint="Hint"),
        ]
        form = FormScreen(
            "Test",
            fields,
            intro="Intro",
            wide=True,
            validate=lambda v: ("name", "reserved") if v["name"] == "bad" else None,
        )
        async with app.run_test() as pilot:
            await app.push_screen(form, results.append)
            await pilot.pause()
            assert not form.query_one("#row-secret").display
            form.action_submit()
            assert "to pole jest wymagane" in text(form.query_one("#form-error", Static))
            assert "Name" in text(form.query_one("#form-error", Static))
            form.query_one("#f-name", Input).value = "bad"
            form.query_one("#f-date", Input).value = "31.02.2026"
            form.action_submit()
            assert "DD.MM.RRRR" in text(form.query_one("#form-error", Static))
            form.query_one("#f-date", Input).value = "23.10.2026"
            form.action_submit()
            assert "reserved" in text(form.query_one("#form-error", Static))
            form.query_one("#f-name", Input).value = " Jan "
            form.query_one("#f-mode", Select).value = "on"
            await pilot.pause()
            assert form.query_one("#row-secret").display
            form.action_submit()
            assert "Secret" in text(form.query_one("#form-error", Static))
            form.query_one("#f-secret", Input).value = "secret"
            form.query_one("#f-enabled", Checkbox).value = True
            form.query_one("#f-multi").select("b")
            form.query_one("#f-notes", TextArea).load_text(" note ")
            form.action_submit()
            await pilot.pause()
            assert results == [
                {
                    "name": "Jan",
                    "date": date(2026, 10, 23),
                    "mode": "on",
                    "secret": "secret",
                    "enabled": True,
                    "multi": ["a", "b"],
                    "notes": "note",
                }
            ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "dialog,action,expected",
    [
        (lambda: ConfirmScreen("Confirm", "Message"), "_confirm", True),
        (lambda: ConfirmScreen("Confirm", "Message", danger=False), "action_cancel", False),
        (lambda: ChoiceScreen("Choose", [("A", "description", 1)]), "action_cancel", None),
        (lambda: FormScreen("Form", [FormField("x", "X")]), "action_cancel", None),
    ],
)
def test_dialog_results(dialog, action, expected):
    async def scenario():
        app = App()
        results = []
        async with app.run_test() as pilot:
            screen = dialog()
            await app.push_screen(screen, results.append)
            await pilot.pause()
            getattr(screen, action)()
            await pilot.pause()
            assert results == [expected]

    asyncio.run(scenario())


def test_line_editor_validation_catalog_selection_and_zero_correction(ui):
    app, _, _ = ui

    async def scenario():
        results = []
        async with app.run_test(size=(140, 50)) as pilot:
            screen = LineScreen(None)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.action_save()
            await pilot.pause()
            assert not results
            screen.query_one("#catalog", Select).value = app.profile.items[1].nazwa
            await pilot.pause()
            assert screen.query_one("#price", Input).value == "250,00"
            screen.query_one("#quantity", Input).value = "2,5"
            await pilot.pause()
            screen.action_save()
            await pilot.pause()
            assert results[0].quantity == Decimal("2.5") and results[0].net == Decimal("625")
            correction = LineScreen(results[0], correction=True)
            await app.push_screen(correction, results.append)
            await pilot.pause()
            correction.query_one("#quantity", Input).value = "0"
            await pilot.pause()
            correction.action_save()
            await pilot.pause()
            assert results[1].quantity == 0
            await app.push_screen(LineScreen(None), results.append)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert results[2] is None and not isinstance(app.screen, LineScreen)

    asyncio.run(scenario())


@pytest.mark.parametrize("correcting", [False, True])
def test_invoice_editor_updates_dates_numbers_lines_and_saves(ui, correcting):
    app, _, _ = ui

    async def scenario():
        results = []
        draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
        if correcting:
            draft = as_correction(draft, "", app.profile, date.today(), [])
        async with app.run_test(size=(160, 60)) as pilot:
            screen = EditScreen(draft)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.lines.append(Line("Second", unit_price=Decimal(50)))
            screen._refresh_lines(select=0)
            screen._move_line(1)
            assert screen.lines[0].name == (draft.lines[0].name if correcting else "Second")
            screen.query_one("#lines", DataTable).move_cursor(row=0)
            screen.action_remove_line()
            if correcting:
                assert screen.lines[0].quantity == 0
                screen.query_one("#corr_reason", Input).value = "Discount"
            else:
                assert len(screen.lines) == 1
                screen.query_one("#issue_date", Input).value = "02.12.2026"
                await pilot.pause()
                assert screen.query_one("#number", Input).value == "FV/1/12/2026"
                assert screen.query_one("#due_date", Input).value == "16.12.2026"
            screen.query_one("#notes", Input).value = "Notes"
            screen.action_save()
            await pilot.pause()
            assert results and results[0].review and results[0].draft.notes == "Notes"
            saved = results[0].draft
            assert saved.kind == ("KOR" if correcting else "VAT")
            if correcting:
                assert saved.correction.reason == "Discount"
                assert saved.net == -50

    asyncio.run(scenario())


def test_quick_invoice_selects_eu_defaults_and_rejects_invalid_amount(ui):
    app, _, _ = ui

    async def scenario():
        results = []
        async with app.run_test(size=(140, 50)) as pilot:
            screen = QuickInvoiceScreen()
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.query_one("#amount", Input).value = "-1"
            screen._next()
            assert results == []
            contractor = app.profile.contractors[1]
            screen.query_one("#buyer", Select).value = contractor.party.key
            screen.query_one("#amount", Input).value = "1000"
            await pilot.pause()
            screen._next()
            await pilot.pause()
            assert results[0].buyer == contractor.party
            assert results[0].currency == "EUR" and results[0].lines[0].vat_rate == "np II"

    asyncio.run(scenario())


@pytest.mark.parametrize("valid", [True, False])
def test_summary_blocks_invalid_invoices_and_renders_exact_totals(ui, valid):
    app, _, _ = ui

    async def scenario():
        draft = new_draft(app.profile, date.today(), [], net=Decimal(100) if valid else Decimal(0))
        async with app.run_test(size=(140, 50)) as pilot:
            screen = SummaryScreen(draft)
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.query_one("#send", Button).disabled is not valid
            assert bool(screen.blocking) is not valid
            assert screen.check_action("focus_send", ()) is (True if valid else None)
            if valid:
                assert "123,00" in text(screen.query_one(".to-pay", Static))
            if not valid:
                screen.action_send()
                assert app.screen is screen

    asyncio.run(scenario())


def test_main_screen_offline_search_tabs_periods_and_navigation(ui):
    app, _, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    store.upsert_metadata(
        "issued",
        [
            {
                "ksefNumber": "K",
                "invoiceNumber": "FV/1",
                "issueDate": date.today().isoformat(),
                "buyer": {"name": "Buyer"},
                "grossAmount": 123,
            }
        ],
        {"K": build_xml(draft)},
    )
    store.set_checkpoint("Subject1", datetime.now(UTC))

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = MainScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.query_one("#invoices", DataTable).row_count == 1
            screen.query_one("#search", Input).value = "nonexistent"
            await pilot.pause(0.4)
            assert screen.query_one("#invoices", DataTable).row_count == 0
            screen.action_clear_filters()
            await pilot.pause()
            assert screen.query_one("#invoices", DataTable).row_count == 1
            screen.action_next_tab()
            await pilot.pause()
            assert screen.direction == "received"
            assert screen.query_one("#invoices", DataTable).row_count == 0
            screen.action_previous_tab()
            await pilot.pause()
            assert screen.direction == "issued"
            screen.action_focus_search()
            await pilot.pause()
            assert screen.query_one("#search", Input).has_focus
            screen.action_focus_table()
            await pilot.pause()
            assert screen.query_one("#invoices", DataTable).has_focus

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["accepted", "rejected", "offline", "pending"])
def test_pending_invoice_checks_distinguish_rejections_from_transient_failures(ui, outcome):
    app, fake, store = ui
    sent = store.record_sent(invoice_number="FV/1", status="processing", session_ref="S", invoice_ref="I")
    if outcome == "accepted":
        fake.wait_for_invoice.return_value = {"ksefNumber": "K"}
    else:
        fake.wait_for_invoice.side_effect = {
            "rejected": InvoiceRejected("rejected", code=450),
            "offline": KsefError("offline"),
            "pending": StillProcessing("S", "I"),
        }[outcome]

    async def scenario():
        async with app.run_test():
            await app.reconcile_all().wait()
            row = store.db.execute("SELECT * FROM sent WHERE id = ?", (sent,)).fetchone()
            assert row["status"] == {"accepted": "accepted", "rejected": "rejected"}.get(outcome, "processing")
            if outcome == "accepted":
                assert row["ksef_number"] == "K"

    asyncio.run(scenario())


@pytest.mark.parametrize("code,expected", [(200, "accepted"), (150, "processing"), (None, "unknown")])
def test_pending_batch_invoice_is_found_in_session_results_by_file_name(ui, code, expected):
    app, fake, store = ui
    sent = store.record_sent(
        invoice_number="FV/1", status="processing", session_ref="S", file_name="invoice.xml", xml=b"<x/>"
    )
    store.record_sent(invoice_number="FV/2", status="processing", session_ref="S", file_name="other.xml", xml=b"<y/>")
    fake.session_invoices.return_value = [{"invoiceFileName": "invoice.xml", "referenceNumber": "I"}] if code else []
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}
    if code == 150:
        fake.wait_for_invoice.side_effect = StillProcessing("S", "I")

    async def scenario():
        async with app.run_test():
            await app.reconcile_all().wait()
            row = store.db.execute("SELECT * FROM sent WHERE id = ?", (sent,)).fetchone()
            assert row["status"] == expected
            assert row["invoice_ref"] == ("I" if code else None)
            fake.session_invoices.assert_awaited_once_with("S")  # jedna sesja - jedno zapytanie
            if code:
                fake.wait_for_invoice.assert_awaited_once_with("S", "I", timeout=10)
            else:
                fake.wait_for_invoice.assert_not_awaited()

    asyncio.run(scenario())


def test_import_loads_valid_invalid_missing_and_foreign_xml(ui, tmp_path):
    app, _, _ = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    (tmp_path / "valid.xml").write_bytes(build_xml(draft))
    (tmp_path / "broken.xml").write_bytes(b"<broken")
    (tmp_path / "other.xml").write_bytes(b"<other/>")
    files = load_files(str(tmp_path), "7163454949")
    assert [f.path.name for f in files] == ["broken.xml", "other.xml", "valid.xml"]
    assert not files[0].ok and not files[1].ok
    assert files[2].ok and "5438539502" in files[2].warning
    assert not load_files(str(tmp_path / "missing.xml"), "")[0].ok
    assert load_files(str(tmp_path / "absent*.xml"), "") == []

    async def scenario():
        async with app.run_test() as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#import-path", Input).value = str(tmp_path)
            await screen._load().wait()
            assert screen.query_one("#files", DataTable).row_count == 3
            assert not screen.query_one("#send", Button).disabled
            screen.sending = True
            screen._refresh_table()
            assert screen.query_one("#send", Button).disabled

    asyncio.run(scenario())


def test_line_editor_keeps_fractional_price_when_saving_without_edits(ui):
    app, _, _ = ui

    async def scenario():
        line = Line("Fractional price", Decimal(3), unit_price=Decimal("0.33333333"))
        results = []
        async with app.run_test(size=(140, 50)) as pilot:
            screen = LineScreen(line)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            screen.action_save()
            await pilot.pause()
            assert results == [line]

    asyncio.run(scenario())


def test_send_interrupted_by_closing_the_app_is_found_by_hash_and_listed(ui):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    sent = store.record_sent(invoice_number=draft.number, status="sending", session_ref="S", xml=xml)
    fake.session_invoices.return_value = [{"referenceNumber": "I", "invoiceHash": store.sent_entry(sent)["xml_hash"]}]
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}

    async def scenario():
        async with app.run_test():
            await app.reconcile_all().wait()
            row = store.sent_entry(sent)
            assert (row["status"], row["ksef_number"], row["invoice_ref"]) == ("accepted", "K", "I")
            assert store.get("K", "issued")["xml"] == xml  # od razu na liście, bez synchronizacji

    asyncio.run(scenario())


@pytest.mark.parametrize("age,expected", [(timedelta(hours=1), "unknown"), (timedelta(days=2), "not_submitted")])
def test_send_missing_from_its_session_is_released_only_after_a_day(ui, age, expected):
    app, fake, store = ui
    created = (datetime.now(UTC) - age).isoformat()
    sent = store.record_sent(invoice_number="FV/1", status="unknown", session_ref="S", xml=b"<x/>", created=created)
    fake.session_invoices.return_value = []

    async def scenario():
        async with app.run_test():
            await app.reconcile_all().wait()
            assert store.sent_entry(sent)["status"] == expected
            assert ("FV/1" in store.reserved_numbers()) is (expected == "unknown")

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [RateLimitError("wait", retry_after=5), AuthError("expired", http_status=401)])
def test_pending_checks_stop_on_rate_limit_or_expired_login(ui, error):
    app, fake, store = ui
    for n in (1, 2):
        store.record_sent(invoice_number=f"FV/{n}", status="processing", session_ref="S", invoice_ref=f"I{n}")
    fake.wait_for_invoice.side_effect = error

    async def scenario():
        async with app.run_test():
            await app.reconcile_all().wait()
            assert fake.wait_for_invoice.await_count == 1
            assert len(store.unresolved_sent()) == 2

    asyncio.run(scenario())
