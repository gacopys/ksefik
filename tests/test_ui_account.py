import asyncio
from dataclasses import replace

import pytest
from textual.widgets import DataTable, Input, OptionList, Select, Static

from ksefik.client import KsefError
from ksefik.ui import account, certificates, permissions
from ksefik.ui.common import ConfirmScreen, FormScreen


@pytest.mark.parametrize(
    "kind,method,item",
    [
        (
            account.SessionsScreen,
            "list_sessions",
            {
                "referenceNumber": "S",
                "status": {"code": 200, "description": "Accepted"},
                "successfulInvoiceCount": 2,
                "totalInvoiceCount": 2,
            },
        ),
        (
            lambda: account.SessionInvoicesScreen("S"),
            "session_invoices",
            {
                "referenceNumber": "I",
                "invoiceNumber": "FV/1",
                "status": {"code": 450, "description": "Rejected", "details": ["bad"]},
            },
        ),
        (
            account.TokensScreen,
            "list_tokens",
            {
                "referenceNumber": "T",
                "description": "Token",
                "status": "Active",
                "requestedPermissions": ["InvoiceRead"],
                "statusDetails": ["detail"],
            },
        ),
        (
            account.AuthSessionsScreen,
            "auth_sessions",
            {"referenceNumber": "A", "status": {"code": 200}, "isCurrent": True},
        ),
        (account.PeppolScreen, "peppol_providers", {"id": "P", "name": "Provider"}),
        (
            account.CollectiveScreen,
            "query_collective_identifiers",
            {"collectiveIdentifierNumber": "C", "invoiceCount": 3},
        ),
        (
            certificates.CertificatesScreen,
            "query_certificates",
            {"certificateSerialNumber": "1", "name": "Cert", "status": "Active"},
        ),
        (
            permissions.PermissionsScreen,
            "query_person_permissions",
            {
                "id": "P",
                "subjectPersonDetails": {"firstName": "Jan", "lastName": "Test"},
                "permissionScope": "InvoiceRead",
                "description": "Permission",
                "canDelegate": True,
            },
        ),
    ],
)
def test_account_lists_load_select_show_empty_and_handle_api_errors(ui, kind, method, item):
    app, fake, _ = ui
    getattr(fake, method).return_value = [item]
    fake.certificate_limits.return_value = {"canRequest": True}

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = kind()
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            assert screen.query_one("#list", DataTable).row_count == 1
            assert screen.selected == item
            assert screen.query_one("#list").display
            assert isinstance(screen.details(item), str)
            screen.show_items([])
            assert screen.selected is None and not screen.query_one("#list").display
            assert screen.query_one("#list-empty").display
            getattr(fake, method).side_effect = KsefError("denied", http_status=403, details=["reason"])
            await screen.action_reload().wait()
            assert "denied" in str(screen.query_one("#list-empty", Static).content)

    asyncio.run(scenario())


def test_permissions_views_use_correct_client_queries_and_revoke_only_when_allowed(ui):
    app, fake, _ = ui
    for name in (
        "query_person_permissions",
        "query_personal_permissions",
        "query_authorizations",
        "query_entity_permissions",
        "query_subunit_permissions",
        "query_eu_entity_permissions",
        "query_entity_roles",
        "query_subordinate_entities",
    ):
        getattr(fake, name).return_value = []

    async def scenario():
        async with app.run_test(size=(140, 50)) as pilot:
            screen = permissions.PermissionsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            for key, view in permissions.VIEWS.items():
                screen.query_one("#view", Select).value = key
                await pilot.pause()
                await app.workers.wait_for_complete()
                assert screen.check_action("revoke", ()) is (view.revoke is not None)
                row = screen.row({"permissionState": "Inactive", "permissionScope": "InvoiceRead"})
                assert "nieaktywne" in str(row[2])
            fake.query_person_permissions.assert_awaited_with("PermissionsInCurrentContext")
            fake.query_authorizations.assert_awaited_with("Received")

    asyncio.run(scenario())


