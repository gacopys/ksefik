from unittest.mock import Mock

import pytest

from ksefik import tokenstore


@pytest.fixture(autouse=True)
def isolated_tokens(tmp_path, monkeypatch):
    monkeypatch.setattr(tokenstore, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.delenv("KSEF_TOKEN", raising=False)
    monkeypatch.setattr(tokenstore, "_keyring", lambda: None)


def test_environment_overrides_saved_token(monkeypatch):
    tokenstore.save_token("test", "123", "saved")
    monkeypatch.setenv("KSEF_TOKEN", " env-token \n")
    assert tokenstore.load_token("test", "123") == "env-token"


def test_file_fallback_permissions_isolation_overwrite_and_delete():
    assert tokenstore.load_token("test", "123") is None
    destination = tokenstore.save_token("test", "123", "first")
    path = tokenstore.token_file("test", "123")
    assert destination == str(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert tokenstore.load_token("prod", "123") is None
    assert tokenstore.load_token("test", "456") is None
    path.chmod(0o644)
    tokenstore.save_token("test", "123", "second")
    assert path.stat().st_mode & 0o777 == 0o600
    assert tokenstore.load_token("test", "123") == "second"
    tokenstore.delete_token("test", "123")
    tokenstore.delete_token("test", "123")
    assert not path.exists()


def test_empty_fallback_file_is_not_a_token():
    tokenstore.save_token("demo", "123", " \n")
    assert tokenstore.load_token("demo", "123") is None


def test_keyring_preferred_for_read_and_write(monkeypatch):
    kr = Mock()
    kr.get_password.return_value = "secret"
    monkeypatch.setattr(tokenstore, "_keyring", lambda: kr)
    assert tokenstore.load_token("test", "123") == "secret"
    kr.get_password.assert_called_once_with("ksefik", "test:123")
    assert tokenstore.save_token("test", "123", "new") == "systemowy pęk kluczy"
    kr.set_password.assert_called_once_with("ksefik", "test:123", "new")
    assert not tokenstore.token_file("test", "123").exists()
    tokenstore.delete_token("test", "123")
    kr.delete_password.assert_called_once_with("ksefik", "test:123")


@pytest.mark.parametrize("failure", [False, True])
def test_unavailable_or_empty_keyring_falls_back(monkeypatch, failure):
    tokenstore.save_token("test", "123", "disk")
    kr = Mock()
    kr.get_password.return_value = None
    if failure:
        kr.get_password.side_effect = RuntimeError("locked")
    kr.set_password.side_effect = RuntimeError("locked")
    kr.delete_password.side_effect = RuntimeError("locked")
    monkeypatch.setattr(tokenstore, "_keyring", lambda: kr)
    assert tokenstore.load_token("test", "123") == "disk"
    tokenstore.save_token("test", "123", "changed")
    assert tokenstore.load_token("test", "123") == "changed"
    tokenstore.delete_token("test", "123")
    assert not tokenstore.token_file("test", "123").exists()


def test_keyring_backend_detection(monkeypatch):
    import keyring
    from keyring.backends.fail import Keyring

    monkeypatch.undo()
    monkeypatch.setattr(keyring, "get_keyring", Keyring)
    assert tokenstore._keyring() is None
    monkeypatch.setattr(keyring, "get_keyring", Mock(side_effect=RuntimeError("unavailable")))
    assert tokenstore._keyring() is None
    monkeypatch.setattr(keyring, "get_keyring", lambda: object())
    assert tokenstore._keyring() is keyring
