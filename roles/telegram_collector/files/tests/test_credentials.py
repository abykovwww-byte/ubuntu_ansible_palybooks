from types import SimpleNamespace

import pytest

from tg_archive import credentials
from tg_archive.config import ConfigError


def test_session_roundtrip_uses_only_explicit_vault(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "win32")
    stored = {}
    vault = SimpleNamespace(
        get_password=lambda service, account: stored.get((service, account)),
        set_password=lambda service, account, value: stored.update({(service, account): value}),
    )
    monkeypatch.setattr(credentials, "windows_vault", lambda: vault)
    credentials.save_session("SYNTHETIC_SESSION")
    assert credentials.load_session() == "SYNTHETIC_SESSION"
    assert list(stored) == [(credentials.SERVICE, credentials.ACCOUNT)]


def test_missing_session_requires_owner_login(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "win32")
    monkeypatch.setattr(
        credentials, "windows_vault", lambda: SimpleNamespace(get_password=lambda *args: None)
    )
    with pytest.raises(ConfigError, match="owner must run"):
        credentials.load_session()


def test_non_windows_never_uses_fallback_backend(monkeypatch):
    monkeypatch.setattr(credentials.sys, "platform", "linux")
    with pytest.raises(ConfigError, match="Windows Credential Manager"):
        credentials.windows_vault()
