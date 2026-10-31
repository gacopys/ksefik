"""Narzędzie deweloperskie: tworzy token KSeF na ŚRODOWISKU TESTOWYM dla losowego (fikcyjnego) NIP-u.

Środowisko testowe akceptuje samopodpisane certyfikaty, więc skrypt:
  1. generuje certyfikat "pieczęci firmowej" z organizationIdentifier = VATPL-<NIP>,
  2. podpisuje AuthTokenRequest podpisem XAdES-BES i loguje się,
  3. generuje token KSeF z uprawnieniami InvoiceRead + InvoiceWrite i wypisuje go.

Użycie:  uv run python scripts/test_env_token.py [--nip 1234567890] [--setup]
  --setup  zamiast wypisywać token, zapisuje gotowy profil testowy (fikcyjne dane) i token
           w $XDG_CONFIG_HOME/ksefik - uruchamiaj z osobnym XDG_CONFIG_HOME (robi to `make test-env`).
Nie działa (celowo) na produkcji.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import random
import time
from datetime import UTC, datetime, timedelta

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.x509.oid import NameOID
from lxml import etree

API = "https://api-test.ksef.mf.gov.pl/v2"
NS_AUTH = "http://ksef.mf.gov.pl/auth/token/2.0"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_XADES = "http://uri.etsi.org/01903/v1.3.2#"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"

TEST_PROFILE = """\
# Profil piaskownicy: środowisko TESTOWE KSeF, wyłącznie fikcyjne dane.
srodowisko = "test"
synchronizuj_od = "{since}"
synchronizuj = ["wystawione", "otrzymane", "podmiot3"]

[sprzedawca]
nazwa = "Firma Testowa {seller}"
nip = "{seller}"
adres_l1 = "ul. Testowa 1"
adres_l2 = "00-001 Warszawa"

[[rachunki]]
numer = "61109010140000071219812874"

[[rachunki]]
numer = "PL27114020040000300201355387"
waluta = "EUR"
swift = "BREXPLPW"

[faktura]
numeracja = "FV/{{nr}}/{{mm}}/{{rrrr}}"
daty = "dzisiaj"
termin_platnosci_dni = 14

[[pozycje]]
nazwa = "Usługi programistyczne"
jednostka = "usł."
stawka_vat = "23"
domyslna = true

[[pozycje]]
nazwa = "Konsultacje"
jednostka = "godz."
stawka_vat = "23"
cena_netto = "200"

[[kontrahenci]]
nazwa = "Nabywca Testowy Sp. z o.o."
nip = "{buyer}"
adres_l1 = "ul. Przykładowa 2"
adres_l2 = "00-002 Warszawa"
domyslny = true

