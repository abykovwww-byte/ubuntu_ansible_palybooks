"""Sequential metadata discovery. Outputs contain no message or profile text."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from telethon import errors, functions, types, utils

from .config import ChatSpec, ConfigError, Settings
from .telegram import RequestDenied, download_scope


def chat_kind(entity) -> str | None:
    if isinstance(entity, types.User) and not entity.deleted:
        return "bot" if entity.bot else "private"
    if isinstance(entity, types.Chat):
        return "group"
    if isinstance(entity, types.Channel):
        if entity.forum:
            return "forum"
        return "supergroup" if entity.megagroup else "channel"
    # Deleted/empty/forbidden entities and unknown types are not candidates.
    return None


def classify_media(message) -> tuple[str | None, int | None]:
    if not getattr(message, "media", None):
        return None, None
    for attr, kind in (
        ("sticker", "sticker"),
        ("gif", "gif"),
        ("voice", "voice"),
        ("video_note", "video_note"),
        ("video", "video"),
        ("audio", "audio"),
        ("photo", "photo"),
        ("document", "document"),
    ):
        if getattr(message, attr, None):
            file = getattr(message, "file", None)
            return kind, getattr(file, "size", None)
    return "other", None


def write_snapshot(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(rows, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def list_dialogs(client, settings: Settings):
    snapshot_at = datetime.now(UTC).isoformat()
    selected = {chat.chat_id for chat in settings.chats}
    rows, entities = [], {}
    async for dialog in client.iter_dialogs(ignore_migrated=True):
        entity = dialog.entity
        kind = chat_kind(entity)
        if kind is None or (
            kind in {"private", "bot"} and entity.id not in settings.approved_private_ids
        ):
            continue
        peer_id = utils.get_peer_id(entity)
        rows.append(
            {
                "chat_id": peer_id,
                "raw_channel_id": entity.id if isinstance(entity, types.Channel) else None,
                "kind": kind,
                "title": dialog_title(entity),
                "username": getattr(entity, "username", None),
                "participants_count": getattr(entity, "participants_count", None),
                "protected": bool(getattr(entity, "noforwards", False)),
                "in_sync": peer_id in selected,
                "snapshot_at": snapshot_at,
            }
        )
        entities[peer_id] = entity
    write_snapshot(settings.root / "state" / "dialogs.json", rows)
    return rows, entities


def dialog_title(entity):
    if isinstance(entity, types.User):
        return " ".join(p for p in (entity.first_name, entity.last_name) if p) or (
            entity.username or str(entity.id)
        )
    return entity.title


def select_peer(value: int, entities: dict):
    if value in entities:
        return entities[value]
    if value > 0:
        matches = [e for e in entities.values() if isinstance(e, types.Channel) and e.id == value]
        if len(matches) == 1:
            return matches[0]
    raise ConfigError("Chat is not an accessible group/channel in the current dialogs")


async def scan_chat(client, entity, spec: ChatSpec, settings: Settings, until: datetime):
    result = {
        "slug": spec.slug,
        "kind": chat_kind(entity),
        "protected": bool(getattr(entity, "noforwards", False)),
        "since": spec.since.isoformat(),
        "until": until.isoformat(),
        "status": "ok",
        "complete": False,
        "scanned": 0,
        "messages": 0,
        "service_skipped": 0,
        "media": {},
        "media_bytes_by_kind": {},
        "media_unknown_sizes_by_kind": {},
        "media_bytes_known": 0,
        "media_unknown_sizes": 0,
        "files_over_1gb": 0,
        "authors_seen": 0,
        "authors_with_phone": 0,
        "first_available_at": None,
        "reference_count_candidate": None,
        "reference_verified": False,
    }
    if (
        result["kind"] is None
        or (
            result["kind"] in {"private", "bot"}
            and (spec.chat_id not in settings.approved_private_ids or spec.kind != result["kind"])
        )
        or (result["protected"] and not spec.allow_protected)
    ):
        result["status"] = "rejected"
        return result
    authors, phones, media = set(), set(), Counter()
    media_bytes, unknown_sizes = Counter(), Counter()
    start = datetime.combine(spec.since, datetime.min.time(), tzinfo=UTC)
    try:
        first = await client.get_messages(entity, limit=1, reverse=True)
        if first:
            result["first_available_at"] = first[0].date.isoformat()
        reference = await client(
            functions.messages.SearchRequest(
                peer=entity,
                q="",
                filter=types.InputMessagesFilterEmpty(),
                min_date=start,
                max_date=until,
                offset_id=0,
                add_offset=0,
                limit=1,
                max_id=0,
                min_id=0,
                hash=0,
            )
        )
        # Candidate only: Search's treatment of service messages must be verified live.
        result["reference_count_candidate"] = getattr(reference, "count", None)
        async for message in client.iter_messages(
            entity,
            reverse=True,
            offset_date=start - timedelta(seconds=1),
            limit=settings.max_per_chat_run + 1,
        ):
            if message.date < start:
                continue
            if message.date > until:
                break
            if result["scanned"] >= settings.max_per_chat_run:
                result["status"] = "backfilling"
                break
            result["scanned"] += 1
            if isinstance(message, types.MessageService):
                result["service_skipped"] += 1
                continue
            result["messages"] += 1
            sender = message.sender  # Cached object only, no phone/profile lookups.
            if isinstance(sender, types.User):
                authors.add(sender.id)
                if sender.phone:
                    phones.add(sender.id)
            kind, size = classify_media(message)
            if kind:
                media[kind] += 1
                if size is None:
                    result["media_unknown_sizes"] += 1
                    unknown_sizes[kind] += 1
                else:
                    result["media_bytes_known"] += size
                    media_bytes[kind] += size
                    result["files_over_1gb"] += int(size > 1024**3)
        else:
            result["complete"] = True
        if result["status"] == "ok":
            result["complete"] = True
    except RequestDenied:
        raise
    except errors.FloodWaitError as error:
        result["status"] = "backfilling"
        result["flood_wait_seconds"] = error.seconds
    except (errors.ChannelPrivateError, errors.ChatAdminRequiredError):
        result["status"] = "lost_access"
    except (OSError, errors.RPCError):
        result["status"] = "failed"
    result["media"] = dict(sorted(media.items()))
    result["media_bytes_by_kind"] = dict(sorted(media_bytes.items()))
    result["media_unknown_sizes_by_kind"] = dict(sorted(unknown_sizes.items()))
    result["authors_seen"], result["authors_with_phone"] = len(authors), len(phones)
    return result


async def probe_file(client, entity, msg_id: int, settings: Settings) -> dict:
    if bool(getattr(entity, "noforwards", False)):
        raise ConfigError("File probe rejected: protected chat")
    message = await client.get_messages(entity, ids=msg_id)
    if not message:
        raise ConfigError("File probe rejected: message unavailable")
    kind, size = classify_media(message)
    if kind not in {"document", "photo", "video", "audio", "voice", "video_note"}:
        raise ConfigError("File probe rejected: unsupported media kind")
    cap = settings.max_file_mb * 1024**2
    if size is None or size > cap:
        raise ConfigError("File probe rejected: unknown size or file exceeds max_file_mb")
    digest, received = hashlib.sha256(), 0
    with download_scope():
        iterator = client.iter_download(message.media, request_size=512 * 1024)
        try:
            async for chunk in iterator:
                received += len(chunk)
                if received > size or received > cap:
                    raise ConfigError("File probe rejected: size mismatch")
                digest.update(chunk)
        finally:
            await iterator.close()
    if received != size:
        raise ConfigError("File probe rejected: size mismatch")
    # SHA-256 is computed but not emitted: discovery publishes only counters/types.
    digest.digest()
    return {
        "status": "ok",
        "kind": kind,
        "bytes": received,
        "source_dc_id": getattr(
            getattr(message, "document", None) or getattr(message, "photo", None), "dc_id", None
        ),
        "sha256_computed": True,
        "written_to_disk": False,
    }
