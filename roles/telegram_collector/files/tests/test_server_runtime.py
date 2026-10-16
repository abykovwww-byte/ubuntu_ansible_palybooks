import asyncio
import os
import sys
from dataclasses import replace
from types import SimpleNamespace

import asyncssh
import pytest

from tg_archive import credentials, network
from tg_archive.config import ConfigError
from tg_archive.local_filesystem import LocalFilesystem
from tg_archive.route import RouteGuard
from tg_archive.scheduled import collector_command


def test_linux_telegram_proxy_is_explicit_and_has_no_environment_fallback(monkeypatch):
    monkeypatch.setattr(network.sys, "platform", "linux")
    monkeypatch.setenv("ALL_PROXY", "http://unrelated.invalid:123")
    proxy = network.system_proxy()
    assert (proxy["proxy_type"], proxy["addr"], proxy["port"]) == ("socks5", "vpn-egress", 1080)
    assert proxy["rdns"] is True


def test_server_runner_passes_the_exact_config_to_the_child(settings, tmp_path):
    config = tmp_path / "config.toml"
    command, _ = collector_command(
        settings, SimpleNamespace(config=config, chat=None, no_media=False)
    )
    assert command[3:] == ["--config", str(config), "run"]


def test_local_storage_has_no_tailscale_dependency(settings, monkeypatch):
    monkeypatch.setattr(
        "tg_archive.route.shutil.which", lambda _: pytest.fail("Tailscale dependency")
    )
    media = replace(settings.media_settings, storage="local")
    assert asyncio.run(RouteGuard(media).allowed()) is True


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX private-file permissions")
def test_server_session_refuses_world_readable_files_and_symlinks(tmp_path, monkeypatch):
    session = tmp_path / "session"
    session.write_text("SYNTHETIC_SESSION", encoding="ascii")
    monkeypatch.setattr(credentials, "Path", lambda _: session)
    session.chmod(0o644)
    with pytest.raises(ConfigError, match="private regular"):
        credentials.load_session()
    session.chmod(0o400)
    assert credentials.load_session() == "SYNTHETIC_SESSION"
    link = tmp_path / "link"
    link.symlink_to(session)
    monkeypatch.setattr(credentials, "Path", lambda _: link)
    with pytest.raises(ConfigError, match="private regular"):
        credentials.load_session()


@pytest.mark.skipif(os.name == "nt", reason="POSIX atomic media files")
def test_local_media_streams_and_renames_without_changing_relative_paths(tmp_path):
    async def verify():
        fs = LocalFilesystem()
        root = str(tmp_path)
        await fs.mkdir_parents(root, "synthetic/2026-10/1.bin")
        async with fs.open_part(root, "synthetic/2026-10/1.bin.part") as output:
            await output.write(b"synthetic")
        await fs.rename(root, "synthetic/2026-10/1.bin.part", "synthetic/2026-10/1.bin")
        assert await fs.list_files(root) == {"synthetic/2026-10/1.bin": 9}
        assert await fs.stat(root, "synthetic/2026-10/1.bin") == 9
        await fs.remove(root, "synthetic/2026-10/1.bin")
        with pytest.raises(asyncssh.SFTPNoSuchFile):
            await fs.stat(root, "synthetic/2026-10/1.bin")

    asyncio.run(verify())


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture")
def test_local_listing_aborts_on_symlink_before_fsck_can_mutate(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    media = tmp_path / "media"
    media.mkdir()
    (media / "bad").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlink or special"):
        asyncio.run(LocalFilesystem().list_files(str(media)))
    with pytest.raises(ValueError, match="real directory"):
        asyncio.run(LocalFilesystem().mkdir_parents(str(media), "bad/file.part"))


def test_database_advisory_lock_rejects_concurrent_run_before_interrupted_update():
    from tg_archive.database import Store
    from tg_archive.locking import AlreadyRunning

    queries = []

    async def fetch():
        return {"acquired": False}

    async def execute(query):
        queries.append(query)
        return SimpleNamespace(fetchone=fetch)

    with pytest.raises(AlreadyRunning):
        asyncio.run(Store(SimpleNamespace(execute=execute)).begin(None))
    assert len(queries) == 1
    assert "pg_try_advisory_lock" in queries[0]
