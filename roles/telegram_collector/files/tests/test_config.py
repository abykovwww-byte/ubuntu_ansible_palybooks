from pathlib import Path

import pytest

from tg_archive.cli import main
from tg_archive.config import ConfigError, load_env, load_settings
from tg_archive.locking import AlreadyRunning, sync_lock


def test_check_never_accesses_network_or_vault(project, monkeypatch, capsys):
    import tg_archive.cli as cli

    def forbidden(*args, **kwargs):
        raise AssertionError("Network/vault must not be called")

    monkeypatch.setattr(cli, "make_client", forbidden)
    monkeypatch.setattr(cli, "load_session", forbidden)
    assert main(["--config", str(project / "tg-archive.toml"), "--check"]) == 0
    assert '"network_used": false' in capsys.readouterr().out


def test_secrets_are_not_in_settings_repr(project):
    value = "a" * 32
    (project / "sync.env").write_text(f"TG_API_ID=123\nTG_API_HASH={value}\n", encoding="utf-8")
    settings = load_settings(project / "tg-archive.toml")
    assert value not in repr(settings)
    assert "api_id" not in repr(settings)


def test_mcp_env_is_not_loaded(project):
    (project / "mcp.env").write_text("TG_API_ID=bad\nTG_API_HASH=bad\n", encoding="utf-8")
    assert load_settings(project / "tg-archive.toml").api_hash is None


@pytest.mark.parametrize(
    "content",
    [
        "TG_API_ID=1\nTG_API_ID=2",
        "TG_STRING_SESSION=SYNTHETIC",
        "malformed",
        'TG_API_HASH="unclosed',
    ],
)
def test_invalid_env_fails_without_echoing_values(project, content):
    path = project / "sync.env"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigError) as error:
        load_env(path)
    assert content not in str(error.value)


@pytest.mark.parametrize(
    "entry",
    [
        "id: 123\n    slug: candidate",
        "id: -123\n    slug: ../../bad",
        "id: -123\n    slug: candidate\n    media: nope",
    ],
)
def test_unsafe_chat_config_rejected(project, entry):
    (project / "chats.yaml").write_text("chats:\n  - " + entry + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(project / "tg-archive.toml")


def test_lock_prevents_overlap_and_releases(project):
    with sync_lock(project), pytest.raises(AlreadyRunning), sync_lock(project):
        pass
    with sync_lock(project):
        pass


def test_network_requires_credentials(project, capsys):
    assert main(["--config", str(project / "tg-archive.toml"), "discover", "--chat", "-123"]) == 1
    assert "TG_API_ID" in capsys.readouterr().err


def test_no_session_files_in_project():
    root = Path(__file__).resolve().parents[1]
    assert not list((root / "src").rglob("*.session"))


def test_fsck_does_not_access_telegram_or_vault(project, monkeypatch, capsys):
    import tg_archive.cli as cli

    def forbidden(*args, **kwargs):
        raise AssertionError("fsck must not access Telegram or session")

    async def preview(*args, **kwargs):
        assert kwargs == {"dry_run": True, "confirm_mass": False}
        return {"fsck_status": "ok", "dry_run": True}

    monkeypatch.setattr(cli, "make_client", forbidden)
    monkeypatch.setattr(cli, "load_session", forbidden)
    monkeypatch.setattr(cli.maintenance, "fsck", preview)
    assert main(["--config", str(project / "tg-archive.toml"), "fsck", "--dry-run"]) == 0
    assert '"dry_run": true' in capsys.readouterr().out