def test_permission_revocation_requires_confirmation_then_waits_for_completion(ui):
    app, fake, _ = ui
    item = {"id": "P", "permissionScope": "InvoiceRead"}
    fake.query_person_permissions.return_value = [item]
    fake.revoke_permission.return_value = "operation"
    fake.wait_for_permission_operation.return_value = {"code": 200}

    async def scenario():
        async with app.run_test() as pilot:
            screen = permissions.PermissionsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            screen.action_revoke()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            fake.revoke_permission.assert_not_awaited()
            app.screen._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.revoke_permission.assert_awaited_once_with("P")
            fake.wait_for_permission_operation.assert_awaited_once_with("operation")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["person", "entity", "authorization", "indirect", "subunit", "eu_admin", "eu_representative"]
)
def test_permission_forms_have_required_fields_and_can_cancel(ui, kind):
    app, _, _ = ui

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            screen = permissions.PermissionsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            screen._grant_type(kind)
            await pilot.pause()
            assert isinstance(app.screen, FormScreen)
            assert app.screen.fields and any(field.required for field in app.screen.fields)
            app.screen.action_submit()
            assert isinstance(app.screen, FormScreen)
            app.screen.action_cancel()
            await pilot.pause()
            assert app.screen is screen

    asyncio.run(scenario())


def test_limits_render_actual_values_and_api_failure(ui):
    app, fake, _ = ui
    fake.context_limits.return_value = {"onlineSession": {"maxInvoices": 123}, "batchSession": {"maxInvoices": 456}}
    fake.subject_limits.return_value = {"certificate": {"maxCertificates": 7}}
    fake.rate_limits.return_value = {"onlineSession": {"perSecond": 1, "perMinute": 2, "perHour": 3}}

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.LimitsScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await pilot.pause()
            summary = str(screen.query_one("#limits-summary", Static).content)
            assert "123" in summary and "456" in summary and "7" in summary
            assert screen.query_one("#rate-limits", DataTable).row_count == 1
            fake.context_limits.side_effect = KsefError("offline")
            await screen.action_reload().wait()
            assert "offline" in str(screen.query_one("#limits-summary", Static).content)

    asyncio.run(scenario())


def test_session_upo_is_downloaded_to_safe_filename(ui, tmp_path):
    app, fake, _ = ui
    fake.list_sessions.return_value = [{"referenceNumber": "S", "status": {"code": 200}}]
    fake.session_status.return_value = {"upo": {"pages": [{"referenceNumber": "U"}]}}
    fake.session_upo.return_value = b"<upo/>"

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.SessionsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            await screen.action_session_upo().wait()
            assert (tmp_path / "UPO_S_U.xml").read_bytes() == b"<upo/>"
            fake.session_upo.assert_awaited_once_with("S", "U")

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [False, True])
def test_certificate_enrollment_preserves_generated_key_if_later_request_fails(ui, tmp_path, failure):
    app, fake, _ = ui
    fake.query_certificates.return_value = []
    fake.certificate_limits.return_value = {}
    fake.enrollment_data.return_value = {"commonName": "Test", "countryName": "PL"}
    fake.enroll_certificate.return_value = {"referenceNumber": "E"}
    if failure:
        fake.wait_for_enrollment.side_effect = KsefError("temporary outage", http_status=503)
    else:
        fake.wait_for_enrollment.return_value = {"status": {"code": 200}}

    async def scenario():
        async with app.run_test() as pilot:
            screen = certificates.CertificatesScreen()
            await app.push_screen(screen)
            await app.workers.wait_for_complete()
            await screen._enroll({"name": "test-name", "type": "Authentication", "password": "secret"}).wait()
            await pilot.pause()
            keys = list((tmp_path / "certificates").glob("*.klucz.pem"))
            assert len(keys) == 1
            assert keys[0].stat().st_mode & 0o777 == 0o600

    asyncio.run(scenario())


@pytest.mark.parametrize("env", ["prod", "test"])
def test_testdata_menu_available_only_in_test_environment(ui, env):
    app, _, _ = ui
    app._profile = replace(app.profile, env=env)

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.TestDataScreen()
            await app.push_screen(screen)
            await pilot.pause()
            assert (app.screen is screen) is (env == "test")
            if env == "test":
                assert screen.query_one("#operations", OptionList).option_count > 10
                await screen._execute(
                    "Test", ("context/unblock", {"contextIdentifier": {"type": "Nip", "value": "123"}})
                ).wait()
                app.get_client.return_value.testdata.assert_awaited_once()

    asyncio.run(scenario())


