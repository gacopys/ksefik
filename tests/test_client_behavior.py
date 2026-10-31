import asyncio
import base64
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

import ksefik.client as client_module
from ksefik.client import (
    AuthError,
    InvoiceRejected,
    KsefClient,
    KsefError,
    RateLimitError,
    StillProcessing,
    TokenCredentials,
    iso,
    parse_dt,
    person_details,
    report,
)
from ksefik.crypto import SymmetricKey, sha256_b64

API = "https://example.test/v2"
DAY = datetime(2026, 10, 23, tzinfo=UTC)


def authenticated(handler):
    c = KsefClient(API, "5438539502", TokenCredentials("secret"), transport=httpx.MockTransport(handler))
    c._access = ("access", datetime.now(UTC) + timedelta(hours=1))
    return c


def test_datetime_and_identity_helpers():
    assert parse_dt(None) is None
    assert parse_dt("2026-10-23T12:00:00.1234567Z") == datetime(2026, 10, 23, 12, 0, 0, 123456, UTC)
    assert parse_dt("2026-10-23T00:00:00") == DAY
    assert iso(DAY.astimezone(timezone(timedelta(hours=2)))) == "2026-10-23T00:00:00.000Z"
    assert person_details(" Jan ", " Kowalski ") == {
        "subjectDetailsType": "PersonByIdentifier",
        "personById": {"firstName": "Jan", "lastName": "Kowalski"},
    }
    error = KsefError("message", details=["", "detail"])
    assert error.human() == "message\n• detail"


def test_progress_accepts_sync_and_async_callbacks():
    async def scenario():
        events = []
        await report(None, "step", "ok")
        await report(lambda *args: events.append(args), "sync", "ok")
        callback = AsyncMock()
        await report(callback, "async", "ok")
        callback.assert_awaited_once_with("async", "ok")
        assert events == [("sync", "ok")]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind", "message", "details"),
    [
        (401, {}, {}, AuthError, "Sesja", []),
        (403, {}, {}, AuthError, "Brak uprawnień", []),
        (503, {}, {}, KsefError, "niedostępna", []),
        (418, ["unexpected"], {}, KsefError, "418", ['["unexpected"]']),
        (
            400,
            {"title": "Invalid", "errors": [{"description": "a"}, {"message": "b"}]},
            {},
            KsefError,
            "Invalid",
            ["a", "b"],
        ),
        (429, {}, {"Retry-After": "invalid"}, RateLimitError, "ogranicza", []),
        (
            400,
            {
                "exception": {
                    "exceptionDetailList": [
                        {"exceptionCode": 1, "exceptionDescription": "first", "details": ["a"]},
                        {"exceptionDescription": "second", "details": ["b"]},
                    ]
                }
            },
            {},
            KsefError,
            "first",
            ["a", "second", "b"],
        ),
    ],
)
def test_http_error_variants(status, body, headers, kind, message, details):
    error = KsefClient._error(httpx.Response(status, json=body, headers=headers))
    assert isinstance(error, kind)
    assert error.http_status == status and message in error.message
    assert error.details == details


@pytest.mark.parametrize("content_type,details", [("text/plain", ["failure"]), ("text/html", [])])
def test_non_json_error_body(content_type, details):
    error = KsefClient._error(httpx.Response(400, text="failure", headers={"Content-Type": content_type}))
    assert error.details == details


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")])
def test_transport_failures_become_readable_errors(exception):
    def handler(request):
        raise exception

    async def scenario():
        async with authenticated(handler) as c:
            with pytest.raises(KsefError) as caught:
                await c.context_limits()
            assert caught.value.__cause__ is exception

    asyncio.run(scenario())


def test_unauthorized_response_refreshes_once_and_preserves_request():
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/auth/token/refresh"):
            assert request.headers["Authorization"] == "Bearer refresh"
            return httpx.Response(200, json={"accessToken": {"token": "new", "validUntil": "2099-01-01T00:00:00Z"}})
        return httpx.Response(401)

    async def scenario():
        async with authenticated(handler) as c:
            c._refresh = ("refresh", datetime.now(UTC) + timedelta(hours=1))
            with pytest.raises(AuthError):
                await c._call("POST", "/operation", json_body={"a": 1}, params={"keep": 2, "omit": None})
        assert len(requests) == 3
        for request in (requests[0], requests[2]):
            assert json.loads(request.content) == {"a": 1}
            assert dict(request.url.params) == {"keep": "2"}
        assert requests[2].headers["Authorization"] == "Bearer new"

    asyncio.run(scenario())


