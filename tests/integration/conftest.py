"""Testy end-to-end z testowym KSeF (https://api-test.ksef.mf.gov.pl) jako backendem.

Uruchamiaj wyłącznie przez `make run-integration-test` - ustawia osobny katalog domowy aplikacji
(XDG_CONFIG_HOME / XDG_DATA_HOME), wyłącza systemowy pęk kluczy i włącza zmienną KSEF_INTEGRATION.

Każde uruchomienie tworzy nowe, fikcyjne firmy (losowe NIP-y) uwierzytelniane samopodpisaną pieczęcią -
KSeF akceptuje takie certyfikaty tylko na środowisku testowym. Dzięki temu dane testów nie mieszają się
między uruchomieniami, a limity zapytań (np. 20 eksportów na godzinę) liczą się od zera.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from ksef_env import Company, issue_token, make_company

log = logging.getLogger("ksef_it")
# Zapytania loguje ksefik.client (z kodem błędu KSeF); niskopoziomowe logi połączeń tylko zaśmiecają przebieg.
for noisy in ("httpcore", "httpx", "asyncio", "markdown_it"):
    logging.getLogger(noisy).setLevel(logging.WARNING)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item, call):
    report = yield
    if report.when == "call" or report.failed:
        log.info("=== %s %s: %s (%.1fs)", report.when.upper(), item.nodeid, report.outcome, report.duration)
    return report


def pytest_runtest_logstart(nodeid, location):
    log.info("=== START %s", nodeid)


def pytest_collection_modifyitems(config, items):
    if os.environ.get("KSEF_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="testy z prawdziwym KSeF - uruchom: make run-integration-test")
    for item in items:
        if "integration" in str(item.fspath):
            item.add_marker(skip)


def _check_isolation() -> None:
    """Bezpiecznik: testy nie mogą dotknąć prawdziwego profilu, bazy ani tokenów użytkownika."""
    from ksefik.config import CONFIG_DIR, DATA_ROOT

    home = Path.home()
    for path in (CONFIG_DIR, DATA_ROOT):
        if path.is_relative_to(home / ".config") or path.is_relative_to(home / ".local"):
            pytest.exit(f"Testy integracyjne wskazują na prawdziwy katalog {path} - uruchom: make run-integration-test")
    if os.environ.get("PYTHON_KEYRING_BACKEND") != "keyring.backends.fail.Keyring":
        pytest.exit("Testy integracyjne wymagają wyłączonego pęku kluczy - uruchom: make run-integration-test")


@pytest.fixture(scope="session")
def it_home() -> Path:
    _check_isolation()
    return Path(os.environ.get("KSEF_IT_HOME") or Path(os.environ["XDG_DATA_HOME"]).parent)


@pytest.fixture(scope="session")
def company(it_home) -> Company:
    """Firma, w imieniu której działa aplikacja (sprzedawca)."""
    company = make_company("Firma Testowa", it_home / "pieczecie")
    company.token = asyncio.run(issue_token(company))
    return company


@pytest.fixture(scope="session")
def partner(it_home) -> Company:
    """Druga firma: kontrahent, który wystawia faktury dla `company` i nadaje jej uprawnienia."""
    return make_company("Kontrahent Testowy", it_home / "pieczecie")


OPENED_URLS: list[str] = []


@pytest.fixture
def opened_urls() -> list[str]:
    """Adresy, które aplikacja próbowała otworzyć w przeglądarce (w testach nic się nie otwiera)."""
    return OPENED_URLS


@pytest.fixture
def downloads(tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "Pobrane"
    folder.mkdir()
    import ksefik.ui.app as app_module
    import ksefik.ui.common as common

    monkeypatch.setattr(common, "downloads_dir", lambda: folder)
    monkeypatch.setattr(app_module, "downloads_dir", lambda: folder)  # inaczej pliki trafiłyby do ~/Downloads
    return folder


@pytest.fixture(autouse=True)
def isolated_app(tmp_path, monkeypatch, downloads) -> Iterator[Path]:
    """Każdy test ma własny katalog danych i konfiguracji; aplikacja nie otwiera przeglądarki."""
    _check_isolation()
    import ksefik.profile as profile_module
    import ksefik.tokenstore as tokenstore
    import ksefik.ui.certificates as certificates
    from ksefik.ui.app import KsefApp

    monkeypatch.setattr(profile_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(tokenstore, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(certificates, "CERT_DIR", tmp_path / "config" / "certyfikaty")
    monkeypatch.setattr(KsefApp, "open_url", lambda self, url, **kw: OPENED_URLS.append(url))
    _log_app_events(monkeypatch, KsefApp)
    OPENED_URLS.clear()
    monkeypatch.delenv("KSEF_TOKEN", raising=False)
    monkeypatch.delenv("KSEF_KEY_PASSWORD", raising=False)
    yield tmp_path


def _log_app_events(monkeypatch, app_class) -> None:
    """Powiadomienia i zmiany ekranów aplikacji trafiają do logu przebiegu."""
    notify, push, pop = app_class.notify, app_class.push_screen, app_class.pop_screen

    def logged_notify(self, message, *, title="", severity="information", **kwargs):
        log.info("Powiadomienie [%s] %s: %s", severity, title, str(message).replace("\n", " | "))
        return notify(self, message, title=title, severity=severity, **kwargs)

    def logged_push(self, screen, *args, **kwargs):
        log.info("Ekran + %s", type(screen).__name__ if not isinstance(screen, str) else screen)
        return push(self, screen, *args, **kwargs)

    def logged_pop(self):
        log.info("Ekran - %s", type(self.screen).__name__)
        return pop(self)

    monkeypatch.setattr(app_class, "notify", logged_notify)
    monkeypatch.setattr(app_class, "push_screen", logged_push)
    monkeypatch.setattr(app_class, "pop_screen", logged_pop)