def test_certificate_enrollment_success_download_and_revocation(ui, tmp_path, monkeypatch, clipboard):
    import base64

    from cryptography.hazmat.primitives import serialization
    from test_client import MF_CERT

    app, fake, _ = ui
    der = base64.b64encode(MF_CERT.public_bytes(serialization.Encoding.DER)).decode()
    fake.certificate_limits.return_value = {"canRequest": True}
    item = {"certificateSerialNumber": "123", "status": "Active", "name": "Test cert"}
    fake.query_certificates.return_value = [item]
    fake.enrollment_data.return_value = {"commonName": "Test", "countryName": "PL"}
    fake.enroll_certificate.return_value = {"referenceNumber": "E"}
    fake.wait_for_enrollment.return_value = {"status": {"code": 200}, "certificateSerialNumber": "123"}
    fake.retrieve_certificates.return_value = [{"certificate": der}]

    async def scenario():
        async with app.run_test(size=(140, 60)) as pilot:
            screen = certificates.CertificatesScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_enroll()
            await pilot.pause()
            form = app.screen
            assert isinstance(form, FormScreen)
            form.query_one("#f-name", Input).value = "New test cert"
            form.action_submit()
            await pilot.pause()
            await app.workers.wait_for_complete()
            files = list((tmp_path / "certificates").glob("*.pem"))
            assert len(files) == 2
            assert len(clipboard) == 1 and clipboard[0].startswith("[logowanie]")
            await screen.action_download().wait()
            assert (tmp_path / "certificates/123.pem").read_bytes() == MF_CERT.public_bytes(serialization.Encoding.PEM)
            screen.action_revoke()
            await pilot.pause()
            app.screen.action_submit()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            fake.revoke_certificate.assert_not_awaited()
            app.screen._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.revoke_certificate.assert_awaited_once_with("123", "Unspecified")

    asyncio.run(scenario())


def test_session_invoice_failed_filter_and_receipt_download(ui, tmp_path):
    app, fake, _ = ui
    fake.session_invoices.return_value = [{"referenceNumber": "I", "ksefNumber": "K", "status": {"code": 200}}]
    fake.upo_by_reference.return_value = b"<upo/>"

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.SessionInvoicesScreen("S")
            await app.push_screen(screen)
            await pilot.pause()
            await screen.action_upo().wait()
            assert (tmp_path / "UPO_K.xml").read_bytes() == b"<upo/>"
            screen.action_toggle_failed()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.session_invoices.assert_awaited_with("S", failed_only=True)
            screen.action_toggle_failed()
            await pilot.pause()
            await app.workers.wait_for_complete()
            fake.session_invoices.assert_awaited_with("S", failed_only=False)

    asyncio.run(scenario())


@pytest.mark.parametrize("current", [True, False])
def test_auth_session_revocation_uses_correct_endpoint(ui, current):
    app, fake, _ = ui
    fake.auth_sessions.return_value = [{"referenceNumber": "A", "status": {"code": 200}, "isCurrent": current}]

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.AuthSessionsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            screen.action_revoke()
            await pilot.pause()
            assert isinstance(app.screen, ConfirmScreen)
            app.screen._confirm()
            await pilot.pause()
            await app.workers.wait_for_complete()
            if current:
                fake.revoke_current_session.assert_awaited_once_with()
            else:
                fake.revoke_auth_session.assert_awaited_once_with("A")

    asyncio.run(scenario())


def test_collective_identifier_details_show_invoice_payment(ui):
    app, fake, _ = ui
    fake.query_collective_identifiers.return_value = [{"collectiveIdentifierNumber": "C"}]
    fake.collective_identifier_invoices.return_value = [
        {"ksefNumber": "K", "payment": {"amount": 123, "currency": "PLN"}}
    ]

    async def scenario():
        async with app.run_test() as pilot:
            screen = account.CollectiveScreen()
            await app.push_screen(screen)
            await pilot.pause()
            await screen._show_invoices("C").wait()
            assert "123,00 PLN" in str(screen.query_one("#list-details", Static).content)

    asyncio.run(scenario())


def test_permission_grant_builds_identity_payload_and_waits_for_result(ui):
    app, fake, _ = ui
    fake.grant_person_permissions.return_value = "operation"
    fake.query_person_permissions.return_value = []
    fake.wait_for_permission_operation.return_value = {"code": 200}

    async def scenario():
        async with app.run_test() as pilot:
            screen = permissions.PermissionsScreen()
            await app.push_screen(screen)
            await pilot.pause()
            _, handler = screen._person_form()
            values = {
                "kind": "Pesel",
                "value": "44051401458",
                "first": "Jan",
                "last": "Test",
                "permissions": ["InvoiceRead"],
                "description": "Unit test",
            }
            assert await screen._grant(handler)(values) is None
            fake.grant_person_permissions.assert_awaited_once_with(
                "Pesel",
                "44051401458",
                ["InvoiceRead"],
                "Unit test",
                {"subjectDetailsType": "PersonByIdentifier", "personById": {"firstName": "Jan", "lastName": "Test"}},
            )
            fake.wait_for_permission_operation.assert_awaited_once_with("operation")

    asyncio.run(scenario())
