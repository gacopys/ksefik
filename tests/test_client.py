"""Klient KSeF na atrapie HTTP (bez sieci): logowanie, stronicowanie, błędy, wysyłka wsadowa i podpis XAdES."""

import asyncio
import base64
import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID
from lxml import etree

from ksefik.client import (
    CertificateCredentials,
    KsefClient,
    KsefError,
    RateLimitError,
    TokenCredentials,
    allowed_ips_policy,
)
from ksefik.crypto import SymmetricKey
from ksefik.xades import NS_DS, Signer, auth_token_request, enrollment_csr, sign_xades

API = "https://ksef.example/v2"
NIP = "5438539502"


def self_signed(key) -> x509.Certificate:
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test"), x509.NameAttribute(NameOID.COUNTRY_NAME, "PL")])
    now = datetime.now(UTC)
    return (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )


MF_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
MF_CERT = self_signed(MF_KEY)


def public_keys() -> list[dict]:
    der = base64.b64encode(MF_CERT.public_bytes(serialization.Encoding.DER)).decode()
    now = datetime.now(UTC)
    return [
        {
            "certificate": der,
            "publicKeyId": "A" * 44,
            "usage": ["KsefTokenEncryption", "SymmetricKeyEncryption"],
            "validFrom": (now - timedelta(days=1)).isoformat(),
            "validTo": (now + timedelta(days=1)).isoformat(),
        }
    ]


def mf_decrypt(b64: str) -> bytes:
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    return MF_KEY.decrypt(base64.b64decode(b64), oaep)


