import base64
from datetime import UTC, datetime, timedelta

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from ksefik.crypto import SymmetricKey, b64, sha256_b64, sha256_b64url
from ksefik.xades import CredentialsError, PasswordRequired, enrollment_csr, load_signer, save_certificate, save_key


@pytest.fixture(scope="module")
def identity():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Unit test")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(123)
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    return key, cert


@pytest.mark.parametrize("format", ["pem", "der", "combined", "p12", "pfx"])
@pytest.mark.parametrize("password", [None, "secret"])
def test_signer_loads_supported_formats_with_or_without_password(tmp_path, identity, format, password):
    key, cert = identity
    cert_path = tmp_path / f"cert.{format}"
    key_path = None
    encryption = serialization.BestAvailableEncryption(password.encode()) if password else serialization.NoEncryption()
    if format in ("p12", "pfx"):
        cert_path.write_bytes(pkcs12.serialize_key_and_certificates(b"test", key, cert, None, encryption))
    elif format == "combined":
        cert_path.write_bytes(
            cert.public_bytes(serialization.Encoding.PEM)
            + key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption)
        )
    else:
        encoding = serialization.Encoding.PEM if format == "pem" else serialization.Encoding.DER
        cert_path.write_bytes(cert.public_bytes(encoding))
        key_path = tmp_path / "key"
        key_path.write_bytes(key.private_bytes(encoding, serialization.PrivateFormat.PKCS8, encryption))
    if password:
        with pytest.raises(PasswordRequired):
            load_signer(cert_path, key_path)
        with pytest.raises(CredentialsError):
            load_signer(cert_path, key_path, "wrong")
    signer = load_signer(cert_path, key_path, password)
    assert signer.fingerprint == cert.fingerprint(hashes.SHA256()).hex().upper()
    assert signer.subject == "CN=Unit test" and signer.valid_to == cert.not_valid_after_utc
    assert signer.key.public_key().public_numbers() == key.public_key().public_numbers()


def test_credential_file_errors_and_mismatched_keys(tmp_path, identity):
    key, cert = identity
    with pytest.raises(CredentialsError, match="odczytać certyfikatu"):
        load_signer(tmp_path / "missing")
    certificate = tmp_path / "cert.pem"
    certificate.write_bytes(b"invalid")
    with pytest.raises(CredentialsError, match="X.509"):
        load_signer(certificate)
    certificate.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    with pytest.raises(CredentialsError, match="odczytać klucza"):
        load_signer(certificate, tmp_path / "missing")
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(b"invalid")
    with pytest.raises(CredentialsError, match="klucza prywatnego"):
        load_signer(certificate, key_path)
    other = ec.generate_private_key(ec.SECP256R1())
    save_key(other, key_path)
    with pytest.raises(CredentialsError, match="nie pasuje"):
        load_signer(certificate, key_path)
    save_key(key, key_path)
    with pytest.raises(CredentialsError, match="pomiń hasło"):
        load_signer(certificate, key_path, "unexpected")
    unsupported = ed25519.Ed25519PrivateKey.generate()
    key_path.write_bytes(
        unsupported.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(CredentialsError, match="RSA i EC"):
        load_signer(certificate, key_path)


def test_pkcs12_without_private_key_is_rejected(tmp_path, identity):
    _, cert = identity
    path = tmp_path / "cert.p12"
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"test", None, cert, None, serialization.NoEncryption()))
    with pytest.raises(CredentialsError, match="kluczem prywatnym"):
        load_signer(path)


def test_save_key_permissions_encryption_and_certificate_roundtrip(tmp_path, identity):
    key, cert = identity
    key_path = tmp_path / "nested/key.pem"
    save_key(key, key_path, "secret")
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert b"ENCRYPTED PRIVATE KEY" in key_path.read_bytes()
    cert_path = tmp_path / "cert.pem"
    assert save_certificate(b64(cert.public_bytes(serialization.Encoding.DER)), cert_path) == cert
    assert load_signer(cert_path, key_path, "secret").certificate == cert


def test_csr_includes_repeated_attributes_and_ignores_absent_values():
    der, _ = enrollment_csr({"commonName": "Test", "organizationName": ["A", "B"], "surname": None})
    csr = x509.load_der_x509_csr(der)
    assert csr.is_signature_valid
    assert [a.value for a in csr.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME)] == ["A", "B"]


@pytest.mark.parametrize("data", [b"", b"a", b"a" * 16, b"a" * 17, bytes(range(256))])
def test_encryption_handles_padding_boundaries_and_hash_encodings(data):
    key = SymmetricKey.generate()
    encrypted = key.encrypt(data)
    assert len(key.key) == 32 and len(key.iv) == 16
    assert len(encrypted) % 16 == 0 and len(encrypted) > len(data)
    assert key.decrypt(encrypted) == data
    assert base64.b64decode(sha256_b64(data)) == base64.urlsafe_b64decode(sha256_b64url(data) + "=")
    assert b64(data) == base64.b64encode(data).decode()


def test_decrypt_rejects_invalid_ciphertext():
    key = SymmetricKey.generate()
    with pytest.raises(ValueError):
        key.decrypt(b"invalid")
