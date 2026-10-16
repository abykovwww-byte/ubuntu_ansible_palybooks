import asyncio
import json
from dataclasses import replace
from datetime import date

import pytest
from telethon import types
from test_collector import STAMP, MemoryStore, counts
from test_collector import Client as HistoryClient
from test_content import message
from test_discovery import Client

from tg_archive.collector import collect_chat
from tg_archive.config import ChatSpec, ConfigError, load_settings
from tg_archive.discovery import list_dialogs
from tg_archive.reader import link
from tg_archive.scope import peer_valid, private_ids
from tg_archive.sync_add import resolve


def test_private_positive_id_requires_explicit_kind_and_owner_allowlist():
    assert peer_valid(9, "private", (9,))
    assert peer_valid(9, "bot", (9,))
    assert not peer_valid(10, "private", (9,))
    assert not peer_valid(9, None, (9,))
    assert not peer_valid(9, "group", (9,))
    assert not peer_valid(-9, "private", (9,))


def test_private_approval_receipt_is_required(project):
    (project / "state").mkdir()
    path = project / "state/approved-private-peers.json"
    config = {"private_approval_file": "state/approved-private-peers.json"}
    assert private_ids(config, project) == ()
    path.write_text(
        json.dumps({"approved_by": "llm", "approved_private_ids": [9]}), encoding="utf-8"
    )
    with pytest.raises(ValueError):
        private_ids(config, project)
    path.write_text(
        json.dumps({"approved_by": "owner", "approved_private_ids": [9]}), encoding="utf-8"
    )
    assert private_ids(config, project) == (9,)
    with pytest.raises(ValueError):
        private_ids({"private_approval_file": "../arbitrary.json"}, project)


def test_config_still_rejects_unapproved_positive_id(project):
    path = project / "tg-archive.toml"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "refresh_days = 3", "approved_private_ids = [9]\nrefresh_days = 3"
        ),
        encoding="utf-8",
    )
    (project / "chats.yaml").write_text(
        "chats:\n- id: 9\n  slug: private-9\n  kind: private\n", encoding="utf-8"
    )
    assert load_settings(path).chats[0].kind == "private"
    (project / "chats.yaml").write_text(
        "chats:\n- id: 10\n  slug: private-10\n  kind: private\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError):
        load_settings(path)


def test_snapshot_includes_only_owner_approved_private_peers(settings):
    peers = [
        types.User(id=1, first_name="Synthetic person"),
        types.User(id=2, first_name="Synthetic bot", bot=True),
        types.User(id=3, first_name="Not approved"),
        types.User(id=4, deleted=True),
    ]
    rows, entities = asyncio.run(
        list_dialogs(Client(entities=peers), replace(settings, approved_private_ids=(1, 2, 4)))
    )
    assert set(entities) == {1, 2}
    assert {r["kind"] for r in rows} == {"private", "bot"}
    assert all(r["raw_channel_id"] is None for r in rows)
    assert all("phone" not in r and "access_hash" not in r for r in rows)


@pytest.mark.parametrize("bot", [False, True])
def test_approved_private_collection_and_wrong_type_guard(settings, bot):
    entity = types.User(id=9, first_name="Synthetic", bot=bot)
    kind = "bot" if bot else "private"
    spec = ChatSpec(9, "private-9", date(2025, 1, 1), kind=kind)
    approved = replace(settings, approved_private_ids=(9,), refresh_days=0)
    item = message()
    item.peer_id = types.PeerUser(9)
    item.from_id = types.PeerUser(9)
    stats = {"9": counts()["-7"]}
    store = MemoryStore()
    asyncio.run(collect_chat(HistoryClient([item]), store, entity, spec, approved, STAMP, stats))
    assert store.status == "ok" and len(store.messages) == 1
    for config, entry in (
        (settings, spec),
        (approved, replace(spec, kind=None)),
        (approved, replace(spec, kind="private" if bot else "bot")),
    ):
        store = MemoryStore()
        client = HistoryClient([item])
        asyncio.run(
            collect_chat(client, store, entity, entry, config, STAMP, {"9": counts()["-7"]})
        )
        assert store.status == "rejected" and not client.history_calls and not store.messages


def test_resolver_private_owner_scope_and_no_unknown_user_inference():
    rows = [
        {"chat_id": 9, "kind": "private", "title": "Synthetic", "username": "person"},
        {"chat_id": 10, "kind": "bot", "title": "Synthetic bot", "username": "test_bot"},
    ]
    assert resolve("9", rows)["status"] == "rejected"
    assert resolve("9", rows, (9,)) == {"id": 9, "kind": "private", "verified": True}
    assert resolve("@test_bot", rows, (10,))["kind"] == "bot"
    assert resolve("99", rows, (99,))["status"] == "rejected"


@pytest.mark.parametrize("kind", ["private", "bot"])
def test_private_links_never_fabricate_channel_urls(kind):
    assert link({"kind": kind, "chat_id": 9, "public_username": "person", "msg_id": 1}) is None