def test_refresh_failure_reauthenticates_and_auth_lock_prevents_duplicate_login(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(403)) as c:
            c._access = None
            c._refresh = ("refresh", datetime.now(UTC) + timedelta(hours=1))

            async def login():
                c._access = ("fresh", datetime.now(UTC) + timedelta(hours=1))

            login_mock = AsyncMock(side_effect=login)
            monkeypatch.setattr(c, "_authenticate", login_mock)
            await asyncio.gather(*(c.ensure_auth() for _ in range(5)))
            login_mock.assert_awaited_once()
            assert c._refresh is None

    asyncio.run(scenario())


def test_key_rotation_retries_only_key_error_and_only_once():
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            operation = AsyncMock(side_effect=[KsefError("retired", code=21470), "done"])
            assert await c._with_key_retry(operation) == "done"
            assert [call.args for call in operation.call_args_list] == [(False,), (True,)]
            for code in (1, 21470):
                failure = KsefError("failure", code=code)
                operation = AsyncMock(side_effect=failure)
                with pytest.raises(KsefError) as caught:
                    await c._with_key_retry(operation)
                assert caught.value is failure
                assert operation.await_count == (2 if code == 21470 else 1)

    asyncio.run(scenario())


def test_public_key_selection_ignores_expired_future_and_wrong_usage():
    calls = []
    keys = [
        {"id": "old", "usage": ["wanted"], "validFrom": "2020-01-01Z", "validTo": "2099-01-01Z"},
        {"id": "new", "usage": ["wanted"], "validFrom": "2025-01-01Z", "validTo": "2099-01-01Z"},
        {"id": "expired", "usage": ["wanted"], "validFrom": "2025-01-01Z", "validTo": "2025-02-01Z"},
        {"id": "future", "usage": ["wanted"], "validFrom": "2090-01-01Z"},
        {"id": "wrong", "usage": ["other"], "validFrom": "2026-01-01Z"},
    ]

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=keys)

    async def scenario():
        async with authenticated(handler) as c:
            assert (await c._public_key("wanted"))["id"] == "new"
            await c._public_key("wanted")
            assert len(calls) == 1
            await c._public_key("wanted", refresh=True)
            assert len(calls) == 2
            with pytest.raises(KsefError, match="ważnego klucza"):
                await c._public_key("absent")

    asyncio.run(scenario())


