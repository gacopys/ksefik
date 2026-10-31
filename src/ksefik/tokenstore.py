"""Przechowywanie tokena KSeF: systemowy keyring, a gdy go brak - plik z prawami 0600."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from .config import CONFIG_DIR

SERVICE = "ksefik"


def _account(env: str, nip: str) -> str:
    return f"{env}:{nip}"


def token_file(env: str, nip: str) -> Path:
    """Plik z tokenem używany, gdy system nie ma pęku kluczy."""
    return CONFIG_DIR / f"token-{env}-{nip}"


def _keyring():
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None


def storage_label(env: str, nip: str) -> str:
    """Gdzie zostanie zapisany token - opis do pokazania przed zapisem (zależy od tego, czy jest pęk kluczy)."""
    if _keyring():
        return "w systemowym pęku kluczy"
    return f"w pliku {token_file(env, nip)} (dostęp tylko dla Ciebie)"


def env_token_set() -> bool:
    """Czy token podano w zmiennej KSEF_TOKEN - ma wtedy pierwszeństwo przed zapisanym."""
    return bool(os.environ.get("KSEF_TOKEN", "").strip())


def load_token(env: str, nip: str) -> str | None:
    if token := os.environ.get("KSEF_TOKEN"):
        return token.strip()
    if kr := _keyring():
        try:
            if token := kr.get_password(SERVICE, _account(env, nip)):
                return token
        except Exception:  # pęk kluczy zablokowany lub uszkodzony - token może być jeszcze w pliku
            pass
    path = token_file(env, nip)
    if path.exists():
        return path.read_text(encoding="utf-8").strip() or None
    return None


def save_token(env: str, nip: str, token: str) -> str:
    """Zapisuje token i zwraca opis miejsca zapisu (do pokazania użytkownikowi)."""
    if kr := _keyring():
        try:
            kr.set_password(SERVICE, _account(env, nip), token)
            return "systemowy pęk kluczy"
        except Exception:  # pęk kluczy niedostępny - zapis do pliku z prawami 0600 poniżej
            pass
    path = token_file(env, nip)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(token, encoding="utf-8")
    return str(path)


def delete_token(env: str, nip: str) -> None:
    if kr := _keyring():
        with contextlib.suppress(Exception):
            kr.delete_password(SERVICE, _account(env, nip))
    token_file(env, nip).unlink(missing_ok=True)
