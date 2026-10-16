import asyncio
import importlib
import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tg_archive.fetch_cache import FetchSettings, checked_path, cleanup, mark_web
from tg_archive.locking import AlreadyRunning, sync_lock
from tg_archive.reader import attachment, bound, link, project_message, search_plan, trim_window
from tg_archive.reader_config import load_reader, snapshot
from tg_archive.sync_add import add, resolve


def row(msg=1, text="synthetic", **kwargs):
    return dict(
        chat_id=-1001234567890,
        slug="example",
        msg_id=msg,
        text=text,
        reply_to_id=kwargs.get("reply_to_id"),
        sender_id=123,
        sent_at=datetime(2026, 10, 13, tzinfo=UTC),
        kind="supergroup",
        public_username=None,
        topic_id=None,
        media_kind=None,
        **{k: v for k, v in kwargs.items() if k != "reply_to_id"},
    )


@pytest.fixture
def reader_settings(tmp_path):
    config = tmp_path / "tg-archive.toml"
    config.write_text(
        "default_since=2025-01-01\nrefresh_days=3\n[media]\n"
        "sftp_host='127.0.0.1'\nsftp_port=22\nremote_root='/media'\n"
        f"[fetch]\nlocal_dir='{tmp_path / 'fetch'}'\nmax_mb=200\nttl_hours=24\n",
        encoding="utf-8",
    )
    env = tmp_path / "mcp.env"
    env.write_text(
        "TG_DB_READER_DSN=postgresql://tg_reader@localhost/test\nTG_SFTP_RO_KEY=test\n",
        encoding="utf-8",
    )
    (tmp_path / "chats.yaml").write_text("# owner selection\nchats: []\n", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state/dialogs.json").write_text(
        json.dumps(
            [
                dict(
                    chat_id=-1001234567890,
                    raw_channel_id=1234567890,
                    kind="supergroup",
                    title="Synthetic group",
                    username="test_chat",
                    snapshot_at="2026-10-13T00:00:00Z",
                ),
                dict(chat_id=-42, raw_channel_id=None, kind="group", title="Small group"),
                dict(chat_id=99, kind="user", title="Person", username="person"),
            ]
        ),
        encoding="utf-8",
    )
    return load_reader(config, env)


@pytest.mark.parametrize(
    "query", ["SOC-12", "schema.vsdx", "10.0.0.1", "abc123", "/opt/test", "HOST_1"]
)
def test_identifier_search_includes_text_and_unsaved_media(query):
    where, params, _, rank, match = search_plan(query=query)
    assert match == "substring" and "m.text ilike" in where and "m.media_name ilike" in where
    assert params == ["%" + query + "%", "%" + query + "%"] or "_" in query
    assert rank == []


@pytest.mark.parametrize("query", ["обновление схемы", "archive", '"новая схема" -старая'])
def test_fulltext_search_and_relevance(query):
    where, params, order, rank, match = search_plan(query=query)
    assert "websearch_to_tsquery('russian', %s)" in where
    assert params == [query] and rank == [query] and order.startswith("ts_rank")
    assert match == "text"


def test_filters_parameterized_and_wildcards_literal():
    payload = "x' OR 1=1 --"
    where, params, _, _, _ = search_plan(sender=payload, ext=["xlsx"], has_media=True)
    assert payload not in where and pattern_in(params, payload)
    assert "m.media_ext = any(%s)" in where and "is not null" in where
    assert search_plan(query="SOC_12")[1] == [r"%SOC\_12%"] * 2


def pattern_in(params, value):
    return "%" + value + "%" in params


@pytest.mark.parametrize(
    "filters",
    [{}, {"query": " "}, {"ext": [".pdf"]}, {"ext": ["PDF"]}, {"query": "SOC-1", "chats": []}],
)
def test_invalid_filters_rejected(filters):
    with pytest.raises(ValueError):
        search_plan(**filters)


def test_dates_are_explicit():
    assert bound("2025-05-01").tzinfo is UTC
    assert bound("2025-05-01", end=True).day == 2
    assert bound("2025-05-01T12:00:00+03:00").utcoffset().total_seconds() == 10800
    with pytest.raises(ValueError):
        bound("2025-05-01T12:00:00")


def test_blank_optional_terms_do_not_hide_extension_matches():
    where, params, _, _, _ = search_plan(query=" ", sender=" ", ext=["pdf"])
    assert where == "m.media_ext = any(%s)" and params == [["pdf"]]