@pytest.mark.parametrize("method,key", [("_pages", "hasMore"), ("_continuation", "continuationToken")])
def test_pagination_honors_limit_without_extra_request(method, key):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"items": [{"id": 1}, {"id": 2}], key: "more"})

    async def scenario():
        async with authenticated(handler) as c:
            assert await getattr(c, method)("/items", "items", limit=1) == [{"id": 1}]
            assert len(requests) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,args,path,key",
    [
        ("auth_sessions", (), "/auth/sessions", "items"),
        ("list_sessions", (), "/sessions", "sessions"),
        ("session_invoices", ("S",), "/sessions/S/invoices", "invoices"),
        ("query_metadata", ({"subjectType": "Subject2"},), "/invoices/query/metadata", "invoices"),
        ("query_personal_permissions", (), "/permissions/query/personal/grants", "permissions"),
        ("query_subunit_permissions", (), "/permissions/query/subunits/grants", "permissions"),
        ("query_entity_permissions", (), "/permissions/query/entities/grants", "permissions"),
        ("query_entity_roles", (), "/permissions/query/entities/roles", "roles"),
        ("query_subordinate_entities", (), "/permissions/query/subordinate-entities/roles", "roles"),
        ("query_authorizations", (), "/permissions/query/authorizations/grants", "authorizationGrants"),
        ("query_eu_entity_permissions", (), "/permissions/query/eu-entities/grants", "permissions"),
        ("query_certificates", (), "/certificates/query", "certificates"),
        ("peppol_providers", (), "/peppol/query", "peppolProviders"),
        ("collective_identifier_invoices", (["C"],), "/collective-identifiers/invoices", "invoices"),
        ("collective_identifiers_for_invoice", ("K",), "/collective-identifiers/ksef/K", "collectiveIdentifiers"),
        ("query_collective_identifiers", (DAY, DAY), "/collective-identifiers/query", "collectiveIdentifiers"),
    ],
)
def test_query_endpoints_return_records_and_authenticate(name, args, path, key):
    def handler(request):
        assert request.url.path == "/v2" + path
        assert request.headers["Authorization"] == "Bearer access"
        assert int(request.url.params["pageSize"]) > 0
        return httpx.Response(200, json={key: [{"id": "record"}]})

    async def scenario():
        async with authenticated(handler) as c:
            assert await getattr(c, name)(*args) == [{"id": "record"}]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,args,method,path,body,response,expected",
    [
        (
            "generate_token",
            (["InvoiceRead"], "test token"),
            "POST",
            "/tokens",
            {"permissions": ["InvoiceRead"], "description": "test token"},
            {"token": "new"},
            {"token": "new"},
        ),
        (
            "grant_person_permissions",
            ("Pesel", "123", ["InvoiceRead"], "desc", {"personById": {}}),
            "POST",
            "/permissions/persons/grants",
            {
                "subjectIdentifier": {"type": "Pesel", "value": "123"},
                "permissions": ["InvoiceRead"],
                "description": "desc",
                "subjectDetails": {"personById": {}},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_entity_permissions",
            ("123", [("InvoiceRead", True)], "desc", "Company"),
            "POST",
            "/permissions/entities/grants",
            {
                "subjectIdentifier": {"type": "Nip", "value": "123"},
                "permissions": [{"type": "InvoiceRead", "canDelegate": True}],
                "description": "desc",
                "subjectDetails": {"fullName": "Company"},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_authorization",
            ("Nip", "123", "SelfInvoicing", "desc", "Company"),
            "POST",
            "/permissions/authorizations/grants",
            {
                "subjectIdentifier": {"type": "Nip", "value": "123"},
                "permission": "SelfInvoicing",
                "description": "desc",
                "subjectDetails": {"fullName": "Company"},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_indirect_permissions",
            ("Nip", "123", ["InvoiceRead"], "desc", {}, ("AllPartners", None)),
            "POST",
            "/permissions/indirect/grants",
            {
                "subjectIdentifier": {"type": "Nip", "value": "123"},
                "permissions": ["InvoiceRead"],
                "description": "desc",
                "subjectDetails": {},
                "targetIdentifier": {"type": "AllPartners"},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_subunit_admin",
            ("Nip", "123", ("InternalId", "X"), "desc", {}, "Unit"),
            "POST",
            "/permissions/subunits/grants",
            {
                "subjectIdentifier": {"type": "Nip", "value": "123"},
                "contextIdentifier": {"type": "InternalId", "value": "X"},
                "description": "desc",
                "subjectDetails": {},
                "subunitName": "Unit",
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_eu_entity_admin",
            ("FP", "123-DE456", "desc", "Company", {}, {"address": "Street"}),
            "POST",
            "/permissions/eu-entities/administration/grants",
            {
                "subjectIdentifier": {"type": "Fingerprint", "value": "FP"},
                "contextIdentifier": {"type": "NipVatUe", "value": "123-DE456"},
                "description": "desc",
                "euEntityName": "Company",
                "subjectDetails": {},
                "euEntityDetails": {"address": "Street"},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "grant_eu_entity_representative",
            ("FP", ["InvoiceRead"], "desc", {}),
            "POST",
            "/permissions/eu-entities/grants",
            {
                "subjectIdentifier": {"type": "Fingerprint", "value": "FP"},
                "permissions": ["InvoiceRead"],
                "description": "desc",
                "subjectDetails": {},
            },
            {"referenceNumber": "R"},
            "R",
        ),
        (
            "enroll_certificate",
            ("Test", "Authentication", b"csr", DAY),
            "POST",
            "/certificates/enrollments",
            {
                "certificateName": "Test",
                "certificateType": "Authentication",
                "csr": "Y3Ny",
                "validFrom": "2026-10-23T00:00:00.000Z",
            },
            {"referenceNumber": "R"},
            {"referenceNumber": "R"},
        ),
        (
            "retrieve_certificates",
            (["1"],),
            "POST",
            "/certificates/retrieve",
            {"certificateSerialNumbers": ["1"]},
            {"certificates": [{"serial": "1"}]},
            [{"serial": "1"}],
        ),
        (
            "generate_collective_identifier",
            ([{"ksefNumber": "K"}], "payment"),
            "POST",
            "/collective-identifiers",
            {"invoices": [{"ksefNumber": "K"}], "description": "payment"},
            {"collectiveIdentifierNumber": "C"},
            "C",
        ),
        (
            "revoke_certificate",
            ("1", "KeyCompromise"),
            "POST",
            "/certificates/1/revoke",
            {"revocationReason": "KeyCompromise"},
            None,
            None,
        ),
        ("revoke_permission", ("P",), "DELETE", "/permissions/common/grants/P", None, {"referenceNumber": "R"}, "R"),
        (
            "revoke_authorization",
            ("P",),
            "DELETE",
            "/permissions/authorizations/grants/P",
            None,
            {"referenceNumber": "R"},
            "R",
        ),
        ("revoke_auth_session", ("S",), "DELETE", "/auth/sessions/S", None, None, None),
        ("revoke_current_session", (), "DELETE", "/auth/sessions/current", None, None, None),
        ("revoke_token", ("T",), "DELETE", "/tokens/T", None, None, None),
    ],
)
def test_mutation_endpoint_contracts(name, args, method, path, body, response, expected):
    def handler(request):
        assert request.method == method and request.url.path == "/v2" + path
        assert request.headers["Authorization"] == "Bearer access"
        assert (json.loads(request.content) if request.content else None) == body
        return httpx.Response(200, json=response) if response is not None else httpx.Response(204)

    async def scenario():
        async with authenticated(handler) as c:
            assert await getattr(c, name)(*args) == expected
            if name == "revoke_current_session":
                assert c._access is None and c._refresh is None

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,args,path",
    [
        ("token_status", ("T",), "/tokens/T"),
        ("permission_operation", ("P",), "/permissions/operations/P"),
        ("attachment_permission_status", (), "/permissions/attachments/status"),
        ("certificate_limits", (), "/certificates/limits"),
        ("enrollment_data", (), "/certificates/enrollments/data"),
        ("enrollment_status", ("E",), "/certificates/enrollments/E"),
        ("context_limits", (), "/limits/context"),
        ("subject_limits", (), "/limits/subject"),
        ("rate_limits", (), "/rate-limits"),
        ("session_status", ("S",), "/sessions/S"),
        ("invoice_status", ("S", "I"), "/sessions/S/invoices/I"),
        ("export_status", ("E",), "/invoices/exports/E"),
    ],
)
def test_status_endpoint_contracts(name, args, path):
    def handler(request):
        assert request.method == "GET" and request.url.path == "/v2" + path
        return httpx.Response(200, json={"status": "example"})

    async def scenario():
        async with authenticated(handler) as c:
            assert await getattr(c, name)(*args) == {"status": "example"}

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,args,path",
    [
        ("get_invoice", ("K",), "/invoices/ksef/K"),
        ("upo_by_reference", ("S", "I"), "/sessions/S/invoices/I/upo"),
        ("upo_by_ksef_number", ("S", "K"), "/sessions/S/invoices/ksef/K/upo"),
        ("session_upo", ("S", "U"), "/sessions/S/upo/U"),
    ],
)
def test_xml_download_contracts(name, args, path):
    def handler(request):
        assert request.url.path == "/v2" + path and request.headers["Accept"] == "application/xml"
        return httpx.Response(200, content=b"<document/>")

    async def scenario():
        async with authenticated(handler) as c:
            assert await getattr(c, name)(*args) == b"<document/>"

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,status_method",
    [
        ("wait_for_invoice", "invoice_status"),
        ("wait_for_session", "session_status"),
        ("wait_for_permission_operation", "permission_operation"),
        ("wait_for_enrollment", "enrollment_status"),
    ],
)
@pytest.mark.parametrize("outcome", ["accepted", "rejected", "timeout"])
def test_polling_terminal_states_and_timeouts(monkeypatch, name, status_method, outcome):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            status = {
                "code": {"accepted": 200, "rejected": 450, "timeout": 100}[outcome],
                "description": "reason",
                "details": ["detail"],
                "extensions": {"original": "K"},
            }
            terminal = {"status": status, "ksefNumber": "K"}
            mocked = AsyncMock(return_value=terminal)
            monkeypatch.setattr(c, status_method, mocked)
            args = ("S", "I") if name == "wait_for_invoice" else ("R",)
            if outcome == "accepted":
                result = await getattr(c, name)(*args, timeout=-1)
                assert (result if name == "wait_for_permission_operation" else result["status"]) == status
            elif outcome == "rejected":
                with pytest.raises(InvoiceRejected if name == "wait_for_invoice" else KsefError) as caught:
                    await getattr(c, name)(*args, timeout=-1)
                assert caught.value.code == 450 and caught.value.details == ["detail"]
                if name == "wait_for_invoice":
                    assert caught.value.extensions == {"original": "K"}
            elif name == "wait_for_permission_operation":
                with pytest.raises(client_module.OperationPending) as caught:
                    await getattr(c, name)(*args, timeout=-1)
                assert caught.value.reference == "R" and caught.value.status == status
            elif name == "wait_for_enrollment":
                assert await getattr(c, name)(*args, timeout=-1)
            else:
                with pytest.raises(StillProcessing if name == "wait_for_invoice" else KsefError):
                    await getattr(c, name)(*args, timeout=-1)

    asyncio.run(scenario())


def test_online_upload_encrypts_document_and_hashes_original_and_ciphertext():
    key = SymmetricKey.generate()
    xml = b"<invoice/>"

    def handler(request):
        assert request.url.path == "/v2/sessions/online/S/invoices"
        body = json.loads(request.content)
        ciphertext = base64.b64decode(body["encryptedInvoiceContent"])
        assert key.decrypt(ciphertext) == xml
        assert (body["invoiceHash"], body["invoiceSize"]) == (sha256_b64(xml), len(xml))
        assert (body["encryptedInvoiceHash"], body["encryptedInvoiceSize"]) == (sha256_b64(ciphertext), len(ciphertext))
        assert body["offlineMode"] is True and body["hashOfCorrectedInvoice"] == "oldhash"
        return httpx.Response(202, json={"referenceNumber": "I"})

    async def scenario():
        async with authenticated(handler) as c:
            assert await c.send_to_session("S", key, xml, offline=True, hash_of_corrected="oldhash") == "I"

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [None, InvoiceRejected("invalid invoice", code=450), StillProcessing("S", "I")])
def test_single_send_always_closes_session_and_preserves_result(monkeypatch, failure):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            monkeypatch.setattr(c, "send_to_session", AsyncMock(return_value="I"))
            monkeypatch.setattr(c, "wait_for_invoice", AsyncMock(return_value={"ksefNumber": "K"}, side_effect=failure))
            monkeypatch.setattr(c, "fetch_upo", AsyncMock(return_value=b"upo"))
            close = AsyncMock(side_effect=KsefError("close failed"))
            monkeypatch.setattr(c, "close_online_session", close)
            if failure:
                with pytest.raises(type(failure)) as caught:
                    await c.send_invoice(b"invoice")
                assert caught.value is failure
            else:
                result = await c.send_invoice(b"invoice")
                assert (result.ksef_number, result.session_ref, result.invoice_ref, result.upo_xml) == (
                    "K",
                    "S",
                    "I",
                    b"upo",
                )
            close.assert_awaited_once_with("S")

    asyncio.run(scenario())


def test_multiple_online_uploads_continue_after_rejection_but_stop_on_rate_limit(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            send = AsyncMock(side_effect=[KsefError("invalid"), "I"])
            monkeypatch.setattr(c, "send_to_session", send)
            close = AsyncMock()
            monkeypatch.setattr(c, "close_online_session", close)
            refs = []
            session, results = await c.send_invoices_online(
                [("bad", b"a"), ("good", b"b")], offline=True, on_refs=lambda *r: refs.append(r)
            )
            assert session == "S" and results[0][2].message == "invalid" and results[1] == ("good", "I", None)
            assert isinstance(results[0][2], client_module.NotSubmitted) and refs == [(1, "I")]
            # Limit zapytań: ten i kolejne pliki nie zostały wysłane - wynik zwracany, a nie gubiony.
            send.side_effect = ["I1", RateLimitError("wait")]
            session, results = await c.send_invoices_online([("a", b"a"), ("b", b"b"), ("c", b"c")])
            assert results[0] == ("a", "I1", None)
            assert [(name, type(error).__name__) for name, _, error in results[1:]] == [
                ("b", "NotSubmitted"),
                ("c", "NotSubmitted"),
            ]
            assert send.await_count == 4 and close.await_count == 2

    asyncio.run(scenario())


def test_export_decrypts_parts_in_order_and_matches_xml_to_metadata(monkeypatch):
    key = SymmetricKey.generate()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_metadata.json", json.dumps({"invoices": [{"ksefNumber": "K"}]}))
        archive.writestr("folder/K.XML", b"<invoice/>")
        archive.writestr("ignore.txt", "ignored")
    data = buffer.getvalue()
    parts = {"1": key.encrypt(data[:50]), "2": key.encrypt(data[50:])}
    downloads = []

    def handler(request):
        if request.url.host == "download.test":
            assert "Authorization" not in request.headers
            ordinal = request.url.path.strip("/")
            downloads.append(ordinal)
            return httpx.Response(200, content=parts[ordinal])
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["onlyMetadata"] is True
            assert body["filters"]["dateRange"] == {
                "dateType": "PermanentStorage",
                "from": iso(DAY),
                "to": iso(DAY + timedelta(days=1)),
            }
            assert body["filters"]["subjectType"] == "Subject2" and body["filters"]["currency"] == "EUR"
            return httpx.Response(202, json={"referenceNumber": "E"})
        return httpx.Response(
            200,
            json={
                "status": {"code": 200},
                "package": {
                    "parts": [{"ordinalNumber": n, "url": f"https://download.test/{n}"} for n in (2, 1)],
                    "isTruncated": True,
                    "lastPermanentStorageDate": iso(DAY),
                    "permanentStorageHwmDate": iso(DAY),
                },
            },
        )

    async def scenario():
        async with authenticated(handler) as c:
            monkeypatch.setattr(client_module.SymmetricKey, "generate", lambda: key)
            monkeypatch.setattr(c, "_encryption", AsyncMock(return_value={}))
            result = await c.export_invoices(
                "Subject2", DAY, DAY + timedelta(days=1), only_metadata=True, extra_filters={"currency": "EUR"}
            )
            assert result.metadata == [{"ksefNumber": "K"}]
            assert result.xml == {"K": b"<invoice/>"}
            assert result.is_truncated and result.hwm == DAY and result.last_permanent_storage == DAY
            assert downloads == ["1", "2"]

    asyncio.run(scenario())


def test_accepted_invoice_is_not_reported_failed_when_receipt_is_rate_limited(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            monkeypatch.setattr(c, "send_to_session", AsyncMock(return_value="I"))
            monkeypatch.setattr(c, "wait_for_invoice", AsyncMock(return_value={"ksefNumber": "K"}))
            receipt = AsyncMock(side_effect=RateLimitError("wait"))
            monkeypatch.setattr(c, "upo_by_reference", receipt)
            monkeypatch.setattr(c, "close_online_session", AsyncMock())
            result = await c.send_invoice(b"<invoice/>")
            assert result.ksef_number == "K" and result.upo_xml is None
            receipt.assert_awaited_once()  # bez kolejnych prób - i tak trafiłyby w limit

    asyncio.run(scenario())


def test_status_connection_failure_after_upload_preserves_reference_for_recovery(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            monkeypatch.setattr(c, "send_to_session", AsyncMock(return_value="I"))
            failure = KsefError("connection lost")
            monkeypatch.setattr(c, "wait_for_invoice", AsyncMock(side_effect=failure))
            monkeypatch.setattr(c, "close_online_session", AsyncMock())
            with pytest.raises(StillProcessing) as caught:
                await c.send_invoice(b"<invoice/>")
            assert (caught.value.session_ref, caught.value.invoice_ref) == ("S", "I")
            assert caught.value.__cause__ is failure
            c.close_online_session.assert_awaited_once_with("S")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "name,status_method,args",
    [
        ("wait_for_invoice", "invoice_status", ("S", "I")),
        ("wait_for_session", "session_status", ("S",)),
        ("wait_for_enrollment", "enrollment_status", ("E",)),
        ("wait_for_permission_operation", "permission_operation", ("P",)),
    ],
)
def test_polling_retries_processing_before_success(monkeypatch, name, status_method, args):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            statuses = [{"status": {"code": 100}}, {"status": {"code": 200}}]
            monkeypatch.setattr(c, status_method, AsyncMock(side_effect=statuses))
            sleep = AsyncMock()
            monkeypatch.setattr(client_module.asyncio, "sleep", sleep)
            result = await getattr(c, name)(*args)
            assert (result if name == "wait_for_permission_operation" else result["status"])["code"] == 200
            sleep.assert_awaited_once()

    asyncio.run(scenario())


def test_token_polling_and_receipt_retry_backoff(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(client_module.asyncio, "sleep", AsyncMock())
            monkeypatch.setattr(c, "token_status", AsyncMock(side_effect=[{"status": "Pending"}, {"status": "Active"}]))
            assert await c.wait_for_token("T") == {"status": "Active"}
            monkeypatch.setattr(c, "upo_by_reference", AsyncMock(side_effect=[KsefError("not ready"), b"upo"]))
            assert await c.fetch_upo("S", "I") == b"upo"
            monkeypatch.setattr(c, "upo_by_reference", AsyncMock(side_effect=KsefError("not ready")))
            assert await c.fetch_upo("S", "I", attempts=2) is None
            monkeypatch.setattr(c, "upo_by_reference", AsyncMock(side_effect=RateLimitError("wait")))
            assert await c.fetch_upo("S", "I") is None  # po przyjęciu faktury brak UPO nie jest błędem

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "operation,authorized",
    [("subject", False), ("person/remove", False), ("limits/context/session", True), ("rate-limits", True)],
)
def test_testdata_only_authenticates_operations_that_require_it(operation, authorized):
    def handler(request):
        assert request.url.path == "/v2/testdata/" + operation
        assert ("Authorization" in request.headers) is authorized
        assert json.loads(request.content) == {"nip": "test"}
        return httpx.Response(204)

    async def scenario():
        async with authenticated(handler) as c:
            await c.testdata(operation, {"nip": "test"}, method="DELETE")

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [200, 450])
def test_export_empty_package_or_terminal_error(monkeypatch, status):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(202, json={"referenceNumber": "E"})) as c:
            monkeypatch.setattr(c, "_encryption", AsyncMock(return_value={}))
            monkeypatch.setattr(c, "export_status", AsyncMock(return_value={"status": {"code": status}}))
            if status == 200:
                package = await c.export_invoices("Subject1", DAY)
                assert package.metadata == [] and package.xml == {}
            else:
                with pytest.raises(KsefError) as caught:
                    await c.export_invoices("Subject1", DAY)
                assert caught.value.code == 450

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "exception,interrupted",
    [
        (httpx.ConnectError("offline"), False),
        (httpx.ConnectTimeout("no route"), False),
        (httpx.ReadTimeout("slow"), True),
        (httpx.RemoteProtocolError("dropped"), True),
    ],
)
def test_transport_failures_tell_whether_the_request_may_have_arrived(exception, interrupted):
    def handler(request):
        raise exception

    async def scenario():
        async with authenticated(handler) as c:
            with pytest.raises(KsefError) as caught:
                await c.context_limits()
            assert isinstance(caught.value, client_module.ConnectionInterrupted) is interrupted

    asyncio.run(scenario())


