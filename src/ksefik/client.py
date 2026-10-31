"""Asynchroniczny klient KSeF API 2.0.

Obejmuje wszystkie obszary API: uwierzytelnianie (token KSeF albo podpis XAdES), sesje interaktywne i wsadowe,
UPO, pobieranie i eksport faktur, tokeny, uprawnienia, certyfikaty KSeF, limity, identyfikatory zbiorcze,
dostawców Peppol oraz dane testowe (tylko środowisko testowe).
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import re
import time
import zipfile
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .config import ENVIRONMENTS, __version__
from .crypto import SymmetricKey, b64, rsa_oaep_encrypt, sha256_b64
from .xades import Signer, auth_token_request, sign_xades

FORM_CODE_FA3 = {"systemCode": "FA (3)", "schemaVersion": "1-0E", "value": "FA"}
BATCH_PART_SIZE = 100 * 1000 * 1000  # maksymalna część paczki przed szyfrowaniem

Progress = Callable[[str, str], Awaitable[None] | None]
"""Wywoływane jako progress(krok, stan), gdzie stan to 'start', 'ok' albo opis postępu."""

# Zapytania do KSeF (bez nagłówków i treści - nigdy tokenów); włączane np. w testach integracyjnych.
log = logging.getLogger("ksefik.client")


class KsefError(Exception):
    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        code: int | None = None,
        details: list[str] | None = None,
        retry_after: int | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.http_status = http_status
        self.code = code
        self.details = [d for d in (details or []) if d]
        self.retry_after = retry_after
        self.extensions = extensions or {}

    def human(self) -> str:
        lines = [self.message]
        lines += [f"• {d}" for d in self.details]
        return "\n".join(lines)


class RateLimitError(KsefError):
    pass


class AuthError(KsefError):
    pass


class CredentialsRejected(AuthError):
    """KSeF odrzucił dane logowania (np. unieważniony lub błędny token) - trzeba podać inne."""


class InvoiceRejected(KsefError):
    """KSeF odrzucił fakturę (np. błąd schematu, duplikat numeru)."""


class ConnectionInterrupted(KsefError):
    """Połączenie zerwane po wysłaniu żądania - nie wiadomo, czy KSeF je otrzymał."""


class NotSubmitted(KsefError):
    """Faktura na pewno nie dotarła do KSeF (błąd przed jej przesłaniem) - można ją bezpiecznie wysłać ponownie."""

    @classmethod
    def wrap(cls, exc: KsefError) -> NotSubmitted:
        return cls(
            exc.message,
            http_status=exc.http_status,
            code=exc.code,
            details=exc.details,
            retry_after=exc.retry_after,
            extensions=exc.extensions,
        )


class OperationPending(KsefError):
    """KSeF przyjął wniosek (np. o zmianę uprawnień), ale nie skończył go przetwarzać - wynik sprawdzisz po numerze."""

    def __init__(self, reference: str, status: dict[str, Any] | None = None) -> None:
        super().__init__("Wniosek jest w trakcie przetwarzania w KSeF. Nie potwierdzono jeszcze jego wyniku.")
        self.reference = reference
        self.status = status or {}


class EnrollmentRejected(KsefError):
    """KSeF odrzucił wniosek o certyfikat - certyfikatu nie będzie."""


class StillProcessing(KsefError):
    """Faktura wysłana, ale KSeF nie skończył jej przetwarzać w czasie oczekiwania."""

    def __init__(
        self,
        session_ref: str,
        invoice_ref: str,
        message: str = "KSeF wciąż przetwarza fakturę. Status sprawdzisz później w aplikacji.",
    ) -> None:
        super().__init__(message)
        self.session_ref = session_ref
        self.invoice_ref = invoice_ref


class OutcomeUnknown(StillProcessing):
    """Połączenie zerwane w trakcie wysyłania faktury: KSeF mógł ją otrzymać, ale nie znamy jej numeru."""

    def __init__(self, session_ref: str) -> None:
        super().__init__(
            session_ref,
            "",
            "Połączenie z KSeF zostało przerwane w trakcie wysyłki. Wynik sprawdzisz później w aplikacji.",
        )


def find_session_invoice(
    items: list[dict[str, Any]], *, invoice_hash: str | None, file_name: str | None, invoice_number: str | None
) -> dict[str, Any] | None:
    """Faktura z listy faktur sesji, gdy nie znamy jej numeru referencyjnego (skrót XML, nazwa pliku albo numer)."""
    keys = (("invoiceHash", invoice_hash), ("invoiceFileName", file_name), ("invoiceNumber", invoice_number))
    for key, value in keys:
        if value and (found := next((i for i in items if i.get(key) == value), None)):
            return found
    return None


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # .NET potrafi zwrócić 7 cyfr ułamka sekundy - Python akceptuje maksymalnie 6.
    value = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def allowed_ips_policy(entries: Iterable[str]) -> dict[str, list[str]]:
    """Dzieli listę z profilu na adresy, zakresy (a-b) i maski CIDR - format AuthorizationPolicy."""
    policy: dict[str, list[str]] = {"ip4Addresses": [], "ip4Ranges": [], "ip4Masks": []}
    for entry in entries:
        entry = entry.strip()
        key = "ip4Masks" if "/" in entry else "ip4Ranges" if "-" in entry else "ip4Addresses"
        policy[key].append(entry)
    return {k: v for k, v in policy.items() if v}


@dataclass
class TokenCredentials:
    token: str


@dataclass
class CertificateCredentials:
    signer: Signer
    subject_type: str = "certificateSubject"  # albo certificateFingerprint


Credentials = TokenCredentials | CertificateCredentials


@dataclass
class SendResult:
    ksef_number: str
    session_ref: str
    invoice_ref: str
    acquisition_date: datetime | None
    upo_xml: bytes | None


@dataclass
class BatchResult:
    session_ref: str
    status: dict[str, Any]
    invoices: list[dict[str, Any]]


@dataclass
class ExportPackage:
    metadata: list[dict[str, Any]] = field(default_factory=list)
    xml: dict[str, bytes] = field(default_factory=dict)
    is_truncated: bool = False
    last_permanent_storage: datetime | None = None
    hwm: datetime | None = None


async def report(progress: Progress | None, step: str, state: str) -> None:
    if progress is None:
        return
    result = progress(step, state)
    if asyncio.iscoroutine(result):
        await result


def _identifier(kind: str, value: str | None = None) -> dict[str, str]:
    data = {"type": kind}
    if value:
        data["value"] = value
    return data


def person_details(first_name: str, last_name: str) -> dict[str, Any]:
    """Dane osoby identyfikowanej NIP-em lub PESEL-em (subjectDetails przy nadawaniu uprawnień)."""
    return {
        "subjectDetailsType": "PersonByIdentifier",
        "personById": {"firstName": first_name.strip(), "lastName": last_name.strip()},
    }


class KsefClient:
    def __init__(
        self,
        base_url: str,
        context_id: str,
        credentials: Credentials,
        *,
        context_type: str = "Nip",
        allowed_ips: Iterable[str] = (),
        verify_certificate_chain: bool = False,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url
        self.context_type = context_type
        self.context_id = context_id
        self.credentials = credentials
        self.allowed_ips = allowed_ips_policy(allowed_ips)
        self.verify_certificate_chain = verify_certificate_chain
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=httpx.Timeout(timeout, connect=15.0),
            headers={"Accept": "application/json", "User-Agent": f"ksefik/{__version__}"},
            transport=transport,
        )
        self._transport = transport
        # Wołane, gdy KSeF odrzuci dane logowania - aplikacja pokazuje wtedy „wymaga logowania”.
        self.on_credentials_rejected: Callable[[], Any] | None = None
        self._access: tuple[str, datetime] | None = None
        self._refresh: tuple[str, datetime] | None = None
        self._public_keys: list[dict[str, Any]] | None = None
        self._auth_lock = asyncio.Lock()
        self.auth_reference: str | None = None  # numer sesji uwierzytelnienia bieżącego logowania

    @property
    def nip(self) -> str:
        return self.context_id

    async def __aenter__(self) -> KsefClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ HTTP

    async def _call(
        self,
        method: str,
        path: str,
        *,
        auth: str | None = "access",
        json_body: Any = None,
        content: bytes | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        accept: str | None = None,
        _retried: bool = False,
    ) -> httpx.Response:
        all_headers: dict[str, str] = dict(headers or {})
        if auth == "access":
            await self.ensure_auth()
            assert self._access
            all_headers["Authorization"] = f"Bearer {self._access[0]}"
        elif auth:
            all_headers["Authorization"] = f"Bearer {auth}"
        if accept:
            all_headers["Accept"] = accept
        if params:
            params = {k: v for k, v in params.items() if v is not None}
        started = time.monotonic()
        try:
            response = await self._http.request(
                method, path, json=json_body, content=content, params=params, headers=all_headers
            )
        except httpx.HTTPError as exc:
            log.warning("%s %s -> %s po %.2fs", method, path, exc.__class__.__name__, time.monotonic() - started)
            raise self._transport_error(exc) from exc
        elapsed = time.monotonic() - started
        if response.is_success:
            log.debug("%s %s -> %s (%.2fs)", method, path, response.status_code, elapsed)
        else:
            log.warning(
                "%s %s -> %s (%.2fs) %s", method, path, response.status_code, elapsed, response.text[:500].strip()
            )

        if response.status_code == 401 and auth == "access" and not _retried:
            self._access = None
            return await self._call(
                method,
                path,
                auth=auth,
                json_body=json_body,
                content=content,
                params=params,
                headers=headers,
                accept=accept,
                _retried=True,
            )
        if response.is_success:
            return response
        raise self._error(response)

    @staticmethod
    def _transport_error(exc: httpx.HTTPError) -> KsefError:
        if isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout)):
            return KsefError("KSeF nie odpowiedział w wyznaczonym czasie. Spróbuj ponownie za chwilę.")
        if isinstance(exc, httpx.ConnectError):
            return KsefError(f"Brak połączenia z KSeF ({exc.__class__.__name__}). Sprawdź internet.")
        # Żądanie mogło już dotrzeć do KSeF - dla wysyłki faktury to „wynik nieznany”, nie „nie wysłano”.
        if isinstance(exc, httpx.TimeoutException):
            return ConnectionInterrupted("KSeF nie odpowiedział w wyznaczonym czasie. Spróbuj ponownie za chwilę.")
        if isinstance(exc, httpx.TransportError):
            return ConnectionInterrupted(f"Brak połączenia z KSeF ({exc.__class__.__name__}). Sprawdź internet.")
        raise exc

    async def _json(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._call(method, path, **kwargs)
        if not response.content:
            return {}
        return response.json()

    async def _continuation(
        self, path: str, key: str, *, method: str = "GET", limit: int | None = None, **kwargs: Any
    ) -> list[dict[str, Any]]:
        """Pobiera kolejne strony wyników stronicowanych tokenem `x-continuation-token`."""
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            headers = {"x-continuation-token": token} if token else None
            data = await self._json(method, path, headers=headers, **kwargs)
            items += data.get(key) or []
            token = data.get("continuationToken")
            if not token or (limit and len(items) >= limit):
                return items[:limit] if limit else items

    async def _pages(
        self,
        path: str,
        key: str,
        *,
        method: str = "POST",
        json_body: Any = None,
        page_size: int = 100,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Pobiera kolejne strony wyników stronicowanych numerem strony (`pageOffset` + `hasMore`)."""
        items: list[dict[str, Any]] = []
        page = 0
        while True:
            query = {**(params or {}), "pageOffset": page, "pageSize": page_size}
            data = await self._json(method, path, json_body=json_body, params=query)
            items += data.get(key) or []
            if not data.get("hasMore") or (limit and len(items) >= limit):
                return items[:limit] if limit else items
            page += 1

    @staticmethod
    def _error(response: httpx.Response) -> KsefError:
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        if status == 429:
            retry_after = None
            with contextlib.suppress(ValueError):
                retry_after = int(response.headers.get("Retry-After", ""))
            details = (body.get("status") or {}).get("details") or []
            wait = f" Spróbuj ponownie za {retry_after} s." if retry_after else ""
            return RateLimitError(
                f"KSeF chwilowo ogranicza liczbę zapytań.{wait}",
                http_status=status,
                details=details,
                retry_after=retry_after,
            )

        exception = body.get("exception") or {}
        detail_list = exception.get("exceptionDetailList") or []
        if detail_list:
            first = detail_list[0]
            details: list[str] = []
            for item in detail_list:
                if item is not first and item.get("exceptionDescription"):
                    details.append(item["exceptionDescription"])
                details += item.get("details") or []
            return KsefError(
                first.get("exceptionDescription") or f"Błąd KSeF (HTTP {status})",
                http_status=status,
                code=first.get("exceptionCode"),
                details=details,
            )
        if body.get("title") or body.get("detail"):
            errors = [e.get("description") or e.get("message") or str(e) for e in body.get("errors") or []]
            return KsefError(body.get("detail") or body.get("title"), http_status=status, details=errors)
        if status >= 500:
            return KsefError(
                f"Usługa KSeF jest chwilowo niedostępna (HTTP {status}). Spróbuj ponownie za chwilę.",
                http_status=status,
            )
        if status == 401:
            return AuthError("Sesja w KSeF wygasła lub dane logowania są nieprawidłowe.", http_status=status)
        if status == 403:
            return AuthError("Brak uprawnień do tej operacji w KSeF.", http_status=status)
        text = response.text.strip()
        details = [text[:300]] if text and "html" not in response.headers.get("content-type", "") else []
        return KsefError(f"KSeF zwrócił błąd HTTP {status}.", http_status=status, details=details)

    # ------------------------------------------------------- klucze publiczne

    async def public_key_certificates(self) -> list[dict[str, Any]]:
        return await self._json("GET", "/security/public-key-certificates", auth=None)

    async def _public_key(self, usage: str, *, refresh: bool = False) -> dict[str, Any]:
        if self._public_keys is None or refresh:
            self._public_keys = await self.public_key_certificates()
        now = datetime.now(UTC)
        candidates = [
            k
            for k in self._public_keys
            if usage in k.get("usage", [])
            and (parse_dt(k.get("validFrom")) or now) <= now <= (parse_dt(k.get("validTo")) or now)
        ]
        if not candidates:
            raise KsefError(f"KSeF nie udostępnia ważnego klucza publicznego ({usage}).")
        return max(candidates, key=lambda k: parse_dt(k["validFrom"]) or now)

    async def _with_key_retry(self, operation: Callable[[bool], Awaitable[Any]]) -> Any:
        """Ponawia operację z odświeżonymi kluczami, gdy KSeF zgłosi nieznany/wycofany klucz (21470)."""
        try:
            return await operation(False)
        except KsefError as exc:
            if exc.code != 21470:
                raise
            return await operation(True)

    async def _encryption(self, key: SymmetricKey, refresh_keys: bool) -> dict[str, str]:
        pk = await self._public_key("SymmetricKeyEncryption", refresh=refresh_keys)
        return {
            "encryptedSymmetricKey": b64(rsa_oaep_encrypt(pk["certificate"], key.key)),
            "initializationVector": b64(key.iv),
            "publicKeyId": pk["publicKeyId"],
        }

    # --------------------------------------------------------- uwierzytelnienie

    async def ensure_auth(self) -> None:
        async with self._auth_lock:
            now = datetime.now(UTC)
            if self._access and self._access[1] - now > timedelta(seconds=60):
                return
            if self._refresh and self._refresh[1] - now > timedelta(seconds=60):
                try:
                    data = (await self._json("POST", "/auth/token/refresh", auth=self._refresh[0]))["accessToken"]
                    self._access = (data["token"], parse_dt(data["validUntil"]) or now)
                    return
                except KsefError:
                    self._refresh = None
            await self._authenticate()

    async def _start_token_auth(self, challenge: dict[str, Any], token: str) -> dict[str, Any]:
        async def init(refresh_keys: bool) -> dict[str, Any]:
            key = await self._public_key("KsefTokenEncryption", refresh=refresh_keys)
            encrypted = rsa_oaep_encrypt(key["certificate"], f"{token}|{challenge['timestampMs']}".encode())
            body: dict[str, Any] = {
                "challenge": challenge["challenge"],
                "contextIdentifier": {"type": self.context_type, "value": self.context_id},
                "encryptedToken": b64(encrypted),
                "publicKeyId": key["publicKeyId"],
            }
            if self.allowed_ips:
                body["authorizationPolicy"] = {"allowedIps": self.allowed_ips}
            return await self._json("POST", "/auth/ksef-token", auth=None, json_body=body)

        return await self._with_key_retry(init)

    async def _start_xades_auth(self, challenge: dict[str, Any], credentials: CertificateCredentials) -> dict[str, Any]:
        request = auth_token_request(
            challenge["challenge"],
            self.context_type,
            self.context_id,
            subject_type=credentials.subject_type,
            allowed_ips=self.allowed_ips,
        )
        signed = sign_xades(request, credentials.signer)
        return await self._json(
            "POST",
            "/auth/xades-signature",
            auth=None,
            content=signed,
            headers={"Content-Type": "application/xml"},
            params={"verifyCertificateChain": "true" if self.verify_certificate_chain else None},
        )

    async def _authenticate(self) -> None:
        challenge = await self._json("POST", "/auth/challenge", auth=None)
        try:
            if isinstance(self.credentials, TokenCredentials):
                started = await self._start_token_auth(challenge, self.credentials.token)
            else:
                started = await self._start_xades_auth(challenge, self.credentials)
        except KsefError as exc:
            message = f"Nie udało się zalogować do KSeF: {exc.message}"
            rejected = exc.http_status is not None and 400 <= exc.http_status < 500 and exc.http_status != 429
            if rejected:
                self._credentials_rejected()
                raise CredentialsRejected(message, http_status=exc.http_status, details=exc.details) from exc
            raise AuthError(message, details=exc.details) from exc

        auth_token = started["authenticationToken"]["token"]
        reference = started["referenceNumber"]
        loop = asyncio.get_running_loop()
        # Przy podpisie kwalifikowanym KSeF sprawdza status certyfikatu u wystawcy (OCSP) - to może potrwać.
        deadline = loop.time() + 180
        delay = 0.5
        while True:
            status = (await self._json("GET", f"/auth/{reference}", auth=auth_token))["status"]
            if status["code"] != 100:
                break
            if loop.time() > deadline:
                raise AuthError("KSeF zbyt długo weryfikuje logowanie. Spróbuj ponownie.")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 3.0)

        if status["code"] != 200:
            self._credentials_rejected()
            raise CredentialsRejected(
                f"Logowanie odrzucone: {status.get('description', 'nieznany powód')}",
                code=status["code"],
                details=status.get("details") or [],
            )
        tokens = await self._json("POST", "/auth/token/redeem", auth=auth_token)
        now = datetime.now(UTC)
        self._access = (tokens["accessToken"]["token"], parse_dt(tokens["accessToken"]["validUntil"]) or now)
        self._refresh = (tokens["refreshToken"]["token"], parse_dt(tokens["refreshToken"]["validUntil"]) or now)
        self.auth_reference = reference

    def _credentials_rejected(self) -> None:
        if self.on_credentials_rejected:
            self.on_credentials_rejected()

    async def auth_sessions(self) -> list[dict[str, Any]]:
        """Aktywne sesje uwierzytelnienia w bieżącym kontekście."""
        return await self._continuation("/auth/sessions", "items", params={"pageSize": 100})

    async def revoke_current_session(self) -> None:
        await self._call("DELETE", "/auth/sessions/current")
        self._access = self._refresh = None

    async def revoke_auth_session(self, reference: str) -> None:
        await self._call("DELETE", f"/auth/sessions/{reference}")

    # ---------------------------------------------------------- sesja interaktywna

    async def open_online_session(self, key: SymmetricKey, form_code: dict[str, str] | None = None) -> dict[str, Any]:
        async def open_session(refresh_keys: bool) -> dict[str, Any]:
            body = {"formCode": form_code or FORM_CODE_FA3, "encryption": await self._encryption(key, refresh_keys)}
            return await self._json("POST", "/sessions/online", json_body=body)

        return await self._with_key_retry(open_session)

    async def send_to_session(
        self,
        session_ref: str,
        key: SymmetricKey,
        xml: bytes,
        *,
        offline: bool = False,
        hash_of_corrected: str | None = None,
    ) -> str:
        encrypted = key.encrypt(xml)
        body: dict[str, Any] = {
            "invoiceHash": sha256_b64(xml),
            "invoiceSize": len(xml),
            "encryptedInvoiceHash": sha256_b64(encrypted),
            "encryptedInvoiceSize": len(encrypted),
            "encryptedInvoiceContent": b64(encrypted),
        }
        if offline:
            body["offlineMode"] = True
        if hash_of_corrected:
            body["hashOfCorrectedInvoice"] = hash_of_corrected
        response = await self._json("POST", f"/sessions/online/{session_ref}/invoices", json_body=body)
        return response["referenceNumber"]

    async def close_online_session(self, session_ref: str) -> None:
        await self._call("POST", f"/sessions/online/{session_ref}/close")

    async def send_invoice(
        self, xml: bytes, progress: Progress | None = None, on_refs: Callable[..., Any] | None = None
    ) -> SendResult:
        """Wysyła jedną fakturę w sesji interaktywnej.

        `on_refs(session_ref=…)` i `on_refs(invoice_ref=…)` są wołane, gdy tylko numery są znane - żeby po awarii
        dało się odnaleźć wysyłkę. Błędy: NotSubmitted (nic nie dotarło do KSeF), OutcomeUnknown (połączenie
        przerwane w trakcie przesyłania), StillProcessing (faktura w KSeF, wynik później), InvoiceRejected.
        """
        try:
            await report(progress, "auth", "start")
            await self.ensure_auth()
            await report(progress, "auth", "ok")
            await report(progress, "session", "start")
            key = SymmetricKey.generate()
            session_ref = (await self.open_online_session(key))["referenceNumber"]
        except KsefError as exc:
            raise NotSubmitted.wrap(exc) from exc
        if on_refs:
            on_refs(session_ref=session_ref)  # zapis przed wysłaniem faktury - po awarii wynik znajdziemy w tej sesji
        await report(progress, "session", "ok")

        try:
            await report(progress, "upload", "start")
            try:
                invoice_ref = await self.send_to_session(session_ref, key, xml)
            except ConnectionInterrupted as exc:
                raise OutcomeUnknown(session_ref) from exc
            except KsefError as exc:
                if (exc.http_status or 0) >= 500:
                    raise OutcomeUnknown(session_ref) from exc  # błąd serwera - KSeF mógł fakturę przyjąć
                raise NotSubmitted.wrap(exc) from exc  # KSeF odmówił przyjęcia żądania
            if on_refs:
                on_refs(invoice_ref=invoice_ref)
            await report(progress, "upload", "ok")

            await report(progress, "processing", "start")
            try:
                status = await self.wait_for_invoice(session_ref, invoice_ref)
            except (InvoiceRejected, StillProcessing):
                raise
            except KsefError as exc:
                # XML został już wysłany. Błąd odczytu statusu nie oznacza odrzucenia;
                # zachowaj referencje potrzebne do późniejszego sprawdzenia.
                raise StillProcessing(session_ref, invoice_ref) from exc
            await report(progress, "processing", "ok")

            # Faktura jest już przyjęta; brak UPO nie zmienia wyniku wysyłki.
            await report(progress, "upo", "start")
            upo = await self.fetch_upo(session_ref, invoice_ref)
            await report(progress, "upo", "ok" if upo else "brak - pobierzesz później")
        finally:
            await report(progress, "close", "start")
            try:
                await self.close_online_session(session_ref)
                await report(progress, "close", "ok")
            except KsefError:
                # Sesja i tak wygaśnie sama; to nie wpływa na przyjętą fakturę.
                await report(progress, "close", "pominięto")

        return SendResult(
            ksef_number=status["ksefNumber"],
            session_ref=session_ref,
            invoice_ref=invoice_ref,
            acquisition_date=parse_dt(status.get("acquisitionDate")),
            upo_xml=upo,
        )

    async def send_invoices_online(
        self,
        files: list[tuple[str, bytes]],
        progress: Progress | None = None,
        *,
        offline: bool = False,
        on_session: Callable[[str], Any] | None = None,
        on_refs: Callable[[int, str], Any] | None = None,
    ) -> tuple[str, list[tuple[str, str | None, KsefError | None]]]:
        """Wysyła kilka faktur w jednej sesji interaktywnej. Zwraca (sesja, [(plik, nr referencyjny, błąd)]).

        Błąd pliku to NotSubmitted (KSeF go nie przyjął, np. limit zapytań - wtedy także pozostałe pliki)
        albo OutcomeUnknown (połączenie przerwane w trakcie przesyłania). `on_refs(pozycja, nr)` po każdym
        przesłanym pliku (pozycja na liście `files`, od 0).
        """
        key = SymmetricKey.generate()
        await report(progress, "session", "start")
        try:
            session_ref = (await self.open_online_session(key))["referenceNumber"]
        except KsefError as exc:
            raise NotSubmitted.wrap(exc) from exc
        if on_session:
            on_session(session_ref)
        await report(progress, "session", "ok")
        results: list[tuple[str, str | None, KsefError | None]] = []
        try:
            for n, (name, xml) in enumerate(files, 1):
                await report(progress, "upload", f"{n}/{len(files)}: {name}")
                try:
                    reference = await self.send_to_session(session_ref, key, xml, offline=offline)
                except RateLimitError as exc:
                    # Kolejne pliki i tak trafiłyby w limit - nie wysłano ani tego, ani pozostałych.
                    results += [(rest, None, NotSubmitted.wrap(exc)) for rest, _ in files[n - 1 :]]
                    break
                except ConnectionInterrupted:
                    results.append((name, None, OutcomeUnknown(session_ref)))
                    continue
                except KsefError as exc:
                    error = OutcomeUnknown(session_ref) if (exc.http_status or 0) >= 500 else NotSubmitted.wrap(exc)
                    results.append((name, None, error))
                    continue
                if on_refs:
                    on_refs(n - 1, reference)
                results.append((name, reference, None))
            await report(progress, "upload", "ok")
        finally:
            await report(progress, "close", "start")
            try:
                await self.close_online_session(session_ref)
                await report(progress, "close", "ok")
            except KsefError:
                await report(progress, "close", "pominięto")
        return session_ref, results

    # ---------------------------------------------------------------- sesja wsadowa

    async def send_batch(
        self,
        files: list[tuple[str, bytes]],
        progress: Progress | None = None,
        *,
        offline: bool = False,
        on_session: Callable[[str], Any] | None = None,
    ) -> str:
        """Wysyłka wsadowa: paczka ZIP dzielona na części do 100 MB, szyfrowana AES-256. Zwraca numer sesji.

        KSeF przetwarza paczkę dopiero po zamknięciu sesji: błąd wcześniej to NotSubmitted, przerwane
        zamykanie - OutcomeUnknown.
        """
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, xml in files:
                zf.writestr(name if name.lower().endswith(".xml") else f"{name}.xml", xml)
        package = archive.getvalue()
        count = max(1, -(-len(package) // BATCH_PART_SIZE))
        size = -(-len(package) // count)
        key = SymmetricKey.generate()
        parts = [key.encrypt(package[i * size : (i + 1) * size]) for i in range(count)]

        async def open_batch(refresh_keys: bool) -> dict[str, Any]:
            body: dict[str, Any] = {
                "formCode": FORM_CODE_FA3,
                "batchFile": {
                    "fileSize": len(package),
                    "fileHash": sha256_b64(package),
                    "fileParts": [
                        {"ordinalNumber": n, "fileSize": len(part), "fileHash": sha256_b64(part)}
                        for n, part in enumerate(parts, 1)
                    ],
                },
                "encryption": await self._encryption(key, refresh_keys),
            }
            if offline:
                body["offlineMode"] = True
            return await self._json("POST", "/sessions/batch", json_body=body)

        await report(progress, "session", "start")
        try:
            opened = await self._with_key_retry(open_batch)
        except KsefError as exc:
            raise NotSubmitted.wrap(exc) from exc
        session_ref = opened["referenceNumber"]
        if on_session:
            on_session(session_ref)
        await report(progress, "session", "ok")

        await report(progress, "upload", "start")
        async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0), transport=self._transport) as up:
            for request in sorted(opened["partUploadRequests"], key=lambda r: r["ordinalNumber"]):
                part = parts[request["ordinalNumber"] - 1]
                await report(progress, "upload", f"część {request['ordinalNumber']}/{len(parts)}")
                try:
                    response = await up.request(
                        request.get("method", "PUT"), request["url"], content=part, headers=request.get("headers")
                    )
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    # Sesja nie zostanie zamknięta, więc KSeF nie przetworzy paczki.
                    raise NotSubmitted(f"Nie udało się wysłać części paczki: {exc}") from exc
        await report(progress, "upload", "ok")

        await report(progress, "close", "start")
        try:
            await self._call("POST", f"/sessions/batch/{session_ref}/close")
        except ConnectionInterrupted as exc:
            # Zamknięcie sesji mogło dotrzeć do KSeF, a wtedy paczka zostanie przetworzona.
            raise OutcomeUnknown(session_ref) from exc
        except KsefError as exc:
            if (exc.http_status or 0) >= 500:
                raise OutcomeUnknown(session_ref) from exc
            raise NotSubmitted.wrap(exc) from exc
        await report(progress, "close", "ok")
        return session_ref

    async def wait_for_session(self, session_ref: str, timeout: float = 600.0) -> dict[str, Any]:
        """Czeka, aż KSeF przetworzy sesję (status 200) i zwraca jej status."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        delay = 2.0
        while True:
            status = await self.session_status(session_ref)
            code = status["status"]["code"]
            if code == 200:
                return status
            if code >= 400:
                raise KsefError(
                    f"Sesja zakończona błędem: {status['status'].get('description', '')} (kod {code})",
                    code=code,
                    details=status["status"].get("details") or [],
                )
            if loop.time() > deadline:
                raise KsefError("KSeF wciąż przetwarza sesję - sprawdź jej status później w zakładce Sesje.")
            await asyncio.sleep(delay)
            delay = min(delay * 1.3, 10.0)

    # ------------------------------------------------------------------- sesje

    async def list_sessions(
        self,
        session_type: str = "Online",
        *,
        statuses: list[str] | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        reference: str | None = None,
        limit: int | None = 200,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "sessionType": session_type,
            "pageSize": 100,
            "referenceNumber": reference,
            "dateCreatedFrom": iso(created_from) if created_from else None,
            "dateCreatedTo": iso(created_to) if created_to else None,
            "statuses": statuses,
        }
        return await self._continuation("/sessions", "sessions", params=params, limit=limit)

    async def session_status(self, session_ref: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{session_ref}")

    async def session_invoices(self, session_ref: str, *, failed_only: bool = False) -> list[dict[str, Any]]:
        path = f"/sessions/{session_ref}/invoices" + ("/failed" if failed_only else "")
        return await self._continuation(path, "invoices", params={"pageSize": 1000})

    async def invoice_status(self, session_ref: str, invoice_ref: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{session_ref}/invoices/{invoice_ref}")

    async def wait_for_invoice(self, session_ref: str, invoice_ref: str, timeout: float = 180.0) -> dict[str, Any]:
        """Czeka na zakończenie przetwarzania. Zwraca status 200 albo rzuca InvoiceRejected/StillProcessing."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        delay = 1.0
        while True:
            data = await self.invoice_status(session_ref, invoice_ref)
            code = data["status"]["code"]
            if code == 200:
                return data
            if code not in (100, 150):
                raise InvoiceRejected(
                    f"KSeF odrzucił fakturę: {data['status'].get('description', '')} (kod {code})",
                    code=code,
                    details=data["status"].get("details") or [],
                    extensions=data["status"].get("extensions") or {},
                )
            if loop.time() > deadline:
                raise StillProcessing(session_ref, invoice_ref)
            await asyncio.sleep(delay)
            delay = min(delay * 1.3, 5.0)

    async def fetch_upo(self, session_ref: str, invoice_ref: str, attempts: int = 5) -> bytes | None:
        """UPO przyjętej faktury (po numerze referencyjnym). Zwraca None, gdy UPO nie jest jeszcze dostępne.

        Nigdy nie rzuca: brak potwierdzenia nie zmienia tego, że faktura została przyjęta.
        """
        for attempt in range(attempts):
            try:
                return await self.upo_by_reference(session_ref, invoice_ref)
            except RateLimitError:
                return None  # kolejne próby i tak trafiłyby w limit - UPO pobierzesz później
            except KsefError:
                if attempt < attempts - 1:
                    await asyncio.sleep(1.0 + attempt)
        return None

    async def upo_by_reference(self, session_ref: str, invoice_ref: str) -> bytes:
        path = f"/sessions/{session_ref}/invoices/{invoice_ref}/upo"
        return (await self._call("GET", path, accept="application/xml")).content

    async def upo_by_ksef_number(self, session_ref: str, ksef_number: str) -> bytes:
        path = f"/sessions/{session_ref}/invoices/ksef/{ksef_number}/upo"
        return (await self._call("GET", path, accept="application/xml")).content

    async def session_upo(self, session_ref: str, upo_ref: str) -> bytes:
        path = f"/sessions/{session_ref}/upo/{upo_ref}"
        return (await self._call("GET", path, accept="application/xml")).content

    # ------------------------------------------------------------------ faktury

    async def get_invoice(self, ksef_number: str) -> bytes:
        """Treść XML faktury po numerze KSeF."""
        return (await self._call("GET", f"/invoices/ksef/{ksef_number}", accept="application/xml")).content

    async def query_metadata(
        self,
        filters: dict[str, Any],
        *,
        sort: str = "Desc",
        page_size: int = 250,
        limit: int | None = 1000,
    ) -> list[dict[str, Any]]:
        """Metadane faktur (POST /invoices/query/metadata) z dowolnymi filtrami API."""
        return await self._pages(
            "/invoices/query/metadata",
            "invoices",
            json_body=filters,
            page_size=page_size,
            limit=limit,
            params={"sortOrder": sort},
        )

    async def export_invoices(
        self,
        subject_type: str,
        date_from: datetime,
        date_to: datetime | None = None,
        progress: Progress | None = None,
        *,
        only_metadata: bool = False,
        extra_filters: dict[str, Any] | None = None,
    ) -> ExportPackage:
        key = SymmetricKey.generate()
        date_range: dict[str, Any] = {"dateType": "PermanentStorage", "from": iso(date_from)}
        if date_to is not None:
            date_range["to"] = iso(date_to)

        async def start(refresh_keys: bool) -> dict[str, Any]:
            body: dict[str, Any] = {
                "encryption": await self._encryption(key, refresh_keys),
                "filters": {"subjectType": subject_type, "dateRange": date_range, **(extra_filters or {})},
            }
            if only_metadata:
                body["onlyMetadata"] = True
            return await self._json("POST", "/invoices/exports", json_body=body)

        reference = (await self._with_key_retry(start))["referenceNumber"]
        await report(progress, "export", "KSeF przygotowuje paczkę…")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 600
        delay = 1.0
        while True:
            status = await self.export_status(reference)
            code = status["status"]["code"]
            if code == 200:
                break
            if code != 100:
                raise KsefError(
                    f"Eksport faktur nie powiódł się: {status['status'].get('description', '')} (kod {code})",
                    code=code,
                    details=status["status"].get("details") or [],
                )
            if loop.time() > deadline:
                raise KsefError("KSeF zbyt długo przygotowuje paczkę faktur. Spróbuj później.")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)

        package = status.get("package") or {}
        parts = sorted(package.get("parts") or [], key=lambda p: p["ordinalNumber"])
        result = ExportPackage(
            is_truncated=bool(package.get("isTruncated")),
            last_permanent_storage=parse_dt(package.get("lastPermanentStorageDate")),
            hwm=parse_dt(package.get("permanentStorageHwmDate")),
        )
        if not parts:
            return result

        await report(progress, "export", f"Pobieranie {package.get('invoiceCount', 0)} faktur…")
        archive = bytearray()
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=15.0), transport=self._transport) as dl:
            for part in parts:
                try:
                    response = await dl.request(part.get("method", "GET"), part["url"])
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    raise KsefError(f"Nie udało się pobrać części paczki faktur: {exc}") from exc
                archive += key.decrypt(response.content)

        with zipfile.ZipFile(io.BytesIO(bytes(archive))) as zf:
            for name in zf.namelist():
                if name == "_metadata.json":
                    result.metadata = json.loads(zf.read(name)).get("invoices", [])
                elif name.lower().endswith(".xml"):
                    result.xml[name.rsplit("/", 1)[-1][:-4]] = zf.read(name)
        return result

    async def export_status(self, reference: str) -> dict[str, Any]:
        return await self._json("GET", f"/invoices/exports/{reference}")

    # ------------------------------------------------------------------ tokeny

    async def generate_token(self, permissions: list[str], description: str) -> dict[str, Any]:
        """Nowy token KSeF ({referenceNumber, token}); token jest zwracany tylko raz."""
        body = {"permissions": permissions, "description": description}
        return await self._json("POST", "/tokens", json_body=body)

    async def list_tokens(
        self,
        *,
        statuses: list[str] | None = None,
        description: str | None = None,
        author: tuple[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"pageSize": 100, "status": statuses, "description": description}
        if author:
            params["authorIdentifierType"], params["authorIdentifier"] = author
        return await self._continuation("/tokens", "tokens", params=params)

    async def token_status(self, reference: str) -> dict[str, Any]:
        return await self._json("GET", f"/tokens/{reference}")

    async def revoke_token(self, reference: str) -> None:
        await self._call("DELETE", f"/tokens/{reference}")

    async def wait_for_token(self, reference: str, timeout: float = 60.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = await self.token_status(reference)
            if status["status"] != "Pending" or loop.time() > deadline:
                return status
            await asyncio.sleep(1.5)

    # -------------------------------------------------------------- uprawnienia

    async def grant_person_permissions(
        self,
        identifier_type: str,
        identifier: str,
        permissions: list[str],
        description: str,
        subject_details: dict[str, Any],
    ) -> str:
        """Uprawnienia do pracy w KSeF dla osoby fizycznej (NIP, PESEL albo odcisk certyfikatu)."""
        body = {
            "subjectIdentifier": _identifier(identifier_type, identifier),
            "permissions": permissions,
            "description": description,
            "subjectDetails": subject_details,
        }
        return (await self._json("POST", "/permissions/persons/grants", json_body=body))["referenceNumber"]

    async def grant_entity_permissions(
        self, nip: str, permissions: list[tuple[str, bool]], description: str, full_name: str
    ) -> str:
        """Uprawnienia do obsługi faktur dla innego podmiotu (np. biura rachunkowego). (rodzaj, można_delegować)."""
        body = {
            "subjectIdentifier": _identifier("Nip", nip),
            "permissions": [{"type": kind, "canDelegate": delegate} for kind, delegate in permissions],
            "description": description,
            "subjectDetails": {"fullName": full_name},
        }
        return (await self._json("POST", "/permissions/entities/grants", json_body=body))["referenceNumber"]

    async def grant_authorization(
        self, identifier_type: str, identifier: str, permission: str, description: str, full_name: str
    ) -> str:
        """Uprawnienie podmiotowe: samofakturowanie, faktury RR, przedstawiciel podatkowy albo PEF."""
        body = {
            "subjectIdentifier": _identifier(identifier_type, identifier),
            "permission": permission,
            "description": description,
            "subjectDetails": {"fullName": full_name},
        }
        return (await self._json("POST", "/permissions/authorizations/grants", json_body=body))["referenceNumber"]

    async def grant_indirect_permissions(
        self,
        identifier_type: str,
        identifier: str,
        permissions: list[str],
        description: str,
        subject_details: dict[str, Any],
        target: tuple[str, str | None] | None = None,
    ) -> str:
        """Uprawnienia nadawane pośrednio (np. przez biuro rachunkowe pracownikowi) - dla klienta albo wszystkich."""
        body: dict[str, Any] = {
            "subjectIdentifier": _identifier(identifier_type, identifier),
            "permissions": permissions,
            "description": description,
            "subjectDetails": subject_details,
        }
        if target:
            body["targetIdentifier"] = _identifier(*target)
        return (await self._json("POST", "/permissions/indirect/grants", json_body=body))["referenceNumber"]

    async def grant_subunit_admin(
        self,
        identifier_type: str,
        identifier: str,
        context: tuple[str, str],
        description: str,
        subject_details: dict[str, Any],
        subunit_name: str | None = None,
    ) -> str:
        """Administrator jednostki podrzędnej (identyfikator wewnętrzny albo NIP jednostki JST/grupy VAT)."""
        body: dict[str, Any] = {
            "subjectIdentifier": _identifier(identifier_type, identifier),
            "contextIdentifier": _identifier(*context),
            "description": description,
            "subjectDetails": subject_details,
        }
        if subunit_name:
            body["subunitName"] = subunit_name
        return (await self._json("POST", "/permissions/subunits/grants", json_body=body))["referenceNumber"]

    async def grant_eu_entity_admin(
        self,
        fingerprint: str,
        nip_vat_ue: str,
        description: str,
        eu_entity_name: str,
        subject_details: dict[str, Any],
        eu_entity_details: dict[str, str],
    ) -> str:
        body = {
            "subjectIdentifier": _identifier("Fingerprint", fingerprint),
            "contextIdentifier": _identifier("NipVatUe", nip_vat_ue),
            "description": description,
            "euEntityName": eu_entity_name,
            "subjectDetails": subject_details,
            "euEntityDetails": eu_entity_details,
        }
        path = "/permissions/eu-entities/administration/grants"
        return (await self._json("POST", path, json_body=body))["referenceNumber"]

    async def grant_eu_entity_representative(
        self, fingerprint: str, permissions: list[str], description: str, subject_details: dict[str, Any]
    ) -> str:
        body = {
            "subjectIdentifier": _identifier("Fingerprint", fingerprint),
            "permissions": permissions,
            "description": description,
            "subjectDetails": subject_details,
        }
        return (await self._json("POST", "/permissions/eu-entities/grants", json_body=body))["referenceNumber"]

    async def revoke_permission(self, permission_id: str) -> str:
        return (await self._json("DELETE", f"/permissions/common/grants/{permission_id}"))["referenceNumber"]

    async def revoke_authorization(self, permission_id: str) -> str:
        return (await self._json("DELETE", f"/permissions/authorizations/grants/{permission_id}"))["referenceNumber"]

    async def permission_operation(self, reference: str) -> dict[str, Any]:
        return await self._json("GET", f"/permissions/operations/{reference}")

    async def wait_for_permission_operation(self, reference: str, timeout: float = 60.0) -> dict[str, Any]:
        """Czeka na wynik nadania/odebrania uprawnień.

        Rzuca KsefError, gdy operacja się nie powiodła, i OperationPending, gdy po `timeout` nadal trwa.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = (await self.permission_operation(reference))["status"]
            if status["code"] == 200:
                return status
            if status["code"] >= 400:
                raise KsefError(
                    f"Operacja nie powiodła się: {status.get('description', '')} (kod {status['code']})",
                    code=status["code"],
                    details=status.get("details") or [],
                )
            if loop.time() > deadline:
                raise OperationPending(reference, status)
            await asyncio.sleep(1.5)

    async def attachment_permission_status(self) -> dict[str, Any]:
        return await self._json("GET", "/permissions/attachments/status")

    async def query_personal_permissions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Własne uprawnienia zalogowanego podmiotu w bieżącym kontekście."""
        return await self._pages("/permissions/query/personal/grants", "permissions", json_body=filters or {})

    async def query_person_permissions(
        self, query_type: str = "PermissionsGrantedInCurrentContext", filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Uprawnienia osób/podmiotów do pracy w KSeF (nadane w bieżącym kontekście albo w nim obowiązujące)."""
        body = {"queryType": query_type, **(filters or {})}
        return await self._pages("/permissions/query/persons/grants", "permissions", json_body=body)

    async def query_subunit_permissions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self._pages("/permissions/query/subunits/grants", "permissions", json_body=filters or {})

    async def query_entity_permissions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Uprawnienia do obsługi faktur, które bieżący kontekst otrzymał od innych podmiotów."""
        return await self._pages("/permissions/query/entities/grants", "permissions", json_body=filters or {})

    async def query_entity_roles(self) -> list[dict[str, Any]]:
        return await self._pages("/permissions/query/entities/roles", "roles", method="GET")

    async def query_subordinate_entities(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        path = "/permissions/query/subordinate-entities/roles"
        return await self._pages(path, "roles", json_body=filters or {})

    async def query_authorizations(
        self, query_type: str = "Granted", filters: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Uprawnienia podmiotowe: nadane innym (Granted) albo otrzymane (Received)."""
        body = {"queryType": query_type, **(filters or {})}
        return await self._pages("/permissions/query/authorizations/grants", "authorizationGrants", json_body=body)

    async def query_eu_entity_permissions(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self._pages("/permissions/query/eu-entities/grants", "permissions", json_body=filters or {})

    # ------------------------------------------------------------ certyfikaty

    async def certificate_limits(self) -> dict[str, Any]:
        return await self._json("GET", "/certificates/limits")

    async def enrollment_data(self) -> dict[str, Any]:
        """Dane do CSR - dostępne tylko po zalogowaniu podpisem (XAdES), nie tokenem."""
        return await self._json("GET", "/certificates/enrollments/data")

    async def enroll_certificate(
        self, name: str, certificate_type: str, csr_der: bytes, valid_from: datetime | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"certificateName": name, "certificateType": certificate_type, "csr": b64(csr_der)}
        if valid_from:
            body["validFrom"] = iso(valid_from)
        return await self._json("POST", "/certificates/enrollments", json_body=body)

    async def enrollment_status(self, reference: str) -> dict[str, Any]:
        return await self._json("GET", f"/certificates/enrollments/{reference}")

    async def wait_for_enrollment(self, reference: str, timeout: float = 120.0) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            status = await self.enrollment_status(reference)
            code = status["status"]["code"]
            if code == 200:
                return status
            if code >= 400:
                raise EnrollmentRejected(
                    f"Wniosek odrzucony: {status['status'].get('description', '')} (kod {code})",
                    code=code,
                    details=status["status"].get("details") or [],
                )
            if loop.time() > deadline:
                return status
            await asyncio.sleep(2.0)

    async def retrieve_certificates(self, serial_numbers: list[str]) -> list[dict[str, Any]]:
        body = {"certificateSerialNumbers": serial_numbers}
        return (await self._json("POST", "/certificates/retrieve", json_body=body))["certificates"]

    async def revoke_certificate(self, serial_number: str, reason: str = "Unspecified") -> None:
        await self._call("POST", f"/certificates/{serial_number}/revoke", json_body={"revocationReason": reason})

    async def query_certificates(self, filters: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        return await self._pages("/certificates/query", "certificates", json_body=filters or {}, page_size=50)

    # ------------------------------------------------------------------ limity

    async def context_limits(self) -> dict[str, Any]:
        return await self._json("GET", "/limits/context")

    async def subject_limits(self) -> dict[str, Any]:
        return await self._json("GET", "/limits/subject")

    async def rate_limits(self) -> dict[str, Any]:
        return await self._json("GET", "/rate-limits")

    # ------------------------------------------------- identyfikatory zbiorcze

    async def generate_collective_identifier(
        self, invoices: list[dict[str, Any]], description: str | None = None
    ) -> str:
        """Identyfikator zbiorczy dla płatności za wiele faktur; invoices: [{ksefNumber, payment?}]."""
        body: dict[str, Any] = {"invoices": invoices}
        if description:
            body["description"] = description
        return (await self._json("POST", "/collective-identifiers", json_body=body))["collectiveIdentifierNumber"]

    async def query_collective_identifiers(
        self, created_from: datetime, created_to: datetime, *, number: str | None = None
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"dateCreatedFrom": iso(created_from), "dateCreatedTo": iso(created_to)}
        if number:
            body["collectiveIdentifierNumber"] = number
        return await self._continuation(
            "/collective-identifiers/query",
            "collectiveIdentifiers",
            method="POST",
            json_body=body,
            params={"pageSize": 200},
        )

    async def collective_identifier_invoices(self, numbers: list[str]) -> list[dict[str, Any]]:
        return await self._continuation(
            "/collective-identifiers/invoices",
            "invoices",
            method="POST",
            json_body={"collectiveIdentifierNumbers": numbers},
            params={"pageSize": 500},
        )

    async def collective_identifiers_for_invoice(self, ksef_number: str) -> list[dict[str, Any]]:
        path = f"/collective-identifiers/ksef/{ksef_number}"
        return await self._continuation(path, "collectiveIdentifiers", params={"pageSize": 200})

    # ------------------------------------------------------------------ Peppol

    async def peppol_providers(self) -> list[dict[str, Any]]:
        return await self._pages("/peppol/query", "peppolProviders", method="GET", page_size=100)

    # ------------------------------------------------- dane testowe (środowisko TE)

    async def testdata(self, operation: str, body: dict[str, Any] | None = None, *, method: str = "POST") -> None:
        """Operacje /testdata/* (np. "subject", "person", "permissions", "attachment", "context/block",
        "limits/context/session", "rate-limits"). Część z nich nie wymaga logowania."""
        if self.base_url.rstrip("/") in {ENVIRONMENTS[env]["api"] for env in ("prod", "demo")}:
            # Ostatnia zapora: fikcyjne dane i zmiany limitów nigdy nie trafiają do produkcji ani demo.
            raise KsefError("Dane testowe są dostępne tylko na środowisku testowym KSeF.")
        anonymous = operation.split("/")[0] in {"subject", "person", "permissions", "attachment", "context"}
        await self._call(method, f"/testdata/{operation}", json_body=body, auth=None if anonymous else "access")
