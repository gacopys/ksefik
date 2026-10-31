import pytest

from ui.harness import build_app


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Dane, pobrane pliki i certyfikaty aplikacji trafiają do katalogu tymczasowego."""
    from ksefik import profile as profile_module
    from ksefik.ui import app, certificates, common

    monkeypatch.setattr(common, "downloads_dir", lambda: tmp_path / "Pobrane")
    monkeypatch.setattr(app, "downloads_dir", lambda: tmp_path / "Pobrane")
    (tmp_path / "Pobrane").mkdir()
    monkeypatch.setattr(profile_module, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(certificates, "CERT_DIR", tmp_path / "certificates")
    monkeypatch.delenv("KSEF_TOKEN", raising=False)
    return tmp_path


@pytest.fixture
def seeded(isolated):
    """(aplikacja, sztuczny klient, baza) - baza z przykładowymi fakturami."""
    app, fake, store = build_app(isolated)
    yield app, fake, store
    store.close()


@pytest.fixture
def empty(isolated):
    app, fake, store = build_app(isolated, seed=False)
    yield app, fake, store
    store.close()
