import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import Mock

import pytest
from textual.widgets import Button, Select

from ksefik.client import (
    InvoiceRejected,
    KsefError,
    NotSubmitted,
    OutcomeUnknown,
    RateLimitError,
    SendResult,
    StillProcessing,
)
from ksefik.fa_parser import parse_invoice
from ksefik.invoice import build_xml, new_draft
from ksefik.ui.import_screen import ImportScreen, XmlFile
from ksefik.ui.invoice_screens import InvoiceScreen, SendScreen


@pytest.mark.parametrize("outcome", ["accepted", "pending", "unknown", "rejected", "offline", "no-client"])
def test_send_screen_records_outcomes_and_saves_accepted_documents(ui, outcome, monkeypatch, clipboard):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    fake.send_invoice.return_value = SendResult("K", "S", "I", datetime.now(UTC), b"<upo/>")
    errors = {
        "pending": StillProcessing("S", "I"),
        "unknown": OutcomeUnknown("S"),
        "rejected": InvoiceRejected("duplicate", code=440, extensions={"originalKsefNumber": "original"}),
        "offline": NotSubmitted("offline"),
    }
    if outcome in errors:
        fake.send_invoice.side_effect = errors[outcome]
    if outcome == "no-client":
        app.get_client.return_value = None

    async def scenario():
        results = []
        async with app.run_test(size=(140, 50)) as pilot:
            screen = SendScreen(draft, xml)
            await app.push_screen(screen, results.append)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.finished and not app.active_sends
            record = store.db.execute("SELECT * FROM sent").fetchone()
            assert (record["env"], record["seller_nip"], record["invoice_kind"]) == ("prod", draft.seller.nip, "VAT")
            result = str(screen.query_one("#result").render())
            if outcome == "accepted":
                assert screen.success and screen.ksef_number == "K"
                assert record["status"] == "accepted" and record["upo"] == b"<upo/>"
                assert record["upo_state"] == "ready"
                assert store.get("K", "issued")["xml"] == xml
                folder = app.profile.data_dir / "wystawione"
                assert len(list(folder.glob("*.xml"))) == 2
                screen._copy()
                assert clipboard == ["K"]
                screen._done()
                await pilot.pause()
                assert results == ["K"]
            elif outcome in ("pending", "unknown"):
                expected = "processing" if outcome == "pending" else "unknown"
                assert record["status"] == screen.outcome == expected
                assert record["session_ref"] == "S"
                assert screen.query_one("#recheck", Button).display
                assert "Nie wysyłaj jej ponownie" in result
            elif outcome == "rejected":
                assert record["status"] == "rejected" and "Szkic i szczegóły wysyłki są zachowane" in result
                assert screen.query_one("#fix", Button).display
            else:
                # Błąd przed przesłaniem faktury to „nie wysłano” - nigdy „odrzucona”.
                assert record["status"] == screen.outcome == "not_submitted"
                assert "Nie wysłano faktury" in result and store.count() == 0
                assert screen.query_one("#back", Button).display

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["accepted", "pending", "offline", "rejected"])
def test_send_recheck_preserves_pending_state_on_transient_failure(ui, outcome):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    fake.send_invoice.side_effect = StillProcessing("S", "I")
    fake.fetch_upo.return_value = b"<upo/>"
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}
    if outcome != "accepted":
        fake.wait_for_invoice.side_effect = {
            "pending": StillProcessing("S", "I"),
            "offline": KsefError("offline"),
            "rejected": InvoiceRejected("invalid", code=450),
        }[outcome]

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = SendScreen(draft, build_xml(draft))
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await screen.recheck().wait()
            row = store.db.execute("SELECT * FROM sent").fetchone()
            assert row["status"] == {"accepted": "accepted", "rejected": "rejected"}.get(outcome, "processing")
            assert screen.success is (outcome == "accepted")
            if outcome == "offline":
                assert screen.query_one("#recheck", Button).display

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("online", "accepted"),
        ("online", "pending"),
        ("online", "rejected"),
        ("online", "rate-limit"),
        ("batch", "accepted"),
        ("batch", "pending"),
        ("batch", "rejected"),
    ],
)
def test_import_send_records_results_and_prevents_resending_pending_invoice(ui, tmp_path, mode, outcome):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    info = {"referenceNumber": "I", "status": {"code": 200}, "ksefNumber": "K", "invoiceFileName": "invoice.xml"}
    if outcome == "rejected":
        info = {
            "referenceNumber": "I",
            "status": {"code": 450, "description": "invalid"},
            "invoiceFileName": "invoice.xml",
        }
    elif outcome == "pending" and mode == "batch":
        info = {"referenceNumber": "I", "status": {"code": 150}, "invoiceFileName": "invoice.xml"}
    fake.send_invoices_online.return_value = ("S", [("invoice.xml", "I", None)])
    fake.send_batch.return_value = "S"
    fake.wait_for_invoice.return_value = info
    fake.session_invoices.return_value = [info]
    if outcome == "pending":
        fake.wait_for_invoice.side_effect = StillProcessing("S", "I")
    elif outcome == "rate-limit":
        fake.send_invoices_online.side_effect = NotSubmitted.wrap(RateLimitError("wait", retry_after=10))
    elif outcome == "rejected" and mode == "online":
        fake.wait_for_invoice.side_effect = InvoiceRejected("invalid", code=450)

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.files = [XmlFile(tmp_path / "invoice.xml", xml, parse_invoice(xml))]
            screen.query_one("#mode", Select).value = mode
            screen.query_one("#send", Button).focus()
            await screen.send().wait()
            await pilot.pause()
            assert not screen.sending
            assert app.focused is screen.query_one("#files")  # nie „Zamknij” - Enter nie zamknie wyników
            if outcome == "accepted":
                assert store.get("K", "issued")["xml"] == xml
                assert screen.files[0].ksef_number == "K"
                assert screen.query_one("#send", Button).disabled
                assert store.sent_record("K")["session_ref"] == "S"
            elif outcome == "pending":
                pending = store.unresolved_sent()
                assert len(pending) == 1 and pending[0]["invoice_ref"] == "I"
                assert screen.query_one("#send", Button).disabled
            else:
                assert store.count() == 0 and not screen.files[0].ksef_number

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["accepted", "pending", "rejected"])
def test_reloading_files_does_not_offer_previously_sent_xml_again(ui, tmp_path, outcome):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    (tmp_path / "invoice.xml").write_bytes(xml)
    status = {"pending": "processing"}.get(outcome, outcome)
    store.record_sent(
        invoice_number=draft.number, status=status, xml=xml, ksef_number="K" if outcome == "accepted" else None
    )

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#import-path").value = str(tmp_path)
            await screen._load().wait()
            f = screen.files[0]
            assert f.ready is (outcome == "rejected")
            assert screen.query_one("#send", Button).disabled is (outcome != "rejected")
            if outcome == "accepted":
                assert f.ksef_number == "K"

    asyncio.run(scenario())


