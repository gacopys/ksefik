"""Podpis XAdES dokumentu AuthTokenRequest (logowanie certyfikatem) i wnioski o certyfikat KSeF (CSR)."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from lxml import etree

from .config import os_error_text

NS_AUTH = "http://ksef.mf.gov.pl/auth/token/2.0"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS_XADES = "http://uri.etsi.org/01903/v1.3.2#"
EXC_C14N = "http://www.w3.org/2001/10/xml-exc-c14n#"
SHA256 = "http://www.w3.org/2001/04/xmlenc#sha256"
RSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"
ECDSA_SHA256 = "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"

PrivateKey = rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey


class CredentialsError(Exception):
    pass


class PasswordRequired(CredentialsError):
    """Klucz prywatny jest zaszyfrowany - potrzebne hasło."""


@dataclass
class Signer:
    certificate: x509.Certificate
    key: PrivateKey

    @property
    def fingerprint(self) -> str:
        return self.certificate.fingerprint(hashes.SHA256()).hex().upper()

    @property
    def subject(self) -> str:
        return self.certificate.subject.rfc4514_string()

    @property
    def valid_to(self) -> datetime:
        return self.certificate.not_valid_after_utc


def load_signer(cert_path: Path, key_path: Path | None = None, password: str | None = None) -> Signer:
    """Certyfikat i klucz z plików PEM/DER albo z jednego pliku PKCS#12 (.p12/.pfx)."""
    secret = password.encode() if password else None
    try:
        data = cert_path.read_bytes()
    except OSError as exc:
        raise CredentialsError(f"Nie można odczytać certyfikatu {cert_path}: {os_error_text(exc)}") from exc

    if cert_path.suffix.lower() in (".p12", ".pfx"):
        try:
            key, cert, _ = pkcs12.load_key_and_certificates(data, secret)
        except ValueError as exc:
            if secret is None:
                raise PasswordRequired("Plik .p12 jest chroniony hasłem.") from exc
            raise CredentialsError("Niepoprawne hasło do pliku .p12 albo uszkodzony plik.") from exc
        if key is None or cert is None:
            raise CredentialsError("Plik .p12 nie zawiera certyfikatu z kluczem prywatnym.")
        return _signer(cert, key)

    try:
        cert = x509.load_pem_x509_certificate(data) if b"-----BEGIN" in data else x509.load_der_x509_certificate(data)
    except ValueError as exc:
        raise CredentialsError(f"Plik {cert_path} nie zawiera certyfikatu X.509.") from exc

    key_file = key_path or cert_path
    try:
        key_data = key_file.read_bytes()
    except OSError as exc:
        raise CredentialsError(f"Nie można odczytać klucza {key_file}: {os_error_text(exc)}") from exc
    try:
        if b"-----BEGIN" in key_data:
            key = serialization.load_pem_private_key(key_data, secret)
        else:
            key = serialization.load_der_private_key(key_data, secret)
    except TypeError as exc:
        if secret is None:
            raise PasswordRequired("Klucz prywatny jest chroniony hasłem.") from exc
        raise CredentialsError("Klucz prywatny nie jest zaszyfrowany - pomiń hasło.") from exc
    except ValueError as exc:
        if secret is not None:
            raise CredentialsError("Niepoprawne hasło do klucza prywatnego.") from exc
        raise CredentialsError(f"Plik {key_file} nie zawiera klucza prywatnego.") from exc
    return _signer(cert, key)


def _signer(cert: x509.Certificate, key: object) -> Signer:
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise CredentialsError("Obsługiwane są klucze RSA i EC (P-256).")
    if cert.public_key().public_numbers() != key.public_key().public_numbers():
        raise CredentialsError("Klucz prywatny nie pasuje do certyfikatu.")
    return Signer(cert, key)


# ---------------------------------------------------------------- AuthTokenRequest

CONTEXT_ELEMENTS = {"Nip": "Nip", "InternalId": "InternalId", "NipVatUe": "NipVatUe", "PeppolId": "PeppolId"}


