from datetime import UTC, datetime, timedelta
from pathlib import Path

from tg_archive.database import is_stale
from tg_archive.runtime import run_async


def test_stale_boundary_and_unknown_freshness():
    now = datetime(2026, 10, 12, tzinfo=UTC)
    assert is_stale(None, now)
    assert not is_stale(now - timedelta(hours=36), now)
    assert is_stale(now - timedelta(hours=36, seconds=1), now)


def test_runtime_supports_async_psycopg_on_windows():
    import asyncio
    import sys

    async def verify():
        if sys.platform == "win32":
            assert isinstance(asyncio.get_running_loop(), asyncio.SelectorEventLoop)
        return "ok"

    assert run_async(verify()) == "ok"


def test_migration_uses_only_the_specified_model():
    import re

    root = Path(__file__).resolve().parents[1]
    source = (root / "migrations/001_archive.sql").read_text(encoding="utf-8")
    assert set(re.findall(r"create table if not exists (\w+)", source)) == {
        "sync_run",
        "tg_chat",
        "tg_user",
        "tg_topic",
        "tg_message",
    }
    assert "access_hash" not in source and "session" not in source.lower()
    assert "gin_trgm_ops" in source and "to_tsvector('russian'" in source