def test_batch_upload_is_recorded_before_waiting_for_its_result(ui, tmp_path):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    fake.send_batch.return_value = "S"
    fake.wait_for_session.side_effect = KsefError("timeout")

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.files = [XmlFile(tmp_path / "invoice.xml", xml, parse_invoice(xml))]
            screen.query_one("#mode", Select).value = "batch"
            await screen.send().wait()
            pending = store.unresolved_sent()
            assert [(r["session_ref"], r["file_name"], r["invoice_ref"]) for r in pending] == [
                ("S", "invoice.xml", None)
            ]
            assert not screen.files[0].ready and screen.query_one("#send", Button).disabled
            assert store.sent_for_xml(xml)["id"] == pending[0]["id"]

    asyncio.run(scenario())


@pytest.mark.parametrize("has_xml", [False, True])
def test_invoice_view_download_copy_export_and_upo(ui, tmp_path, monkeypatch, has_xml, clipboard):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    meta = {
        "ksefNumber": "K",
        "invoiceNumber": "FV/1",
        "issueDate": date.today().isoformat(),
        "seller": {"nip": draft.seller.nip},
        "netAmount": 100,
        "vatAmount": 23,
        "grossAmount": 123,
    }
    store.upsert_metadata("issued", [meta], {"K": xml} if has_xml else {})
    store.record_sent(invoice_number="FV/1", status="accepted", ksef_number="K", session_ref="S")
    fake.get_invoice.return_value = xml
    fake.upo_by_ksef_number.return_value = b"<upo/>"
    monkeypatch.setattr(app, "open_editor", Mock())

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = InvoiceScreen(store.get("K", "issued"))
            await app.push_screen(screen)
            await pilot.pause()
            assert screen.check_action("download", ()) is not has_xml
            assert screen.check_action("correct", ()) is has_xml
            screen.action_copy()
            assert clipboard == ["K"]
            if not has_xml:
                await screen.action_download().wait()
                assert screen.row["xml"] == xml and not screen.check_action("download", ())
            screen.action_save_xml()
            assert (tmp_path / "FV-1_K.xml").read_bytes() == xml
            await screen.action_upo().wait()
            assert (tmp_path / "FV-1_K_UPO.xml").read_bytes() == b"<upo/>"
            await screen.action_upo().wait()
            fake.upo_by_ksef_number.assert_awaited_once()
            screen.action_duplicate()
            assert app.open_editor.call_args.args[0].kind == "VAT"
            screen.action_correct()
            assert app.open_editor.call_args.args[0].correction.ksef_number == "K"

    asyncio.run(scenario())


