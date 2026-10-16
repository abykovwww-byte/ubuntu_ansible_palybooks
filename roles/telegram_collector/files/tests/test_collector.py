"""Sequential collector fixtures contain synthetic messages only."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime

import pytest
from telethon import errors, types
from test_content import document, message

from tg_archive.collector import collect_chat, read_pass, refresh_window, run
from tg_archive.config import ChatSpec

STAMP = datetime(2026, 10, 12, tzinfo=UTC)
SPEC = ChatSpec(-7, "synthetic", date(2025, 1, 1))


def counts():
    return {
        "-7": dict(
            fetched=0,
            upserted=0,
            service_skipped=0,
            deleted=0,
            redacted=0,
            forward_fetched=0,
            refresh_fetched=0,
        )
    }


def group(protected=False):
    return types.Chat(
        id=7,
        title="Synthetic",
        photo=types.ChatPhotoEmpty(),
        participants_count=1,
        date=STAMP,
        version=1,
        noforwards=protected,
    )


class MemoryStore:
    def __init__(self):
        self.messages = {}
        self.cursor = 0
        self.status = None
        self.fail_on_batch = None
        self.batch_count = 0

    async def batch(self, chat_id, records, users, cursor, stats):
        self.batch_count += 1
        if self.batch_count == self.fail_on_batch:
            raise OSError("Synthetic batch failure")
        self.messages.update({row["msg_id"]: deepcopy(row) for row in records})
        if cursor is not None:
            self.cursor = cursor

    async def prepare_chat(self, *args):
        return {"cursor_msg_id": self.cursor}

    async def chat_status(self, chat_id, status, *args, **kwargs):
        self.status = status

    async def window_ids(self, chat_id, since, until):
        return {i for i, row in self.messages.items() if since <= row["sent_at"] <= until}

    async def delete_confirmed(self, chat_id, ids, stats):
        for i in ids:
            self.messages.pop(i)
        stats[str(chat_id)]["deleted"] += len(ids)


class Client:
    def __init__(self, messages, *, fail_after=None, point_fail_at=None, points=None):
        self.messages = messages
        self.fail_after = fail_after
        self.point_fail_at = point_fail_at
        self.points = points or {}
        self.history_calls = []
        self.point_calls = []

    async def iter_messages(self, entity, **options):
        self.history_calls.append(options)
        rows = [
            m
            for m in self.messages
            if m.id > options.get("min_id", 0)
            and m.date > options.get("offset_date", datetime.min.replace(tzinfo=UTC))
        ]
        for i, msg in enumerate(rows[: options["limit"]]):
            if i == self.fail_after:
                raise errors.FloodWaitError(request=None, capture=121)
            yield msg
        if self.fail_after is not None and self.fail_after >= len(rows):
            raise OSError("Synthetic incomplete read")

    async def get_messages(self, entity, *, ids):
        self.point_calls.append(ids)
        if len(self.point_calls) == self.point_fail_at:
            raise OSError("Synthetic point failure")
        return [self.points.get(i) for i in ids]


def test_interruption_after_committed_batch_resumes_without_holes(settings):
    store = MemoryStore()
    source = [message(i) for i in range(1, 1002)]
    first = Client(source, fail_after=750)
    stats = counts()
    asyncio.run(
        collect_chat(first, store, group(), SPEC, replace(settings, refresh_days=0), STAMP, stats)
    )
    assert store.cursor == 500 and len(store.messages) == 500
    assert store.status == "backfilling" and stats["-7"]["flood_wait_seconds"] == 121
    second = Client(source)
    asyncio.run(
        collect_chat(
            second, store, group(), SPEC, replace(settings, refresh_days=0), STAMP, counts()
        )
    )
    assert store.cursor == 1001 and set(store.messages) == set(range(1, 1002))
    assert second.history_calls[0]["min_id"] == 500 and store.status == "ok"
    third = counts()
    asyncio.run(
        collect_chat(
            Client(source), store, group(), SPEC, replace(settings, refresh_days=0), STAMP, third
        )
    )
    assert third["-7"]["fetched"] == 0


def test_atomic_failure_keeps_cursor_and_counts_at_previous_batch(settings):
    store = MemoryStore()
    store.fail_on_batch = 2
    stats = counts()
    with pytest.raises(OSError):
        asyncio.run(
            read_pass(
                Client([message(i) for i in range(1, 1001)]),
                store,
                group(),
                SPEC,
                settings,
                datetime(2025, 1, 1, tzinfo=UTC),
                STAMP,
                0,
                stats,
            )
        )
    assert store.cursor == 500 and stats["-7"]["upserted"] == 500


@pytest.mark.parametrize("remaining", [[2], [1, 3], [1, 2], [2, 3], []])
def test_deletion_middle_edges_and_empty_window(settings, remaining):
    store = MemoryStore()
    asyncio.run(
        collect_chat(
            Client([message(i) for i in (1, 2, 3)]),
            store,
            group(),
            SPEC,
            replace(settings, refresh_days=0),
            STAMP,
            counts(),
        )
    )
    stats = counts()
    source = [message(i, text="Synthetic edited") for i in remaining]
    asyncio.run(refresh_window(Client(source), store, group(), SPEC, settings, STAMP, stats))
    assert set(store.messages) == set(remaining)
    assert stats["-7"]["deleted"] == 3 - len(remaining)
    for row in store.messages.values():
        assert row["text"] == "Synthetic edited"


def test_incomplete_window_never_verifies_or_deletes(settings):
    store = MemoryStore()
    source = [message(i) for i in (1, 2, 3)]
    asyncio.run(
        collect_chat(
            Client(source), store, group(), SPEC, replace(settings, refresh_days=0), STAMP, counts()
        )
    )
    client = Client([message(2)], fail_after=1)
    with pytest.raises(OSError):
        asyncio.run(refresh_window(client, store, group(), SPEC, settings, STAMP, counts()))
    assert set(store.messages) == {1, 2, 3} and not client.point_calls


def test_last_point_query_failure_prevents_all_deletions(settings):
    store = MemoryStore()
    source = [message(i) for i in range(1, 102)]
    asyncio.run(
        collect_chat(
            Client(source), store, group(), SPEC, replace(settings, refresh_days=0), STAMP, counts()
        )
    )
    client = Client([], point_fail_at=2)
    with pytest.raises(OSError):
        asyncio.run(refresh_window(client, store, group(), SPEC, settings, STAMP, counts()))
    assert len(store.messages) == 101 and len(client.point_calls) == 2


def test_missing_history_message_still_present_by_id_is_retained(settings):
    store = MemoryStore()
    asyncio.run(
        collect_chat(
            Client([message(1)]),
            store,
            group(),
            SPEC,
            replace(settings, refresh_days=0),
            STAMP,
            counts(),
        )
    )
    asyncio.run(
        refresh_window(
            Client([], points={1: message(1)}), store, group(), SPEC, settings, STAMP, counts()
        )
    )
    assert set(store.messages) == {1}


def test_limit_marks_backfilling_and_service_advances_cursor(settings):
    service = types.MessageService(
        id=2,
        peer_id=types.PeerChat(7),
        date=STAMP,
        action=types.MessageActionChatAddUser(users=[9]),
    )
    store = MemoryStore()
    stats = counts()
    asyncio.run(
        collect_chat(
            Client([message(1), service, message(3)]),
            store,
            group(),
            SPEC,
            replace(settings, max_per_chat_run=2),
            STAMP,
            stats,
        )
    )
    assert store.cursor == 2 and set(store.messages) == {1}
    assert store.status == "backfilling" and stats["-7"]["service_skipped"] == 1


@pytest.mark.parametrize("entity", [None, types.User(id=9, first_name="Synthetic")])
def test_lost_access_or_private_peer_preserves_existing(settings, entity):
    store = MemoryStore()
    store.messages[1] = {"synthetic": True}
    client = Client([message(2)])
    asyncio.run(collect_chat(client, store, entity, SPEC, settings, STAMP, counts()))
    assert not client.history_calls and set(store.messages) == {1}
    assert store.status in {"lost_access", "rejected"}


def test_protected_requires_owner_text_opt_in_and_always_skips_files(settings):
    source = [message(media=document())]
    store = MemoryStore()
    asyncio.run(collect_chat(Client(source), store, group(True), SPEC, settings, STAMP, counts()))
    assert store.status == "rejected" and not store.messages
    asyncio.run(
        collect_chat(
            Client(source),
            store,
            group(True),
            replace(SPEC, allow_protected=True),
            settings,
            STAMP,
            counts(),
        )
    )
    assert store.status == "ok" and store.messages[1]["media_status"] == "skipped_protected"


def test_unaligned_point_response_keeps_all_candidates(settings):
    from tg_archive.config import ConfigError

    store = MemoryStore()
    asyncio.run(
        collect_chat(
            Client([message(1)]),
            store,
            group(),
            SPEC,
            replace(settings, refresh_days=0),
            STAMP,
            counts(),
        )
    )
    with pytest.raises(ConfigError):
        asyncio.run(
            refresh_window(
                Client([], points={1: message(2)}), store, group(), SPEC, settings, STAMP, counts()
            )
        )
    assert set(store.messages) == {1}


def test_positive_user_id_is_rejected_at_runtime(settings):
    store = MemoryStore()
    stats = {"9": counts()["-7"]}
    asyncio.run(
        collect_chat(Client([]), store, None, replace(SPEC, chat_id=9), settings, STAMP, stats)
    )
    assert stats["9"]["status"] == "rejected" and store.status == "rejected"


def test_dry_run_never_opens_database_or_downloads(settings, monkeypatch):
    from types import SimpleNamespace

    import tg_archive.collector as collector

    class DryClient:
        async def is_user_authorized(self):
            return True

        async def disconnect(self):
            pass

    async def connected(*args):
        pass

    async def dialogs(*args):
        return [], {-7: group()}

    async def scan(*args):
        return {"status": "ok", "complete": True}

    def forbidden(*args, **kwargs):
        raise AssertionError("Dry run must not use database")

    monkeypatch.setattr(collector, "make_client", lambda *args: DryClient())
    monkeypatch.setattr(collector, "load_session", lambda: "")
    monkeypatch.setattr(collector, "connect_client", connected)
    monkeypatch.setattr(collector, "list_dialogs", dialogs)
    monkeypatch.setattr(collector, "scan_chat", scan)
    monkeypatch.setattr(collector, "connect_database", forbidden)
    opts = SimpleNamespace(chat=None, no_media=False, dry_run=True)
    result, code = asyncio.run(
        run(opts, replace(settings, chats=(SPEC,), api_id=123, api_hash="a" * 32))
    )
    assert code == 0 and result["server_writes"] is False
