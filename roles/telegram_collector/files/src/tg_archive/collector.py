"""Sequential text collection followed by server-only attachment streaming."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from telethon import errors, types, utils

from .config import ConfigError
from .content import project_message
from .credentials import load_session
from .database import Store, connect_database
from .discovery import chat_kind, list_dialogs, scan_chat
from .media import media_stage
from .network import connect_client
from .telegram import RequestDenied, make_client


def error_kind(error):
    if isinstance(error, errors.FloodWaitError):
        return "flood_wait"
    if isinstance(error, (errors.ChannelPrivateError, errors.ChatAdminRequiredError)):
        return "lost_access"
    if isinstance(error, RequestDenied):
        return "request_denied"
    if isinstance(error, (TimeoutError, OSError)):
        return "network_error"
    return "operation_failed"


async def read_pass(
    client, store, entity, spec, settings, since, until, cursor, counts, *, refresh=False
):
    stats = counts[str(spec.chat_id)]
    buffer, users, seen = [], {}, set()
    last_id, pending_fetched, skipped, masked = cursor or 0, 0, 0, 0

    async def commit():
        nonlocal buffer, users, pending_fetched, skipped, masked
        # Counters are committed together with their batch and cursor.
        before = stats.copy()
        stats["fetched"] += pending_fetched
        stats["upserted"] += len(buffer)
        stats["service_skipped"] += skipped
        stats["redacted"] += masked
        stats["refresh_fetched" if refresh else "forward_fetched"] += pending_fetched
        try:
            await store.batch(
                spec.chat_id, buffer, list(users.values()), None if refresh else last_id, counts
            )
        except BaseException:
            stats.clear()
            stats.update(before)
            raise
        buffer, users, pending_fetched, skipped, masked = [], {}, 0, 0, 0

    available = max(0, settings.max_per_chat_run - stats["fetched"])
    options = {"reverse": True, "limit": available + 1}
    if cursor and not refresh:
        options["min_id"] = cursor
    else:
        options["offset_date"] = since - timedelta(seconds=1)
    total = 0
    async for message in client.iter_messages(entity, **options):
        if message.date < since or (cursor and not refresh and message.id <= cursor):
            continue
        if message.date > until:
            break
        if total == available:
            await commit()
            return False, seen
        total += 1
        pending_fetched += 1
        last_id = max(last_id, message.id)
        seen.add(message.id)
        if isinstance(message, types.MessageService):
            skipped += 1
        else:
            row, user = project_message(
                message, spec, settings, bool(getattr(entity, "noforwards", False)), until
            )
            buffer.append(row)
            masked += row["redactions"]
            if user:
                users[user["user_id"]] = user
        if pending_fetched == 500:
            await commit()
    await commit()
    return True, seen


async def refresh_window(client, store, entity, spec, settings, until, counts):
    since = max(
        datetime.combine(spec.since, datetime.min.time(), UTC),
        until - timedelta(days=settings.refresh_days),
    )
    complete, seen = await read_pass(
        client, store, entity, spec, settings, since, until, None, counts, refresh=True
    )
    if not complete:
        return False
    candidates = sorted(await store.window_ids(spec.chat_id, since, until) - seen)
    confirmed = []
    recovered, users = [], {}
    # Finish ALL point checks before deleting ANY row; a late failure preserves every candidate.
    for offset in range(0, len(candidates), 100):
        ids = candidates[offset : offset + 100]
        messages = await client.get_messages(entity, ids=ids)
        if len(messages) != len(ids):
            raise ConfigError("Incomplete point verification; deletions skipped")
        for msg_id, message in zip(ids, messages, strict=True):
            if message is None:
                confirmed.append(msg_id)
            elif message.id != msg_id:
                raise ConfigError("Unaligned point verification; deletions skipped")
            elif not isinstance(message, types.MessageService):
                row, user = project_message(
                    message, spec, settings, bool(getattr(entity, "noforwards", False)), until
                )
                recovered.append(row)
                if user:
                    users[user["user_id"]] = user
    if recovered:
        await store.batch(spec.chat_id, recovered, list(users.values()), None, counts)
    await store.delete_confirmed(spec.chat_id, confirmed, counts)
    return True


async def collect_chat(client, store, entity, spec, settings, until, counts):
    stats = counts[str(spec.chat_id)]
    if entity is None:
        if spec.chat_id > 0:
            stats["status"] = "rejected"
        else:
            stats["status"] = "lost_access"
        await store.chat_status(spec.chat_id, stats["status"], stats["status"])
        return
    kind = chat_kind(entity)
    if (
        kind is None
        or utils.get_peer_id(entity) != spec.chat_id
        or (
            kind in {"private", "bot"}
            and (spec.chat_id not in settings.approved_private_ids or spec.kind != kind)
        )
    ):
        stats["status"] = "rejected"
        await store.chat_status(spec.chat_id, "rejected", "invalid_peer")
        return
    chat = await store.prepare_chat(spec, entity, kind, until)
    if getattr(entity, "noforwards", False) and not spec.allow_protected:
        stats["status"] = "rejected"
        await store.chat_status(spec.chat_id, "rejected", "protected")
        return
    try:
        if kind == "forum":
            # Telethon 1.45 uses messages.GetForumTopics, outside the owner's candidate allowlist.
            # Until the owner approves that method, do not label topic-less collection complete.
            raise RequestDenied("messages.GetForumTopicsRequest")
        await store.chat_status(spec.chat_id, "backfilling")
        start = datetime.combine(spec.since, datetime.min.time(), UTC)
        complete, _ = await read_pass(
            client, store, entity, spec, settings, start, until, chat["cursor_msg_id"], counts
        )
        if not complete:
            stats["status"] = "backfilling"
            await store.chat_status(spec.chat_id, "backfilling", backfill_done=False)
            return
        await store.chat_status(spec.chat_id, "backfilling", backfill_done=True)
        if settings.refresh_days and not await refresh_window(
            client, store, entity, spec, settings, until, counts
        ):
            stats["status"] = "backfilling"
            await store.chat_status(spec.chat_id, "backfilling", "refresh_limit")
            return
        stats["status"] = "ok"
        await store.chat_status(spec.chat_id, "ok", backfill_done=True)
    except Exception as error:
        failure = error_kind(error)
        stats["status"] = (
            "backfilling"
            if failure == "flood_wait"
            else "lost_access"
            if failure == "lost_access"
            else "failed"
        )
        stats["error"] = failure
        if isinstance(error, errors.FloodWaitError):
            stats["flood_wait_seconds"] = error.seconds
        await store.chat_status(spec.chat_id, stats["status"], failure)
        if isinstance(error, RequestDenied):
            raise


async def run(args, settings):
    # A daily collector run is also a TTL cleanup trigger for MCP file copies.
    from .fetch_cache import cleanup, load_fetch

    cleanup(load_fetch(getattr(args, "config", settings.root / "tg-archive.toml")))
    selected = [c for c in settings.chats if not args.chat or c.slug == args.chat]
    if not selected:
        raise ConfigError("No selected chat in chats.yaml")
    settings.require_credentials()
    started = datetime.now(UTC)
    counts = {
        str(c.chat_id): dict(
            fetched=0,
            upserted=0,
            deleted=0,
            service_skipped=0,
            redacted=0,
            media_uploaded=0,
            media_bytes=0,
            media_failed=0,
            forward_fetched=0,
            refresh_fetched=0,
        )
        for c in selected
    }
    journal = getattr(args, "journal", None)
    client = make_client(settings, load_session(), journal)
    connection, store = None, None
    try:
        if not args.dry_run:
            connection = await connect_database(settings.root)
            store = Store(connection)
            await store.begin(started)
        await connect_client(client)
        if not await client.is_user_authorized():
            raise ConfigError("Telegram session expired: owner login required")
        _, entities = await list_dialogs(client, settings)
        if args.dry_run:
            reports = []
            for spec in selected:
                entity = entities.get(spec.chat_id)
                reports.append(
                    await scan_chat(client, entity, spec, settings, started)
                    if entity
                    else {"status": "lost_access"}
                )
            code = 0 if all(r["status"] == "ok" for r in reports) else 2
            return {"dry_run": True, "server_writes": False, "chats": reports}, code
        for spec in selected:
            await collect_chat(
                client, store, entities.get(spec.chat_id), spec, settings, started, counts
            )
        try:
            media = await media_stage(
                client, store, settings, selected, entities, counts, no_media=args.no_media
            )
        except RequestDenied:
            raise
        except Exception as error:
            media = {"fsck_status": "aborted_listing", "media_stage_error": type(error).__name__}
        warnings = await store.unconfigured([c.chat_id for c in settings.chats])
        chat_partial = any(s.get("status") != "ok" for s in counts.values())
        partial = (
            chat_partial
            or media["fsck_status"] != "ok"
            or media.get("media_new_failed", 0) > 0
            or media.get("media_quota", 0) > 0
            or media.get("media_cleanup_failed", 0) > 0
            or bool(media.get("media_stage_error"))
        )
        counts["media"] = media
        counts["unconfigured_chats"] = warnings
        status = "partial" if partial else "ok"
        await store.finish(status, counts)
        return {
            "run_id": store.run_id,
            "status": status,
            "counts": counts,
        }, 2 if partial else 0
    except BaseException as error:
        if store and store.run_id is not None:
            failure = (
                "interrupted"
                if isinstance(error, (KeyboardInterrupt, SystemExit))
                else error_kind(error)
            )
            try:
                await store.finish("failed", counts, failure)
            except Exception:
                # The next locked run marks the unfinished row interrupted.
                pass
        raise
    finally:
        await client.disconnect()
        if connection:
            await connection.close()
