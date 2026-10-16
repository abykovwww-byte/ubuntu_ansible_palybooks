"""Telegram -> SFTP streaming with 64 MiB route checkpoints, never a local file."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime

import asyncssh
from telethon import errors, types, utils

from .content import eligibility, project_message
from .fsck import counters, reconcile
from .paths import media_path, within_root
from .route import RouteGuard
from .sftp import writer_filesystem
from .telegram import RequestDenied, download_scope

CHECKPOINT_BYTES = 64 * 1024**2


class PathDeferred(Exception):
    pass


class SizeMismatch(Exception):
    pass


@dataclass
class ByteBudget:
    used: int = 0


def media_error(error):
    if isinstance(error, errors.FloodWaitError):
        return "flood_wait"
    if isinstance(error, errors.FileReferenceExpiredError):
        return "file_reference_expired"
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, (asyncssh.Error, OSError)):
        return "sftp_error"
    if isinstance(error, SizeMismatch):
        return "size_mismatch"
    return "other"


def download_source(message):
    """Match Telethon File.size's heaviest photo variant, not utils' last variant."""
    if message.photo:
        photo = message.photo
        variant = max(photo.sizes, key=lambda s: utils._photo_size_byte_count(s) or 0)
        return types.InputPhotoFileLocation(
            id=photo.id,
            access_hash=photo.access_hash,
            file_reference=photo.file_reference,
            thumb_size=variant.type,
        ), photo.dc_id
    return message.media, None


async def transmit(client, message, row, spec, settings, fs, route, budget=None):
    root = settings.media_settings.remote_root
    relative = media_path(spec, row)
    within_root(root, relative)
    part = relative + ".part"
    expected = row["media_size"]
    if expected is None or expected > settings.max_file_mb * 1024**2:
        raise SizeMismatch
    if not await route.allowed():
        raise PathDeferred
    digest, received, since_check = hashlib.sha256(), 0, 0
    await fs.mkdir_parents(root, relative)
    iterator = None
    completed = False
    caught = None
    try:
        with download_scope():
            source, dc_id = download_source(message)
            iterator = client.iter_download(
                source, request_size=512 * 1024, file_size=expected, dc_id=dc_id
            )
            async with fs.open_part(root, part) as target:
                async for chunk in iterator:
                    # Split even an irregular chunk at the exact checkpoint boundary.
                    position = 0
                    while position < len(chunk):
                        count = min(len(chunk) - position, CHECKPOINT_BYTES - since_check)
                        piece = chunk[position : position + count]
                        if received + count > expected:
                            raise SizeMismatch
                        if budget is not None:
                            # Charge scheduled bytes too if a write partially succeeds and raises.
                            budget.used += count
                        await target.write(piece)
                        digest.update(piece)
                        received += count
                        since_check += count
                        position += count
                        if since_check == CHECKPOINT_BYTES:
                            since_check = 0
                            if not await route.allowed():
                                raise PathDeferred
        if received != expected or await fs.stat(root, part) != expected:
            raise SizeMismatch
        await fs.rename(root, part, relative)
        completed = True
        return relative, digest.hexdigest(), received
    except BaseException as error:
        caught = error
        raise
    finally:
        try:
            if iterator:
                await iterator.close()
        finally:
            if not completed:
                try:
                    await fs.remove(root, part)
                except asyncssh.SFTPNoSuchFile:
                    pass
                except Exception:
                    if caught is None:
                        raise
                    caught.cleanup_failed = True


async def recalculate(store, settings, approved, entities):
    for row in await store.unfinished(list(approved)):
        spec = approved[row["chat_id"]]
        status = eligibility(
            row["media_kind"],
            row["media_size"],
            spec,
            settings,
            bool(getattr(entities[row["chat_id"]], "noforwards", False)),
        )
        await store.set_eligibility(row, status)


async def transfer_queue(client, store, settings, approved, entities, counts, fs, route):
    report = dict(
        media_deferred_path=0,
        media_quota=0,
        media_budget_deferred=0,
        media_new_failed=0,
        media_cleanup_failed=0,
    )
    queue = await store.unfinished(list(approved), pending_only=True)
    if not queue:
        return report
    if not await route.allowed():
        report["media_deferred_path"] = len(queue)
        return report
    budget = ByteBudget()
    options = settings.media_settings
    for offset, row in enumerate(queue):
        spec = approved[row["chat_id"]]
        protected = bool(getattr(entities[row["chat_id"]], "noforwards", False))
        status = eligibility(row["media_kind"], row["media_size"], spec, settings, protected)
        if status != "pending":
            await store.set_eligibility(row, status)
            continue
        if not await route.allowed():
            report["media_deferred_path"] = len(queue) - offset
            break
        # Resolve fresh file_reference, text and identity before opening an SFTP target.
        try:
            message = await client.get_messages(entities[row["chat_id"]], ids=row["msg_id"])
            if message is None:
                raise ValueError("Media message unavailable")
            current, user = project_message(message, spec, settings, protected, datetime.now(UTC))
            await store.batch(spec.chat_id, [current], [user] if user else [], None, counts)
            row = current
            if row["media_status"] != "pending":
                continue
            size = row["media_size"]
            if size is None:
                raise SizeMismatch
            if budget.used + size > options.max_per_run_gb * 1024**3:
                report["media_budget_deferred"] = len(queue) - offset
                break
            if (
                await store.done_bytes() + size > options.max_total_gb * 1024**3
                or await fs.free_bytes(options.remote_root) - size < options.min_free_gb * 1024**3
            ):
                report["media_quota"] = await store.quota_remaining(list(approved))
                break
            path, sha256, received = await transmit(
                client, message, row, spec, settings, fs, route, budget
            )
            # If interrupted here, fsck sees an orphan; no premature done state.
            await store.media_done(row, path, sha256)
            counts[str(spec.chat_id)]["media_uploaded"] += 1
            counts[str(spec.chat_id)]["media_bytes"] += received
        except PathDeferred as error:
            report["media_cleanup_failed"] += int(getattr(error, "cleanup_failed", False))
            report["media_deferred_path"] = len(queue) - offset
            break
        except RequestDenied:
            raise
        except Exception as error:
            report["media_cleanup_failed"] += int(getattr(error, "cleanup_failed", False))
            kind = media_error(error)
            became_failed = await store.media_failure(row, kind)
            if became_failed:
                report["media_new_failed"] += 1
                counts[str(spec.chat_id)]["media_failed"] += 1
            if kind == "flood_wait":
                break
    return report


async def media_stage(client, store, settings, specs, entities, counts, *, no_media=False):
    approved = {
        spec.chat_id: spec
        for spec in specs
        if spec.chat_id in entities
        and counts[str(spec.chat_id)].get("status") not in {"rejected", "lost_access"}
    }
    async with writer_filesystem(settings) as fs:
        result = await reconcile(store, fs, settings.media_settings.remote_root)
        try:
            await recalculate(store, settings, approved, entities)
            if not no_media and result["fsck_status"] == "ok":
                result.update(
                    await transfer_queue(
                        client,
                        store,
                        settings,
                        approved,
                        entities,
                        counts,
                        fs,
                        RouteGuard(settings.media_settings),
                    )
                )
        except RequestDenied:
            raise
        except Exception as error:
            result["media_stage_error"] = type(error).__name__
        result["no_media_requested"] = no_media
        return result


async def standalone_fsck(store, settings, *, dry_run=False, confirm_mass=False):
    try:
        async with writer_filesystem(settings) as fs:
            return await reconcile(
                store,
                fs,
                settings.media_settings.remote_root,
                dry_run=dry_run,
                confirm_mass=confirm_mass,
            )
    except Exception:
        return counters("aborted_listing")
