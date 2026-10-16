import asyncio
import json
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from telethon import errors, types, utils

from tg_archive.config import ChatSpec, ConfigError
from tg_archive.discovery import list_dialogs, probe_file, scan_chat, select_peer, write_snapshot
from tg_archive.telegram import RequestDenied, RequestPolicy

STAMP = datetime(2026, 10, 6, tzinfo=UTC)


def group(protected=False):
    return types.Chat(
        id=7,
        title="SYNTHETIC_GROUP",
        photo=types.ChatPhotoEmpty(),
        participants_count=2,
        date=STAMP,
        version=1,
        noforwards=protected,
    )


def channel(forum=False):
    return types.Channel(
        id=8,
        title="SYNTHETIC_CHANNEL",
        photo=types.ChatPhotoEmpty(),
        date=STAMP,
        megagroup=True,
        forum=forum,
        access_hash=99,
    )


def message(number=1):
    item = types.Message(
        id=number, peer_id=types.PeerChat(7), date=STAMP, message="SYNTHETIC_PRIVATE_TEXT"
    )
    item._sender = types.User(id=11, first_name="SYNTHETIC_PROFILE", phone="SYNTHETIC_PHONE")
    return item


class Client:
    def __init__(self, items=(), entities=(), error=None):
        self.items, self.entities, self.error = items, entities, error
        self.calls = 0

    async def __call__(self, request):
        self.calls += 1
        return SimpleNamespace(count=len(self.items))

    async def get_messages(self, entity, **kwargs):
        self.calls += 1
        if "ids" in kwargs:
            return self.items[0] if self.items else None
        return list(self.items[:1])

    async def iter_messages(self, entity, **kwargs):
        for item in self.items[: kwargs["limit"]]:
            yield item
        if self.error:
            raise self.error

    async def iter_dialogs(self, **kwargs):
        for entity in self.entities:
            yield SimpleNamespace(entity=entity)


def test_snapshot_excludes_personal_and_bot_dialogs(settings):
    client = Client(entities=[types.User(1), types.User(2, bot=True), group(), channel(True)])
    rows, entities = asyncio.run(list_dialogs(client, settings))
    assert {r["kind"] for r in rows} == {"group", "forum"}
    assert len(entities) == 2
    contents = (settings.root / "state" / "dialogs.json").read_text(encoding="utf-8")
    assert "access_hash" not in contents
    assert "phone" not in contents
    assert "SYNTHETIC_PRIVATE_TEXT" not in contents


def test_raw_positive_id_only_resolves_known_channel():
    entities = {utils.get_peer_id(group()): group(), utils.get_peer_id(channel()): channel()}
    assert isinstance(select_peer(8, entities), types.Channel)
    with pytest.raises(ConfigError):
        select_peer(7, entities)
    with pytest.raises(ConfigError):
        select_peer(1234, entities)


def test_discovery_reports_only_counters_and_dates(settings):
    service = types.MessageService(
        id=2, peer_id=types.PeerChat(7), date=STAMP, action=types.MessageActionPinMessage()
    )
    result = asyncio.run(
        scan_chat(
            Client([message(), service]),
            group(),
            ChatSpec(-7, "candidate", date(2026, 1, 1)),
            settings,
            STAMP,
        )
    )
    assert result["complete"]
    assert result["messages"] == 1
    assert result["service_skipped"] == 1
    assert result["authors_seen"] == result["authors_with_phone"] == 1
    output = json.dumps(result)
    assert "SYNTHETIC_PRIVATE_TEXT" not in output
    assert "SYNTHETIC_PROFILE" not in output
    assert "SYNTHETIC_PHONE" not in output
    assert not result["reference_verified"]


def test_scan_limit_is_never_reported_complete(settings):
    result = asyncio.run(
        scan_chat(
            Client([message(n) for n in range(3)]),
            group(),
            ChatSpec(-7, "candidate", date(2026, 1, 1)),
            replace(settings, max_per_chat_run=2),
            STAMP,
        )
    )
    assert result["status"] == "backfilling"
    assert result["scanned"] == 2
    assert not result["complete"]


def test_protected_discovery_requires_owner_flag(settings):
    client = Client([message()])
    spec = ChatSpec(-7, "candidate", date(2026, 1, 1))
    result = asyncio.run(scan_chat(client, group(True), spec, settings, STAMP))
    assert result["status"] == "rejected"
    assert client.calls == 0
    result = asyncio.run(
        scan_chat(client, group(True), replace(spec, allow_protected=True), settings, STAMP)
    )
    assert result["status"] == "ok"