def test_budget_counts_reply_parents_and_retains_anchor():
    rows = [row(i, "x" * 8000, reply_to_id=1000 + i) for i in range(1, 301)]
    parents = [row(1000 + i, "p" * 8000) for i in range(1, 301)]
    kept, replies, before, after, reason = trim_window(rows, parents, limit=300, anchor=150)
    assert any(r["msg_id"] == 150 for r in kept)
    assert sum(len(r["text"]) for r in kept + replies) <= 60000
    assert before and after and reason == "budget"
    assert len(kept) + len(replies) <= 300
    assert {r["msg_id"] for r in replies} == {r["reply_to_id"] for r in kept}


def test_date_window_budget_trims_end_and_one_level():
    rows = [row(i, "x" * 8000, reply_to_id=999) for i in range(1, 20)]
    kept, replies, before, after, reason = trim_window(rows, [row(999, "y" * 8000)], limit=100)
    assert [r["msg_id"] for r in kept] == list(range(1, 7))
    assert len(replies) == 1 and not before and after and reason == "budget"


def test_parent_in_window_not_duplicated_and_limit_counts_parents():
    kept, replies, _, _, reason = trim_window([row(1), row(2, reply_to_id=1)], [row(1)], limit=2)
    assert len(kept) == 2 and not replies and not reason
    kept, replies, _, _, reason = trim_window(
        [row(1, reply_to_id=99)], [row(99)], limit=1, anchor=1
    )
    assert len(kept) == 1 and not replies and reason == "limit"


def test_anomalous_text_and_snippet_caps():
    source = row(text="x" * 12000)
    assert len(project_message(source, 100)["snippet"]) == 300
    full = project_message(source, 100, full=True)
    assert full["text_truncated"] and len(full["text"]) == 8000


@pytest.mark.parametrize(
    "kind,username,topic,expected",
    [
        ("group", None, None, None),
        ("supergroup", "public", None, "https://t.me/public/1"),
        ("channel", None, None, "https://t.me/c/1234567890/1"),
        ("forum", "public", 10, "https://t.me/c/1234567890/10/1"),
    ],
)
def test_links(kind, username, topic, expected):
    source = row()
    source.update(kind=kind, public_username=username, topic_id=topic)
    assert link(source) == expected


@pytest.mark.parametrize(
    "status,fetchable",
    [
        ("done", True),
        ("pending", False),
        ("failed", False),
        ("skipped_kind", False),
        ("skipped_protected", False),
        ("skipped_disabled", False),
        ("skipped_quota", False),
        ("skipped_too_big", False),
    ],
)
def test_every_attachment_status_has_metadata(status, fetchable):
    source = dict(
        media_kind="document",
        media_name="report.xlsx",
        media_ext="xlsx",
        media_size=42,
        media_status=status,
        media_error="timeout",
    )
    result = attachment(source, 200)
    assert result["fetchable"] == fetchable and result["ext"] == "xlsx"
    assert ("error" in result) == (status == "failed")
    assert not attachment(dict(source, media_size=None), 200)["fetchable"]
    assert not attachment(dict(source, media_size=201), 200)["fetchable"]


@pytest.mark.parametrize(
    "selector",
    [
        "-1001234567890",
        "1234567890",
        "@test_chat",
        "https://t.me/test_chat/50",
        "t.me/c/1234567890/50",
        "Synthetic group",
    ],
)
def test_snapshot_resolves_channel_formats(reader_settings, selector):
    rows, _ = snapshot(reader_settings.root)
    assert resolve(selector, rows) == {"id": -1001234567890, "verified": True}


def test_reject_users_unknown_positive_and_unresolvable_names(reader_settings):
    rows, _ = snapshot(reader_settings.root)
    assert resolve("@person", rows)["status"] == "rejected"
    assert resolve("999", rows)["status"] == "rejected"
    assert resolve("@unknown_chat", rows)["status"] == "unresolved"
    assert resolve("t.me/c/9876543210/1", rows) == {"id": -1009876543210, "verified": False}
    assert resolve("-42", rows) == {"id": -42, "verified": True}


def test_ambiguous_title_does_not_write(reader_settings):
    rows, _ = snapshot(reader_settings.root)
    rows.append(dict(rows[0], chat_id=-1009999999999))
    assert resolve("Synthetic group", rows)["status"] == "ambiguous"