def test_import_records_all_uploaded_references_before_waiting_for_status(ui, tmp_path):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    fake.send_invoices_online.return_value = ("S", [("a.xml", "I1", None), ("b.xml", "I2", None)])
    fake.wait_for_invoice.side_effect = RateLimitError("wait")

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.files = [XmlFile(tmp_path / name, xml, parse_invoice(xml)) for name in ("a.xml", "b.xml")]
            await screen.send().wait()
            assert [row["invoice_ref"] for row in store.unresolved_sent()] == ["I1", "I2"]
            assert all(not file.ready for file in screen.files)

    asyncio.run(scenario())


def test_recheck_keeps_accepted_result_when_receipt_is_unavailable(ui):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    fake.send_invoice.side_effect = StillProcessing("S", "I")
    fake.wait_for_invoice.return_value = {"ksefNumber": "K"}
    fake.fetch_upo.side_effect = RateLimitError("wait")

    async def scenario():
        async with app.run_test() as pilot:
            screen = SendScreen(draft, build_xml(draft))
            await app.push_screen(screen)
            await pilot.pause()
            await screen.recheck().wait()
            assert screen.success
            assert store.sent_record("K")["status"] == "accepted"

    asyncio.run(scenario())


def test_interrupted_upload_stays_pending_and_cannot_be_sent_again_from_summary(ui):
    from ksefik.client import OutcomeUnknown
    from ksefik.ui.invoice_screens import SummaryScreen

    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))

    async def send(xml, progress, on_refs):
        on_refs(session_ref="S")
        raise OutcomeUnknown("S")

    fake.send_invoice.side_effect = send

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            summary = SummaryScreen(draft)
            await app.push_screen(summary)
            await pilot.pause()
            summary.action_send()
            await pilot.pause()
            await app.workers.wait_for_complete()
            screen = app.screen
            assert isinstance(screen, SendScreen) and screen.query_one("#recheck", Button).display
            row = store.db.execute("SELECT * FROM sent").fetchone()
            assert (row["status"], row["session_ref"], row["invoice_ref"]) == ("unknown", "S", None)
            assert draft.number in store.reserved_numbers()  # numer zarezerwowany do czasu rozstrzygnięcia
            fake.session_invoices.return_value = [{"referenceNumber": "I", "invoiceHash": row["xml_hash"]}]
            fake.wait_for_invoice.side_effect = StillProcessing("S", "I")
            await screen.recheck().wait()
            row = store.sent_entry(row["id"])
            assert (row["status"], row["invoice_ref"]) == ("processing", "I") and screen.outcome == "processing"
            screen._done()
            await pilot.pause()
            assert app.screen is not summary and summary not in app.screen_stack
            # Ponowne podsumowanie tej samej faktury nie pozwala wysłać jej drugi raz.
            again = SummaryScreen(draft)
            await app.push_screen(again)
            await pilot.pause()
            assert again.query_one("#send", Button).disabled
            assert any("została już wysłana" in message for message in again.blocking)

    asyncio.run(scenario())


def test_accepted_invoice_is_recorded_even_if_its_files_cannot_be_written(ui):
    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    fake.send_invoice.return_value = SendResult("K", "S", "I", datetime.now(UTC), b"<upo/>")
    app.profile.data_dir.mkdir(parents=True, exist_ok=True)
    (app.profile.data_dir / "wystawione").write_text("not a folder", encoding="utf-8")

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = SendScreen(draft, build_xml(draft))
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            assert screen.success and store.sent_record("K")["status"] == "accepted"
            assert store.get("K", "issued") is not None
            assert "nie udało się zapisać pliku" in str(screen.query_one("#result").render())

    asyncio.run(scenario())


def test_import_file_with_interrupted_upload_is_not_offered_again(ui, tmp_path):
    from ksefik.client import ConnectionInterrupted

    app, fake, store = ui
    draft = new_draft(app.profile, date.today(), [], net=Decimal(100))
    xml = build_xml(draft)
    fake.send_invoices_online.return_value = ("S", [("invoice.xml", None, ConnectionInterrupted("dropped"))])

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = ImportScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.files = [XmlFile(tmp_path / "invoice.xml", xml, parse_invoice(xml))]
            await screen.send().wait()
            row = store.unresolved_sent()[0]
            assert (row["session_ref"], row["invoice_ref"], row["file_name"]) == ("S", None, "invoice.xml")
            assert (
                not screen.files[0].ready
                and "przerwane" in screen.files[0].message
                and screen.files[0].state == "unknown"
            )
            fake.wait_for_invoice.assert_not_awaited()

    asyncio.run(scenario())
