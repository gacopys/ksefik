"""Ścieżki aplikacji i środowiska KSeF."""

from __future__ import annotations

import errno
import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

try:
    __version__ = version("ksefik")
except PackageNotFoundError:  # pragma: no cover - uruchomienie bez instalacji paczki
    __version__ = "0.0.0"

ENVIRONMENTS: dict[str, dict[str, str]] = {
    "prod": {
        "label": "Produkcja",
        "api": "https://api.ksef.mf.gov.pl/v2",
        "qr": "https://qr.ksef.mf.gov.pl",
    },
    "demo": {
        "label": "Demo (przedprodukcyjne)",
        "api": "https://api-demo.ksef.mf.gov.pl/v2",
        "qr": "https://qr-demo.ksef.mf.gov.pl",
    },
    "test": {
        "label": "Testowe",
        "api": "https://api-test.ksef.mf.gov.pl/v2",
        "qr": "https://qr-test.ksef.mf.gov.pl",
    },
}


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "ksefik"
DATA_ROOT = _xdg("XDG_DATA_HOME", ".local/share") / "ksefik"
PROFILE_PATH = CONFIG_DIR / "profile.toml"


def default_profile_path() -> Path:
    """Profil wskazany zmienną KSEFIK_PROFILE albo domyślny ~/.config/ksefik/profile.toml."""
    if custom := os.environ.get("KSEFIK_PROFILE"):
        return Path(custom).expanduser()
    return PROFILE_PATH


# Typowe błędy dysku po polsku (system podaje je zwykle po angielsku, np. „Permission denied”).
OS_ERRORS = {
    errno.EACCES: "brak uprawnień",
    errno.EPERM: "brak uprawnień",
    errno.ENOENT: "nie ma takiego pliku ani folderu",
    errno.ENOTDIR: "to nie jest folder",
    errno.EISDIR: "to jest folder, a nie plik",
    errno.ENOSPC: "brak miejsca na dysku",
    errno.EDQUOT: "przekroczony limit miejsca na dysku",
    errno.EROFS: "dysk jest tylko do odczytu",
    errno.EEXIST: "plik już istnieje",
    errno.ENAMETOOLONG: "za długa nazwa pliku",
    errno.EIO: "błąd odczytu lub zapisu dysku",
}


def os_error_text(exc: OSError) -> str:
    """Zrozumiały opis błędu pliku: po polsku dla typowych przypadków, inaczej tekst z systemu."""
    return OS_ERRORS.get(exc.errno or 0) or exc.strerror or str(exc)