[[kontrahenci]]
nazwa = "Testkunde GmbH"
vat_ue = "DE123456789"
adres_l1 = "Hauptstraße 1"
adres_l2 = "10115 Berlin"
kraj = "DE"
waluta = "EUR"
"""


def random_nip() -> str:
    weights = (6, 5, 7, 2, 3, 4, 5, 6, 7)
    while True:
        digits = [random.randint(1, 9)] + [random.randint(0, 9) for _ in range(8)]
        check = sum(d * w for d, w in zip(digits, weights, strict=False)) % 11
        if check != 10:
            return "".join(map(str, digits + [check]))


def make_seal(nip: str, name: str) -> tuple[rsa.RSAPrivateKey, x509.Certificate]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "PL"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, name),
            x509.NameAttribute(x509.ObjectIdentifier("2.5.4.97"), f"VATPL-{nip}"),
            x509.NameAttribute(NameOID.COMMON_NAME, name),
        ]
    )
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .sign(key, hashes.SHA256())
    )
    return key, cert


def c14n(element: etree._Element) -> bytes:
    return etree.tostring(element, method="c14n", exclusive=True, with_comments=False)


def digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def sign_xades(root: etree._Element, key: rsa.RSAPrivateKey, cert: x509.Certificate) -> bytes:
    ds = f"{{{NS_DS}}}"
    xa = f"{{{NS_XADES}}}"
    doc_digest = digest(c14n(root))  # przed dodaniem podpisu == po transformacji enveloped-signature

    sig = etree.SubElement(root, ds + "Signature", Id="Signature", nsmap={"ds": NS_DS})
    signed_info = etree.SubElement(sig, ds + "SignedInfo")
    etree.SubElement(signed_info, ds + "CanonicalizationMethod", Algorithm=EXC_C14N)
    etree.SubElement(signed_info, ds + "SignatureMethod", Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256")

    ref_doc = etree.SubElement(signed_info, ds + "Reference", URI="")
    transforms = etree.SubElement(ref_doc, ds + "Transforms")
    etree.SubElement(transforms, ds + "Transform", Algorithm="http://www.w3.org/2000/09/xmldsig#enveloped-signature")
    etree.SubElement(transforms, ds + "Transform", Algorithm=EXC_C14N)
    etree.SubElement(ref_doc, ds + "DigestMethod", Algorithm=SHA256)
    etree.SubElement(ref_doc, ds + "DigestValue").text = doc_digest

    ref_props = etree.SubElement(
        signed_info, ds + "Reference", Type="http://uri.etsi.org/01903#SignedProperties", URI="#SignedProperties"
    )
    transforms = etree.SubElement(ref_props, ds + "Transforms")
    etree.SubElement(transforms, ds + "Transform", Algorithm=EXC_C14N)
    etree.SubElement(ref_props, ds + "DigestMethod", Algorithm=SHA256)
    props_digest = etree.SubElement(ref_props, ds + "DigestValue")

    sig_value = etree.SubElement(sig, ds + "SignatureValue")
    key_info = etree.SubElement(sig, ds + "KeyInfo")
    x509_data = etree.SubElement(key_info, ds + "X509Data")
    cert_der = cert.public_bytes(serialization.Encoding.DER)
    etree.SubElement(x509_data, ds + "X509Certificate").text = base64.b64encode(cert_der).decode()

    obj = etree.SubElement(sig, ds + "Object")
    qp = etree.SubElement(obj, xa + "QualifyingProperties", Target="#Signature", nsmap={"xades": NS_XADES})
    sp = etree.SubElement(qp, xa + "SignedProperties", Id="SignedProperties")
    ssp = etree.SubElement(sp, xa + "SignedSignatureProperties")
    etree.SubElement(ssp, xa + "SigningTime").text = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    signing_cert = etree.SubElement(ssp, xa + "SigningCertificate")
    cert_el = etree.SubElement(signing_cert, xa + "Cert")
    cert_digest = etree.SubElement(cert_el, xa + "CertDigest")
    etree.SubElement(cert_digest, ds + "DigestMethod", Algorithm=SHA256)
    etree.SubElement(cert_digest, ds + "DigestValue").text = digest(cert_der)
    issuer_serial = etree.SubElement(cert_el, xa + "IssuerSerial")
    etree.SubElement(issuer_serial, ds + "X509IssuerName").text = cert.issuer.rfc4514_string()
    etree.SubElement(issuer_serial, ds + "X509SerialNumber").text = str(cert.serial_number)

    props_digest.text = digest(c14n(sp))
    signature = key.sign(c14n(signed_info), padding.PKCS1v15(), hashes.SHA256())
    sig_value.text = base64.b64encode(signature).decode()
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


def create_token(nip: str) -> str:
    http = httpx.Client(base_url=API, timeout=30)
    challenge = http.post("/auth/challenge").raise_for_status().json()

    root = etree.Element(f"{{{NS_AUTH}}}AuthTokenRequest", nsmap={None: NS_AUTH})
    etree.SubElement(root, f"{{{NS_AUTH}}}Challenge").text = challenge["challenge"]
    ctx = etree.SubElement(root, f"{{{NS_AUTH}}}ContextIdentifier")
    etree.SubElement(ctx, f"{{{NS_AUTH}}}Nip").text = nip
    etree.SubElement(root, f"{{{NS_AUTH}}}SubjectIdentifierType").text = "certificateSubject"

    key, cert = make_seal(nip, f"Firma Testowa {nip}")
    signed = sign_xades(root, key, cert)

    response = http.post("/auth/xades-signature", content=signed, headers={"Content-Type": "application/xml"})
    if response.is_error:
        raise SystemExit(f"xades-signature: HTTP {response.status_code}: {response.text}")
    started = response.json()
    auth_token = started["authenticationToken"]["token"]
    bearer = {"Authorization": f"Bearer {auth_token}"}
    for _ in range(30):
        status = http.get(f"/auth/{started['referenceNumber']}", headers=bearer).raise_for_status().json()["status"]
        if status["code"] != 100:
            break
        time.sleep(1)
    if status["code"] != 200:
        raise SystemExit(f"Uwierzytelnienie nieudane: {status}")
    access = http.post("/auth/token/redeem", headers=bearer).raise_for_status().json()["accessToken"]["token"]

    bearer = {"Authorization": f"Bearer {access}"}
    generated = http.post(
        "/tokens",
        json={"permissions": ["InvoiceRead", "InvoiceWrite"], "description": "ksefik testy"},
        headers=bearer,
    )
    if generated.is_error:
        raise SystemExit(f"/tokens: HTTP {generated.status_code}: {generated.text}")
    generated = generated.json()
    for _ in range(30):
        token_status = http.get(f"/tokens/{generated['referenceNumber']}", headers=bearer).raise_for_status().json()
        if token_status["status"] != "Pending":
            break
        time.sleep(1)
    if token_status["status"] != "Active":
        raise SystemExit(f"Token nieaktywny: {token_status}")
    return generated["token"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--nip", default=None, help="NIP kontekstu (domyślnie losowy)")
    parser.add_argument("--setup", action="store_true", help="zapisz konfigurację testową i token")
    args = parser.parse_args()
    nip = args.nip or random_nip()

    if not args.setup:
        token = create_token(nip)
        print(f"NIP={nip}")
        print(f"KSEF_TOKEN={token}")
        return

    from ksefik.config import PROFILE_PATH
    from ksefik.profile import load_profile
    from ksefik.tokenstore import token_file

    # Bezpiecznik: nigdy nie nadpisujemy prawdziwego (nietestowego) profilu.
    if PROFILE_PATH.exists() and 'srodowisko = "test"' not in PROFILE_PATH.read_text(encoding="utf-8"):
        raise SystemExit(f"{PROFILE_PATH} nie jest profilem testowym - nie nadpisuję. Ustaw osobny XDG_CONFIG_HOME.")
    token = create_token(nip)
    since = datetime.now().replace(day=1).strftime("%Y-%m-%d")
    PROFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROFILE_PATH.write_text(TEST_PROFILE.format(since=since, seller=nip, buyer=random_nip()), encoding="utf-8")
    load_profile(PROFILE_PATH)  # kontrola poprawności zapisanego profilu
    path = token_file("test", nip)
    path.touch(mode=0o600, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    print(f"Piaskownica gotowa: NIP {nip}\n  profil: {PROFILE_PATH}\n  token:  {path}")


if __name__ == "__main__":
    main()