class FakeKsef:
    """Minimalna atrapa API: logowanie, tokeny ze stronicowaniem, uprawnienia, sesja wsadowa."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.auth_body: dict | None = None
        self.xades: bytes | None = None
        self.batch: dict = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/v2")
        if path == "/security/public-key-certificates":
            return httpx.Response(200, json=public_keys())
        if path == "/auth/challenge":
            return httpx.Response(200, json={"challenge": "20260101-CR-0000000000-0000000000-00", "timestampMs": 1})
        if path == "/auth/ksef-token":
            self.auth_body = json.loads(request.content)
            return httpx.Response(202, json={"referenceNumber": "AUTH", "authenticationToken": {"token": "op"}})
        if path == "/auth/xades-signature":
            self.xades = request.content
            return httpx.Response(202, json={"referenceNumber": "AUTH", "authenticationToken": {"token": "op"}})
        if path == "/auth/AUTH":
            return httpx.Response(200, json={"status": {"code": 200, "description": "OK"}})
        if path == "/auth/token/redeem":
            until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            return httpx.Response(
                200,
                json={
                    "accessToken": {"token": "acc", "validUntil": until},
                    "refreshToken": {"token": "ref", "validUntil": until},
                },
            )
        assert request.headers.get("Authorization") == "Bearer acc", path
        if path == "/tokens":
            if request.headers.get("x-continuation-token") == "page2":
                return httpx.Response(200, json={"tokens": [{"referenceNumber": "T2"}]})
            return httpx.Response(200, json={"tokens": [{"referenceNumber": "T1"}], "continuationToken": "page2"})
        if path == "/permissions/query/persons/grants":
            page = int(request.url.params["pageOffset"])
            return httpx.Response(200, json={"permissions": [{"id": f"P{page}"}], "hasMore": page < 2})
        if path == "/limits/context":
            return httpx.Response(
                400,
                json={
                    "exception": {
                        "exceptionDetailList": [
                            {"exceptionCode": 21405, "exceptionDescription": "Błąd", "details": ["szczegół"]}
                        ]
                    }
                },
            )
        if path == "/rate-limits":
            return httpx.Response(429, headers={"Retry-After": "30"}, json={"status": {"details": ["za dużo"]}})
        if path == "/sessions/batch":
            self.batch = json.loads(request.content)
            parts = [
                {
                    "ordinalNumber": p["ordinalNumber"],
                    "method": "PUT",
                    "url": f"https://upload.example/{p['ordinalNumber']}",
                    "headers": {"x-ms-blob-type": "BlockBlob"},
                }
                for p in self.batch["batchFile"]["fileParts"]
            ]
            return httpx.Response(201, json={"referenceNumber": "BATCH", "partUploadRequests": parts})
        if path == "/sessions/batch/BATCH/close":
            return httpx.Response(204)
        return httpx.Response(404, json={"title": "Nie ma", "detail": f"brak {path}"})


@pytest.fixture
def ksef():
    return FakeKsef()


def client(fake: FakeKsef, credentials=None, **kwargs) -> KsefClient:
    transport = httpx.MockTransport(fake)
    return KsefClient(API, NIP, credentials or TokenCredentials("TOKEN"), transport=transport, **kwargs)


def run(coro):
    return asyncio.run(coro)


def test_token_login_and_pagination(ksef):
    async def scenario():
        async with client(ksef, allowed_ips=["10.0.0.1", "10.0.0.0/8"]) as c:
            tokens = await c.list_tokens()
            permissions = await c.query_person_permissions()
            return tokens, permissions

    tokens, permissions = run(scenario())
    assert [t["referenceNumber"] for t in tokens] == ["T1", "T2"]
    assert [p["id"] for p in permissions] == ["P0", "P1", "P2"]
    body = ksef.auth_body
    assert body["contextIdentifier"] == {"type": "Nip", "value": NIP}
    assert mf_decrypt(body["encryptedToken"]) == b"TOKEN|1"
    assert body["authorizationPolicy"] == {"allowedIps": {"ip4Addresses": ["10.0.0.1"], "ip4Masks": ["10.0.0.0/8"]}}


def test_errors_are_readable(ksef):
    async def scenario():
        async with client(ksef) as c:
            with pytest.raises(KsefError) as error:
                await c.context_limits()
            with pytest.raises(RateLimitError) as limited:
                await c.rate_limits()
            with pytest.raises(KsefError) as missing:
                await c.peppol_providers()
            return error.value, limited.value, missing.value

    error, limited, missing = run(scenario())
    assert (error.code, error.message, error.details) == (21405, "Błąd", ["szczegół"])
    assert limited.retry_after == 30 and "30 s" in limited.message
    assert missing.message == "brak /peppol/query"


def test_batch_package_is_encrypted_zip(ksef):
    uploads: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "upload.example":
            assert request.headers["x-ms-blob-type"] == "BlockBlob"
            uploads.append(request.content)
            return httpx.Response(201)
        return ksef(request)

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with KsefClient(API, NIP, TokenCredentials("TOKEN"), transport=transport) as c:
            return await c.send_batch([("a.xml", b"<a/>"), ("b", b"<b/>")])

    assert run(scenario()) == "BATCH"
    encryption = ksef.batch["encryption"]
    key = SymmetricKey(
        mf_decrypt(encryption["encryptedSymmetricKey"]), base64.b64decode(encryption["initializationVector"])
    )
    package = b"".join(key.decrypt(part) for part in uploads)
    assert base64.b64encode(hashlib.sha256(package).digest()).decode() == ksef.batch["batchFile"]["fileHash"]
    with zipfile.ZipFile(io.BytesIO(package)) as zf:
        assert {name: zf.read(name) for name in zf.namelist()} == {"a.xml": b"<a/>", "b.xml": b"<b/>"}


def test_allowed_ips_policy():
    assert allowed_ips_policy(["1.2.3.4", "1.2.3.4-1.2.3.9", "10.0.0.0/8"]) == {
        "ip4Addresses": ["1.2.3.4"],
        "ip4Ranges": ["1.2.3.4-1.2.3.9"],
        "ip4Masks": ["10.0.0.0/8"],
    }


# ---------------------------------------------------------------------------- XAdES


def verify_xades(signed: bytes, certificate: x509.Certificate) -> None:
    root = etree.fromstring(signed)
    sig = root.find(f"{{{NS_DS}}}Signature")
    signed_info = sig.find(f"{{{NS_DS}}}SignedInfo")
    value = base64.b64decode(sig.find(f"{{{NS_DS}}}SignatureValue").text)
    data = etree.tostring(signed_info, method="c14n", exclusive=True)
    key = certificate.public_key()
    if isinstance(key, ec.EllipticCurvePublicKey):
        half = len(value) // 2
        der = encode_dss_signature(int.from_bytes(value[:half], "big"), int.from_bytes(value[half:], "big"))
        key.verify(der, data, ec.ECDSA(hashes.SHA256()))
    else:
        key.verify(value, data, padding.PKCS1v15(), hashes.SHA256())
    # Skrót dokumentu (po usunięciu podpisu) musi się zgadzać z referencją.
    root.remove(sig)
    digest = base64.b64encode(hashlib.sha256(etree.tostring(root, method="c14n", exclusive=True)).digest()).decode()
    assert signed_info.find(f".//{{{NS_DS}}}DigestValue").text == digest


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_xades_signature_verifies(kind):
    key = (
        ec.generate_private_key(ec.SECP256R1())
        if kind == "ec"
        else rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    signer = Signer(self_signed(key), key)
    request = auth_token_request(
        "20260101-CR-0000000000-0000000000-00", "InternalId", f"{NIP}-00011", allowed_ips={"ip4Addresses": ["1.2.3.4"]}
    )
    signed = sign_xades(request, signer)
    assert b"<InternalId>5438539502-00011</InternalId>" in signed
    assert b"<Ip4Address>1.2.3.4</Ip4Address>" in signed
    verify_xades(signed, signer.certificate)


def test_certificate_login_sends_signed_request(ksef):
    key = ec.generate_private_key(ec.SECP256R1())
    signer = Signer(self_signed(key), key)

    async def scenario():
        async with client(ksef, CertificateCredentials(signer, "certificateFingerprint")) as c:
            await c.ensure_auth()

    run(scenario())
    assert b"<SubjectIdentifierType>certificateFingerprint</SubjectIdentifierType>" in ksef.xades
    verify_xades(ksef.xades, signer.certificate)


def test_enrollment_csr_uses_given_identity():
    der, key = enrollment_csr(
        {"commonName": "Firma", "countryName": "PL", "organizationIdentifier": "VATPL-5438539502"}
    )
    csr = x509.load_der_x509_csr(der)
    assert csr.is_signature_valid
    assert {(a.oid.dotted_string, a.value) for a in csr.subject} == {
        ("2.5.4.3", "Firma"),
        ("2.5.4.6", "PL"),
        ("2.5.4.97", "VATPL-5438539502"),
    }
    assert isinstance(key, ec.EllipticCurvePrivateKey)
