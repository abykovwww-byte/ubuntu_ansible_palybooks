import asyncio
import json
import shutil
from dataclasses import replace

import pytest
from test_reader import reader_settings as reader_settings

from tg_archive.reader import Reader
from tg_archive.reader_config import selection
from tg_archive.scope_server import dispatch


def seed(root):
    (root / "state").mkdir(exist_ok=True)
    (root / "chats.yaml").write_text("chats: []\n", encoding="utf-8")
    rows = [
        {
            "chat_id": -7,
            "title": "Synthetic group",
            "kind": "group",
            "snapshot_at": "2026-10-16T10:00:00+00:00",
        }
    ]
    (root / "state/dialogs.json").write_text(json.dumps(rows), encoding="utf-8")


def test_server_add_changes_the_collectors_authoritative_selection_and_reads_back(project):
    seed(project)
    result = dispatch(project, {"op": "add", "chat": "-7", "slug": "synthetic"})
    assert result["status"] == "added" and result["read_back"] is True
    assert result["authority"] == "server"
    assert selection(project)["chats"][0]["id"] == -7
    assert dispatch(project, {"op": "add", "chat": "-7"})["status"] == "existing"


def test_server_scope_snapshot_replaces_any_windows_selection(project):
    seed(project)
    result = dispatch(project, {"op": "snapshot"})
    assert result["selection"] == {"chats": []}
    assert result["dialogs"][0]["chat_id"] == -7
    assert result["snapshot_status"] == "ok"


@pytest.mark.parametrize(
    "payload",
    [
        {"op": "shell"},
        {"op": "snapshot", "path": "/etc/passwd"},
        {"op": "add", "chat": "-7", "media": True},
    ],
)
def test_scope_protocol_has_no_general_command_file_or_policy_operation(project, payload):
    seed(project)
    with pytest.raises(ValueError):
        dispatch(project, payload)


def test_mcp_remote_add_does_not_write_the_windows_selection(reader_settings, monkeypatch):
    from pathlib import Path

    from tg_archive.config import load_settings
    from tg_archive.mcp_server import create_server

    requests = []
    server_root = reader_settings.root / "server"
    server_root.mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[1] / "tg-archive.toml", server_root / "tg-archive.toml"
    )
    seed(server_root)

    async def remote(settings, payload):
        requests.append(payload)
        return dispatch(server_root, payload)

    monkeypatch.setattr("tg_archive.remote_scope.request_scope", remote)
    monkeypatch.setattr("tg_archive.mcp_server.add", lambda *args: pytest.fail("Local write"))
    settings = replace(reader_settings, scope_backend="server")
    server = create_server(settings)

    async def invoke():
        tool = server._tool_manager.get_tool("tg_sync_add")
        return await tool.fn(chat="-7")

    result = asyncio.run(invoke())
    assert result["authority"] == "server"
    assert load_settings(server_root / "tg-archive.toml").chats[0].chat_id == -7
    assert selection(reader_settings.root)["chats"] == []
    assert requests == [{"op": "add", "chat": "-7", "project": None, "since": None, "slug": None}]


def test_server_scope_unavailability_does_not_fall_back_to_windows(reader_settings, monkeypatch):
    async def fail(*args):
        raise OSError("Synthetic unavailable server")

    monkeypatch.setattr("tg_archive.remote_scope.request_scope", fail)
    settings = replace(reader_settings, scope_backend="server")
    with pytest.raises(OSError):
        asyncio.run(Reader(settings).chats())
