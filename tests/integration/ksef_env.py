"""Fikcyjne firmy na środowisku testowym KSeF i dane pomocnicze (NIP, PESEL, identyfikator wewnętrzny)."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

API = "https://api-test.ksef.mf.gov.pl/v2"
KEY_PASSWORD = "haslo-testowe"
# Uprawnienia tokena aplikacji w testach (EnforcementOperations wymaga roli komornika).
TOKEN_PERMISSIONS = [
    "InvoiceRead",
    "InvoiceWrite",
    "CredentialsRead",
    "CredentialsManage",
    "SubunitManage",
    "Introspection",
    "CollectiveIdentifierManage",
]


def random_nip() -> str:
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    while True:
        digits = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]
        check = sum(d * w for d, w in zip(digits, weights, strict=False)) % 11
        if check != 10:
            return "".join(map(str, digits + [check]))


def random_pesel() -> str:
    birth = date(1970, 1, 1) + timedelta(days=random.randint(0, 12000))
    month = birth.month + (20 if birth.year >= 2000 else 0)
    digits = f"{birth.year % 100:02d}{month:02d}{birth.day:02d}{random.randint(0, 9999):04d}"
    weights = (1, 3, 7, 9, 1, 3, 7, 9, 1, 3)
    control = (10 - sum(int(d) * w for d, w in zip(digits, weights, strict=False)) % 10) % 10
    return digits + str(control)


def internal_id(nip: str, unit: int) -> str:
    """Identyfikator wewnętrzny: NIP-NNNNC, gdzie C to suma kontrolna (wagi 1, 3, 1, 3…, modulo 10)."""
    base = f"{nip}{unit:04d}"
    check = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(base)) % 10
    return f"{nip}-{unit:04d}{check}"


@dataclass
class Company:
    """Fikcyjna firma na środowisku testowym: pieczęć (XAdES) i token KSeF z pełnymi uprawnieniami."""

    nip: str
    name: str
    key: ec.EllipticCurvePrivateKey
    certificate: x509.Certificate
    token: str = ""
    cert_path: Path = Path()
    key_path: Path = Path()
    extra: dict = field(default_factory=dict)

    @property
    def signer(self):
        from ksefik.xades import Signer

        return Signer(self.certificate, self.key)

    def client(self, *, token: bool = False):
        from ksefik.client import CertificateCredentials, KsefClient, TokenCredentials

        credentials = TokenCredentials(self.token) if token else CertificateCredentials(self.signer)
        return KsefClient(API, self.nip, credentials)


def make_company(name: str, folder: Path) -> Company:
    nip = random_nip()
    full = f"{name} {nip}"
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "PL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, full),
            x509.NameAttribute(x509.ObjectIdentifier("2.5.4.97"), f"VATPL-{nip}"),
            x509.NameAttribute(NameOID.COMMON_NAME, full),
        ]
    )
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=7))
        .sign(key, hashes.SHA256())
    )
    company = Company(nip, full, key, certificate)
    folder.mkdir(parents=True, exist_ok=True)
    company.cert_path = folder / f"{nip}.pem"
    company.key_path = folder / f"{nip}.klucz.pem"
    company.cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    encryption = serialization.BestAvailableEncryption(KEY_PASSWORD.encode())
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    company.key_path.write_bytes(pem)
    return company


async def issue_token(company: Company) -> str:
    async with company.client() as client:
        created = await client.generate_token(TOKEN_PERMISSIONS, "ksefik testy integracyjne")
        status = await client.wait_for_token(created["referenceNumber"], timeout=90)
        assert status["status"] == "Active", f"token nie aktywował się: {status}"
        return created["token"]