def test_interrupted_upload_is_an_unknown_outcome_with_the_session_reference(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            sessions = []
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            failure = client_module.ConnectionInterrupted("dropped")
            monkeypatch.setattr(c, "send_to_session", AsyncMock(side_effect=failure))
            monkeypatch.setattr(c, "close_online_session", AsyncMock())
            with pytest.raises(client_module.OutcomeUnknown) as caught:
                await c.send_invoice(b"<invoice/>", on_refs=lambda **refs: sessions.append(refs))
            assert sessions == [{"session_ref": "S"}]
            assert (caught.value.session_ref, caught.value.invoice_ref) == ("S", "")
            assert caught.value.__cause__ is failure
            c.close_online_session.assert_awaited_once_with("S")

    asyncio.run(scenario())


@pytest.mark.parametrize("exception", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline")])
def test_interrupted_batch_close_may_still_be_processed(monkeypatch, exception):
    def handler(request):
        raise exception

    async def scenario():
        async with authenticated(handler) as c:
            opened = {"referenceNumber": "S", "partUploadRequests": []}
            monkeypatch.setattr(c, "_with_key_retry", AsyncMock(return_value=opened))
            with pytest.raises(KsefError) as caught:
                await c.send_batch([("a.xml", b"<a/>")])
            unknown = isinstance(caught.value, client_module.OutcomeUnknown)
            assert unknown is isinstance(exception, httpx.ReadTimeout)
            if unknown:
                assert caught.value.session_ref == "S"

    asyncio.run(scenario())


def test_session_invoice_lookup_prefers_hash_then_file_name_then_number():
    items = [
        {"referenceNumber": "N", "invoiceNumber": "FV/1"},
        {"referenceNumber": "F", "invoiceFileName": "a.xml"},
        {"referenceNumber": "H", "invoiceHash": "hash"},
    ]
    find = client_module.find_session_invoice
    assert find(items, invoice_hash="hash", file_name="a.xml", invoice_number="FV/1")["referenceNumber"] == "H"
    assert find(items, invoice_hash="other", file_name="a.xml", invoice_number="FV/1")["referenceNumber"] == "F"
    assert find(items, invoice_hash=None, file_name=None, invoice_number="FV/1")["referenceNumber"] == "N"
    assert find(items, invoice_hash="x", file_name="y.xml", invoice_number="FV/9") is None


def test_references_are_reported_as_soon_as_known(monkeypatch):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            refs = []
            monkeypatch.setattr(c, "open_online_session", AsyncMock(return_value={"referenceNumber": "S"}))
            monkeypatch.setattr(c, "send_to_session", AsyncMock(return_value="I"))
            monkeypatch.setattr(c, "wait_for_invoice", AsyncMock(side_effect=StillProcessing("S", "I")))
            monkeypatch.setattr(c, "close_online_session", AsyncMock())
            with pytest.raises(StillProcessing):
                await c.send_invoice(b"<invoice/>", on_refs=lambda **r: refs.append(r))
            assert refs == [{"session_ref": "S"}, {"invoice_ref": "I"}]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "stage,failure,expected",
    [
        ("auth", AuthError("token expired", http_status=401), "not_submitted"),
        ("session", KsefError("no connection"), "not_submitted"),
        ("session", client_module.ConnectionInterrupted("timeout"), "not_submitted"),
        ("upload", KsefError("bad request", http_status=400), "not_submitted"),
        ("upload", RateLimitError("wait", http_status=429), "not_submitted"),
        ("upload", KsefError("server error", http_status=503), "unknown"),
        ("upload", client_module.ConnectionInterrupted("dropped"), "unknown"),
    ],
)
def test_send_failures_are_classified_by_whether_the_invoice_could_have_arrived(monkeypatch, stage, failure, expected):
    async def scenario():
        async with authenticated(lambda request: httpx.Response(200)) as c:
            monkeypatch.setattr(c, "ensure_auth", AsyncMock(side_effect=failure if stage == "auth" else None))
            opened = AsyncMock(
                return_value={"referenceNumber": "S"}, side_effect=failure if stage == "session" else None
            )
            monkeypatch.setattr(c, "open_online_session", opened)
            monkeypatch.setattr(c, "send_to_session", AsyncMock(side_effect=failure))
            monkeypatch.setattr(c, "close_online_session", AsyncMock())
            with pytest.raises(KsefError) as caught:
                await c.send_invoice(b"<invoice/>")
            if expected == "not_submitted":
                assert isinstance(caught.value, client_module.NotSubmitted)
                assert caught.value.message == failure.message and caught.value.__cause__ is failure
            else:
                assert isinstance(caught.value, client_module.OutcomeUnknown)
                assert caught.value.session_ref == "S"

    asyncio.run(scenario())