def test_add_preserves_comments_and_existing_entry(reader_settings):
    result = add(reader_settings, "@test_chat", "Synthetic project", "2025-05-01", "example")
    assert result["status"] == "added" and result["entry"]["verified"]
    entry = result["entry"]
    assert entry["added_by"] == "llm" and "allow_protected" not in entry and "media" not in entry
    path = reader_settings.root / "chats.yaml"
    assert path.read_text(encoding="utf-8").startswith("# owner selection")
    content = path.read_bytes()
    result = add(reader_settings, "1234567890", "Another project", None, "new-slug")
    assert result["status"] == "existing" and path.read_bytes() == content


def test_add_honors_collector_lock(reader_settings):
    with sync_lock(reader_settings.root), pytest.raises(AlreadyRunning):
        add(reader_settings, "-42", None, None, None)


def test_corrupt_snapshot_explicit(reader_settings):
    (reader_settings.root / "state/dialogs.json").write_text("broken", encoding="utf-8")
    assert snapshot(reader_settings.root) == ([], "unavailable")


def test_reader_config_rejects_writer_and_sync_env(reader_settings):
    config = reader_settings.root / "tg-archive.toml"
    with pytest.raises(ValueError):
        load_reader(config, reader_settings.root / "sync.env")
    (reader_settings.root / "mcp.env").write_text(
        "TG_DB_READER_DSN=postgresql://tg_writer@localhost/test\nTG_SFTP_RO_KEY=test\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_reader(config, reader_settings.root / "mcp.env")


def test_ttl_cleanup_only_managed_regular_copies(tmp_path):
    root = tmp_path / "fetch"
    directory = root / "example"
    directory.mkdir(parents=True)
    expired, fresh, unrelated = [directory / n for n in ("1-old.pdf", "2-new.pdf", "notes.txt")]
    for path in (expired, fresh, unrelated):
        path.write_bytes(b"synthetic")
    os.utime(expired, (0, 0))
    os.utime(unrelated, (0, 0))
    settings = FetchSettings(root, 200, 24)
    assert cleanup(settings) == 1
    assert not expired.exists() and fresh.exists() and unrelated.exists()


def test_symlink_or_junction_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Host does not permit creating test symlinks")
    with pytest.raises(ValueError):
        checked_path(alias / "file")


@pytest.mark.skipif(os.name != "nt", reason="Windows ADS")
def test_mark_of_the_web(tmp_path):
    path = tmp_path / "synthetic.txt"
    path.write_text("synthetic", encoding="ascii")
    mark_web(path)
    assert "ZoneId=3" in Path(str(path) + ":Zone.Identifier").read_text(encoding="ascii")


def test_sdk_exactly_five_tools_and_annotations(reader_settings):
    from tg_archive.mcp_server import create_server

    server = create_server(reader_settings)
    tools = asyncio.run(server.list_tools())
    assert {t.name for t in tools} == {
        "tg_chats",
        "tg_search",
        "tg_read",
        "tg_get_file",
        "tg_sync_add",
    }
    for tool in tools:
        assert tool.annotations.read_only_hint == (tool.name != "tg_sync_add")
        assert not tool.annotations.destructive_hint
    tool = next(t for t in tools if t.name == "tg_sync_add")
    assert set(tool.input_schema["properties"]) == {"chat", "project", "since", "slug"}


def test_mcp_import_graph_does_not_reach_collector(monkeypatch):
    forbidden = {
        "telethon",
        "keyring",
        "tg_archive.config",
        "tg_archive.credentials",
        "tg_archive.database",
        "tg_archive.sftp",
        "tg_archive.content",
    }
    original = __import__

    def guarded(name, *args, **kwargs):
        assert not any(name == n or name.startswith(n + ".") for n in forbidden)
        return original(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", guarded)
    for module in (
        "reader_config",
        "reader",
        "reader_files",
        "fetch_cache",
        "sync_add",
        "mcp_server",
    ):
        importlib.reload(sys.modules["tg_archive." + module])


def test_fresh_process_isolation():
    code = """
import sys
import tg_archive.mcp_server
for module in ('telethon','keyring','tg_archive.config','tg_archive.credentials',
               'tg_archive.database','tg_archive.sftp','tg_archive.content'):
    assert not any(n == module or n.startswith(module+'.') for n in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=15)
    assert result.returncode == 0, "MCP isolation failed (raw child output suppressed)"


def test_mcp_env_rejects_collector_fields(reader_settings):
    path = reader_settings.root / "mcp.env"
    path.write_text(path.read_text(encoding="utf-8") + "TG_API_HASH=forbidden\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_reader(reader_settings.root / "tg-archive.toml", path)