def auth_token_request(
    challenge: str,
    context_type: str,
    context_value: str,
    *,
    subject_type: str = "certificateSubject",
    allowed_ips: dict[str, list[str]] | None = None,
) -> etree._Element:
    root = etree.Element(f"{{{NS_AUTH}}}AuthTokenRequest", nsmap={None: NS_AUTH})
    etree.SubElement(root, f"{{{NS_AUTH}}}Challenge").text = challenge
    ctx = etree.SubElement(root, f"{{{NS_AUTH}}}ContextIdentifier")
    etree.SubElement(ctx, f"{{{NS_AUTH}}}{CONTEXT_ELEMENTS[context_type]}").text = context_value
    etree.SubElement(root, f"{{{NS_AUTH}}}SubjectIdentifierType").text = subject_type
    if allowed_ips and any(allowed_ips.values()):
        policy = etree.SubElement(root, f"{{{NS_AUTH}}}AuthorizationPolicy")
        allowed = etree.SubElement(policy, f"{{{NS_AUTH}}}AllowedIps")
        for key, element in (("ip4Addresses", "Ip4Address"), ("ip4Ranges", "Ip4Range"), ("ip4Masks", "Ip4Mask")):
            for value in allowed_ips.get(key, []):
                etree.SubElement(allowed, f"{{{NS_AUTH}}}{element}").text = value
    return root


def _c14n(element: etree._Element) -> bytes:
    return etree.tostring(element, method="c14n", exclusive=True, with_comments=False)


def _digest(data: bytes) -> str:
    return base64.b64encode(hashlib.sha256(data).digest()).decode()


def sign_xades(root: etree._Element, signer: Signer) -> bytes:
    """Podpis XAdES-BES otaczany (enveloped), SHA-256, RSA PKCS#1 v1.5 albo ECDSA (R||S)."""
    ds = f"{{{NS_DS}}}"
    xa = f"{{{NS_XADES}}}"
    cert = signer.certificate
    is_ec = isinstance(signer.key, ec.EllipticCurvePrivateKey)
    doc_digest = _digest(_c14n(root))  # przed dodaniem podpisu == po transformacji enveloped-signature

    sig = etree.SubElement(root, ds + "Signature", Id="Signature", nsmap={"ds": NS_DS})
    signed_info = etree.SubElement(sig, ds + "SignedInfo")
    etree.SubElement(signed_info, ds + "CanonicalizationMethod", Algorithm=EXC_C14N)
    etree.SubElement(signed_info, ds + "SignatureMethod", Algorithm=ECDSA_SHA256 if is_ec else RSA_SHA256)

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
    etree.SubElement(cert_digest, ds + "DigestValue").text = _digest(cert_der)
    issuer_serial = etree.SubElement(cert_el, xa + "IssuerSerial")
    etree.SubElement(issuer_serial, ds + "X509IssuerName").text = cert.issuer.rfc4514_string()
    etree.SubElement(issuer_serial, ds + "X509SerialNumber").text = str(cert.serial_number)

    props_digest.text = _digest(_c14n(sp))
    data = _c14n(signed_info)
    if isinstance(signer.key, ec.EllipticCurvePrivateKey):
        r, s = decode_dss_signature(signer.key.sign(data, ec.ECDSA(hashes.SHA256())))
        size = (signer.key.curve.key_size + 7) // 8
        signature = r.to_bytes(size, "big") + s.to_bytes(size, "big")
    else:
        signature = signer.key.sign(data, padding.PKCS1v15(), hashes.SHA256())
    sig_value.text = base64.b64encode(signature).decode()
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8")


# -------------------------------------------------------------- certyfikaty KSeF

ENROLLMENT_OIDS = {
    "commonName": NameOID.COMMON_NAME,
    "surname": NameOID.SURNAME,
    "serialNumber": NameOID.SERIAL_NUMBER,
    "countryName": NameOID.COUNTRY_NAME,
    "organizationName": NameOID.ORGANIZATION_NAME,
    "givenName": NameOID.GIVEN_NAME,
    "uniqueIdentifier": NameOID.X500_UNIQUE_IDENTIFIER,
    "organizationIdentifier": x509.ObjectIdentifier("2.5.4.97"),
}


def enrollment_csr(data: dict[str, object]) -> tuple[bytes, ec.EllipticCurvePrivateKey]:
    """CSR (DER) z danymi z /certificates/enrollments/data i nowym kluczem EC P-256 (zalecanym przez KSeF)."""
    attributes = []
    for name, oid in ENROLLMENT_OIDS.items():
        value = data.get(name)
        for item in value if isinstance(value, list) else [value]:
            if item:
                attributes.append(x509.NameAttribute(oid, str(item)))
    key = ec.generate_private_key(ec.SECP256R1())
    csr = x509.CertificateSigningRequestBuilder().subject_name(x509.Name(attributes)).sign(key, hashes.SHA256())
    return csr.public_bytes(serialization.Encoding.DER), key


def save_key(key: PrivateKey, path: Path, password: str | None = None) -> None:
    encryption = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_bytes(pem)


def save_certificate(der_b64: str, path: Path) -> x509.Certificate:
    cert = x509.load_der_x509_certificate(base64.b64decode(der_b64))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert
