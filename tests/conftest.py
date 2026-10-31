import shutil
from pathlib import Path

import pytest

from ksefik.profile import load_profile

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clipboard(monkeypatch):
    """Schowek systemowy w testach: zapamiętuje kopiowane teksty zamiast zmieniać schowek użytkownika."""
    from ksefik.ui import app

    copied: list[str] = []
    monkeypatch.setattr(app, "system_clipboard", lambda text: copied.append(text) or True)
    return copied


@pytest.fixture(autouse=True)
def browser(monkeypatch):
    """Przeglądarka w testach: zapamiętuje otwierane adresy zamiast je otwierać."""
    from textual.app import App

    from ksefik.ui import app

    opened: list[str] = []
    monkeypatch.setattr(App, "open_url", lambda self, url, **kwargs: opened.append(url))
    monkeypatch.setattr(app, "can_open_browser", lambda: True)
    return opened


@pytest.fixture
def example_path(tmp_path):
    path = tmp_path / "profile.toml"
    shutil.copy(ROOT / "profile.example.toml", path)
    return path


@pytest.fixture
def profile(example_path):
    return load_profile(example_path)


@pytest.fixture
def month_end_profile(profile):
    """Profil z numeracją N/M/RRRR i datami na koniec poprzedniego miesiąca (do 10. dnia)."""
    from dataclasses import replace

    from ksefik.profile import InvoiceSettings

    invoice = InvoiceSettings(
        numeracja="{nr}/{m}/{rrrr}", daty="koniec_miesiaca", dzien_graniczny=10, termin_platnosci_dni=10
    )
    return replace(profile, invoice=invoice)


@pytest.fixture
def ui(profile, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from ksefik import profile as profile_module
    from ksefik.store import Store
    from ksefik.ui import app as app_module
    from ksefik.ui import certificates, common
    from ksefik.ui.app import KsefApp

    store = Store(tmp_path / "ui.db")
    fake = AsyncMock()
    fake.fetch_upo.return_value = None  # UPO jeszcze niedostępne (testy ustawiają je same)

    class Harness(KsefApp):
        CSS_PATH = ROOT / "src/ksefik/ui/app.tcss"

        def on_mount(self, event):
            # Textual wywołuje on_mount każdej klasy w MRO - bez prevent_default ruszyłby normalny start (logowanie).
            event.prevent_default()
            self.theme = self.profile.theme

    app = Harness(profile, profile_path=profile.path, store=store)
    app.client = fake
    monkeypatch.setattr(app, "get_client", AsyncMock(return_value=fake))
    monkeypatch.setattr(common, "downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module, "downloads_dir", lambda: tmp_path)
    monkeypatch.setattr(profile_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(certificates, "CERT_DIR", tmp_path / "certificates")
    yield app, fake, store
    store.close()
