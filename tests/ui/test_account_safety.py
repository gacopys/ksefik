"""Uprawnienia i certyfikaty: Enter nie nadaje uprawnień, wnioski w toku i nieodebrane certyfikaty nie giną."""

import asyncio
import base64
import json

from cryptography.hazmat.primitives import serialization
from textual.widgets import Button, Input

from ksefik.client import KsefError, OperationPending
from ksefik.ui import certificates
from ksefik.ui.common import FormScreen
from ksefik.ui.permissions import PermissionsScreen

PERSON = {"f-value": "44051401458", "f-first": "Jan", "f-last": "Testowy", "f-description": "Księgowa - test"}


async def open_person_form(app, pilot):
    screen = PermissionsScreen()
    await app.push_screen(screen)
    await pilot.pause()
    await app.workers.wait_for_complete()
    screen._grant_type("person")
    await pilot.pause()
    form = app.screen
    assert isinstance(form, FormScreen)
    return screen, form


def test_enter_in_a_grant_form_moves_through_fields_and_never_grants(seeded):
    app, fake, _ = seeded

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            _, form = await open_person_form(app, pilot)
            for widget_id, value in PERSON.items():
                field = form.query_one(f"#{widget_id}", Input)
                field.focus()
                field.value = value
                await pilot.press("enter")
                await pilot.pause()
            assert app.focused is form.query_one("#submit", Button)  # po ostatnim polu - przycisk, bez naciskania
            await pilot.press("ctrl+s")  # skrót zapisu nie działa w operacjach w KSeF
            await pilot.pause()
            assert app.screen is form
            fake.grant_person_permissions.assert_not_awaited()

    asyncio.run(scenario())


def test_grant_still_processing_stays_pending_and_is_checked_by_its_reference(seeded):
    app, fake, _ = seeded
    fake.grant_person_permissions.return_value = "OP-1"
    fake.wait_for_permission_operation.side_effect = OperationPending("OP-1", {"code": 100})
    fake.permission_operation.return_value = {"status": {"code": 200, "description": "Zakończono"}}

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen, form = await open_person_form(app, pilot)
            for widget_id, value in PERSON.items():
                form.query_one(f"#{widget_id}", Input).value = value
            form.query_one("#submit", Button).press()
            await pilot.pause()
            await pilot.click("#confirm")  # przegląd: kto, co, w czyim imieniu
            await pilot.pause()
            await app.workers.wait_for_complete()
            outcome = str(screen.query_one("#op-status").render())
            assert "w trakcie" in outcome and "OP-1" in outcome
            assert screen.check_action("check_operations", ()) is True
            # Inny wiersz na liście nie ma znaczenia - sprawdzany jest numer wniosku.
            await pilot.press("s")
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.permission_operation.assert_awaited_once_with("OP-1")
            assert "zakończone" in str(screen.query_one("#op-status").render())
            assert not screen.pending_operations

    asyncio.run(scenario())


def test_certificate_retrieval_failure_keeps_key_and_resumes_on_next_visit(seeded, isolated, monkeypatch):
    from test_client import MF_CERT

    app, fake, _ = seeded
    der = base64.b64encode(MF_CERT.public_bytes(serialization.Encoding.DER)).decode()
    fake.enrollment_data.return_value = {"commonName": "Test", "countryName": "PL"}
    fake.enroll_certificate.return_value = {"referenceNumber": "E-1"}
    fake.wait_for_enrollment.return_value = {"status": {"code": 200}, "certificateSerialNumber": "77"}
    fake.retrieve_certificates.side_effect = KsefError("Przekroczono czas oczekiwania.")
    monkeypatch.setattr(app, "copy_to_clipboard", lambda text: None)
    folder = isolated / "certificates"

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = certificates.CertificatesScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await screen._enroll({"name": "laptop-test", "type": "Authentication", "password": None}).wait()
            assert len(list(folder.glob("*.klucz.pem"))) == 1
            (sidecar,) = folder.glob("*.wniosek.json")
            assert json.loads(sidecar.read_text())["reference"] == "E-1"
            assert "Klucz jest zapisany" in str(screen.query_one("#op-status").render())
            app.pop_screen()
            await pilot.pause()

            # Ponowne otwarcie (np. po restarcie aplikacji): certyfikat jest pobierany dla zapisanego wniosku.
            fake.retrieve_certificates.side_effect = None
            fake.retrieve_certificates.return_value = [{"certificate": der}]
            again = certificates.CertificatesScreen()
            await app.push_screen(again)
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.wait_for_enrollment.assert_awaited_with("E-1")
            assert not list(folder.glob("*.wniosek.json"))
            assert len(list(folder.glob("*.pem"))) == 2  # klucz i certyfikat
            assert "gotowy" in str(again.query_one("#op-status").render())

    asyncio.run(scenario())


def test_rejected_enrollment_request_does_not_leave_a_useless_key(seeded, isolated):
    app, fake, _ = seeded
    fake.enrollment_data.return_value = {"commonName": "Test", "countryName": "PL"}
    fake.enroll_certificate.side_effect = KsefError("Brak uprawnień.", http_status=403)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            screen = certificates.CertificatesScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await screen._enroll({"name": "laptop-test", "type": "Authentication", "password": None}).wait()
            assert not list((isolated / "certificates").glob("*"))

    asyncio.run(scenario())
