"""Kryptografia wymagana przez KSeF 2.0: RSA-OAEP(SHA-256) dla kluczy i AES-256-CBC dla dokumentów."""

from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def sha256_b64(data: bytes) -> str:
    return b64(hashlib.sha256(data).digest())


def sha256_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode("ascii").rstrip("=")


def rsa_oaep_encrypt(cert_der_b64: str, plaintext: bytes) -> bytes:
    cert = x509.load_der_x509_certificate(base64.b64decode(cert_der_b64))
    return cert.public_key().encrypt(
        plaintext,
        asym_padding.OAEP(mgf=asym_padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None),
    )


@dataclass
class SymmetricKey:
    key: bytes
    iv: bytes

    @classmethod
    def generate(cls) -> SymmetricKey:
        return cls(key=os.urandom(32), iv=os.urandom(16))

    def encrypt(self, data: bytes) -> bytes:
        padder = padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).encryptor()
        return encryptor.update(padded) + encryptor.finalize()

    def decrypt(self, data: bytes) -> bytes:
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.iv)).decryptor()
        padded = decryptor.update(data) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