def test_long_flood_wait_preserves_partial_counters(settings):
    client = Client([message()], error=errors.FloodWaitError(request=None, capture=121))
    result = asyncio.run(
        scan_chat(client, group(), ChatSpec(-7, "candidate", date(2026, 1, 1)), settings, STAMP)
    )
    assert result["status"] == "backfilling"
    assert result["messages"] == 1
    assert result["flood_wait_seconds"] == 121
    assert not result["complete"]


def test_lost_access_is_not_a_complete_archive(settings):
    result = asyncio.run(
        scan_chat(
            Client(error=errors.ChannelPrivateError(request=None)),
            group(),
            ChatSpec(-7, "candidate", date(2026, 1, 1)),
            settings,
            STAMP,
        )
    )
    assert result["status"] == "lost_access"
    assert not result["complete"]


def test_discovery_does_not_suppress_denied_requests(settings):
    with pytest.raises(RequestDenied):
        asyncio.run(
            scan_chat(
                Client(error=RequestDenied("SYNTHETIC")),
                group(),
                ChatSpec(-7, "candidate", date(2026, 1, 1)),
                settings,
                STAMP,
            )
        )


def test_snapshot_replace_is_atomic(settings, monkeypatch):
    path = settings.root / "state" / "dialogs.json"
    write_snapshot(path, [{"kind": "group"}])

    def refuse(*args):
        raise OSError("SYNTHETIC_FAILURE")

    monkeypatch.setattr("tg_archive.discovery.os.replace", refuse)
    with pytest.raises(OSError):
        write_snapshot(path, [{"kind": "forum"}])
    assert json.loads(path.read_text(encoding="utf-8")) == [{"kind": "group"}]
    assert not path.with_suffix(".json.tmp").exists()


class Chunks:
    def __init__(self, chunks):
        self.chunks, self.closed = chunks, False

    async def __aiter__(self):
        # Verify that the full iteration takes place inside the restricted download scope.
        from telethon import functions

        for chunk in self.chunks:
            RequestPolicy().check(functions.auth.ExportAuthorizationRequest(dc_id=2))
            yield chunk

    async def close(self):
        self.closed = True


def probe_client(size, chunks):
    attachment = SimpleNamespace(
        media=object(), document=SimpleNamespace(dc_id=2), file=SimpleNamespace(size=size)
    )
    iterator = Chunks(chunks)
    client = Client([attachment])
    client.iter_download = lambda *args, **kwargs: iterator
    return client, iterator


def test_probe_streams_in_memory_and_closes_iterator(settings):
    client, iterator = probe_client(6, [b"abc", b"def"])
    result = asyncio.run(probe_file(client, group(), 1, settings))
    assert result["bytes"] == 6
    assert not result["written_to_disk"]
    assert iterator.closed
    assert not list(settings.root.rglob("*.part"))


def test_protected_probe_never_downloads_even_if_text_allowed(settings):
    client, iterator = probe_client(3, [b"abc"])
    with pytest.raises(ConfigError):
        asyncio.run(probe_file(client, group(True), 1, settings))
    assert client.calls == 0
    assert not iterator.closed


@pytest.mark.parametrize("size,chunks", [(7, [b"abc"]), (2, [b"abc"])])
def test_probe_detects_truncated_or_oversize_transfer_and_closes(settings, size, chunks):
    client, iterator = probe_client(size, chunks)
    with pytest.raises(ConfigError):
        asyncio.run(probe_file(client, group(), 1, settings))
    assert iterator.closed


def test_large_probe_is_rejected_before_download(settings):
    client, iterator = probe_client(2 * 1024**3, [])
    with pytest.raises(ConfigError):
        asyncio.run(probe_file(client, group(), 1, settings))
    assert not iterator.closed


def test_media_sizes_are_available_by_kind_without_filenames(settings):
    document = SimpleNamespace(
        date=STAMP,
        sender=None,
        media=object(),
        document=object(),
        file=SimpleNamespace(size=2048, name="SYNTHETIC_FILENAME"),
    )
    photo = SimpleNamespace(date=STAMP, sender=None, media=object(), photo=object(), file=None)
    result = asyncio.run(
        scan_chat(
            Client([document, photo]),
            group(),
            ChatSpec(-7, "candidate", date(2026, 1, 1)),
            settings,
            STAMP,
        )
    )
    assert result["media"] == {"document": 1, "photo": 1}
    assert result["media_bytes_by_kind"] == {"document": 2048}
    assert result["media_unknown_sizes_by_kind"] == {"photo": 1}
    assert "SYNTHETIC_FILENAME" not in json.dumps(result)
