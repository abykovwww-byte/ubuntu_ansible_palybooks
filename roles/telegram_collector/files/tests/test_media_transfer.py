"""All messages, file names and file bytes are synthetic."""

import asyncio
import hashlib
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import asyncssh
import pytest
from telethon import functions
from test_collector import SPEC, STAMP, group
from test_content import document, message

import tg_archive.media as media
from tg_archive.content import project_message, transition
from tg_archive.telegram import RequestPolicy


class Route:
    def __init__(self, allowed=(True,)):
        self.values = iter(allowed)
        self.last = True
        self.calls = 0

    async def allowed(self):
        self.calls += 1
        self.last = next(self.values, self.last)
        return self.last


class Files:
    def __init__(self, fail_write=False, fail_cleanup=False):
        self.files = {}
        self.written = 0
        self.opens = 0
        self.fail_write = fail_write
        self.fail_cleanup = fail_cleanup

    async def mkdir_parents(self, *args):
        pass

    @asynccontextmanager
    async def open_part(self, root, path):
        self.opens += 1
        self.files[path] = b""

        async def write(chunk):
            self.files[path] += chunk
            self.written += len(chunk)
            if self.fail_write:
                raise asyncssh.SFTPFailure("Synthetic write interruption")

        yield SimpleNamespace(write=write)

    async def stat(self, root, path):
        return len(self.files[path])

    async def rename(self, root, part, final):
        self.files[final] = self.files.pop(part)

    async def remove(self, root, path):
        if self.fail_cleanup:
            raise asyncssh.SFTPFailure("Synthetic cleanup interruption")
        if path not in self.files:
            raise asyncssh.SFTPNoSuchFile("Synthetic absent part")
        self.files.pop(path)

    async def free_bytes(self, root):
        return 1000 * 1024**3


class Download:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __aiter__(self):
        self.iterator = iter(self.chunks)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration from None

    async def close(self):
        self.closed = True


class Client:
    def __init__(self, chunks=(b"x" * 25,), messages=None):
        self.chunks = chunks
        self.messages = messages or {1: message(media=document(size=sum(map(len, chunks))))}
        self.downloads = []
        self.point_reads = 0

    async def get_messages(self, entity, ids):
        self.point_reads += 1
        return self.messages[ids]

    def iter_download(self, *args, **kwargs):
        RequestPolicy().check(functions.auth.ExportAuthorizationRequest(4))
        iterator = Download(self.chunks)
        self.downloads.append(iterator)
        return iterator


class Store:
    def __init__(self, rows):
        self.rows = {(r["chat_id"], r["msg_id"]): deepcopy(r) for r in rows}

    async def unfinished(self, ids, pending_only=False):
        return [
            deepcopy(r)
            for r in self.rows.values()
            if r["chat_id"] in ids
            and (
                r["media_status"] == "pending"
                or (not pending_only and (r["media_status"] or "").startswith("skipped_"))
            )
        ]

    async def set_eligibility(self, row, status):
        self.rows[row["chat_id"], row["msg_id"]]["media_status"] = status

    async def batch(self, chat_id, rows, users, cursor, counts):
        for row in rows:
            key = row["chat_id"], row["msg_id"]
            self.rows[key] = transition(row.copy(), self.rows.get(key))

    async def done_bytes(self):
        return sum(r["media_size"] for r in self.rows.values() if r["media_status"] == "done")

    async def media_done(self, row, path, sha256):
        self.rows[row["chat_id"], row["msg_id"]].update(
            media_status="done", media_path=path, media_sha256=sha256
        )

    async def media_failure(self, row, error):
        saved = self.rows[row["chat_id"], row["msg_id"]]
        saved["media_attempts"] += 1
        saved["media_error"] = error
        failed = saved["media_attempts"] >= 3
        saved["media_status"] = "failed" if failed else "pending"
        return failed

    async def quota_remaining(self, ids):
        changed = 0
        for r in self.rows.values():
            if r["chat_id"] in ids and r["media_status"] == "pending":
                r["media_status"] = "skipped_quota"
                changed += 1
        return changed


def row(settings, size=25, msg_id=1):
    return project_message(
        message(msg_id, media=document(size=size)), SPEC, settings, False, STAMP
    )[0]


def stats():
    return {"-7": dict(media_uploaded=0, media_bytes=0, media_failed=0)}


def test_stream_rename_sha_and_done_once(settings):
    saved = row(settings)
    client, fs, store = Client(), Files(), Store([saved])
    report = asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: SPEC}, {-7: group()}, stats(), fs, Route()
        )
    )
    actual = store.rows[-7, 1]
    assert actual["media_status"] == "done"
    assert actual["media_sha256"] == hashlib.sha256(b"x" * 25).hexdigest()
    assert list(fs.files) == [actual["media_path"]]
    assert not report["media_new_failed"] and client.downloads[0].closed
    asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: SPEC}, {-7: group()}, stats(), fs, Route()
        )
    )
    assert len(client.downloads) == 1


def test_route_change_at_exact_boundary_cleans_part_without_attempt(settings, monkeypatch):
    monkeypatch.setattr(media, "CHECKPOINT_BYTES", 64)
    saved = row(settings, 100)
    store, fs, client = Store([saved]), Files(), Client((b"x" * 50, b"x" * 50))
    report = asyncio.run(
        media.transfer_queue(
            client,
            store,
            settings,
            {-7: SPEC},
            {-7: group()},
            stats(),
            fs,
            Route((True, True, True, False)),
        )
    )
    assert fs.written == 64 and not fs.files
    assert store.rows[-7, 1]["media_attempts"] == 0 and report["media_deferred_path"] == 1


def test_path_defer_still_does_not_charge_attempt_if_cleanup_failed(settings, monkeypatch):
    monkeypatch.setattr(media, "CHECKPOINT_BYTES", 64)
    store, fs, client = Store([row(settings, 100)]), Files(fail_cleanup=True), Client((b"x" * 100,))
    report = asyncio.run(
        media.transfer_queue(
            client,
            store,
            settings,
            {-7: SPEC},
            {-7: group()},
            stats(),
            fs,
            Route((True, True, True, False)),
        )
    )
    assert store.rows[-7, 1]["media_attempts"] == 0
    assert report["media_cleanup_failed"] == 1


def test_route_change_between_files_prevents_next_open(settings):
    rows = [row(settings, msg_id=i) for i in (1, 2)]
    msgs = {i: message(i, media=document()) for i in (1, 2)}
    store, fs, client = Store(rows), Files(), Client(messages=msgs)
    report = asyncio.run(
        media.transfer_queue(
            client,
            store,
            settings,
            {-7: SPEC},
            {-7: group()},
            stats(),
            fs,
            Route((True, True, True, False)),
        )
    )
    assert fs.opens == 1 and client.point_reads == 1 and report["media_deferred_path"] == 1


def test_sftp_error_records_attempt_keeps_metadata_and_cleans_part(settings):
    store, fs, client = Store([row(settings)]), Files(fail_write=True), Client()
    for _ in range(3):
        asyncio.run(
            media.transfer_queue(
                client, store, settings, {-7: SPEC}, {-7: group()}, stats(), fs, Route()
            )
        )
    saved = store.rows[-7, 1]
    assert saved["media_status"] == "failed" and saved["media_attempts"] == 3
    assert saved["media_error"] == "sftp_error" and saved["media_name"] == "schema.VSDX"
    assert not fs.files


@pytest.mark.parametrize(
    "change,expected",
    [
        ("disabled", "skipped_disabled"),
        ("protected", "skipped_protected"),
        ("kind", "skipped_kind"),
        ("size", "skipped_too_big"),
    ],
)
def test_old_pending_recalculates_before_any_download(settings, change, expected):
    spec, entity = SPEC, group()
    saved = row(settings, size=2 * 1024**2)
    if change == "disabled":
        spec = replace(spec, media=False)
    elif change == "protected":
        entity = group(True)
    elif change == "kind":
        settings = replace(settings, media_kinds=("photo",))
    elif change == "size":
        settings = replace(settings, max_file_mb=1)
    store = Store([saved])
    asyncio.run(media.recalculate(store, settings, {-7: spec}, {-7: entity}))
    client, fs = Client(), Files()
    asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: spec}, {-7: entity}, stats(), fs, Route()
        )
    )
    assert store.rows[-7, 1]["media_status"] == expected and not client.downloads


def test_removed_chat_and_done_failed_rows_are_unchanged(settings):
    saved = row(settings)
    for status in ("pending", "done", "failed"):
        store = Store([dict(saved, media_status=status)])
        asyncio.run(media.recalculate(store, settings, {}, {}))
        assert store.rows[-7, 1]["media_status"] == status


def test_disabled_to_pending_to_done(settings):
    store = Store([dict(row(settings), media_status="skipped_disabled")])
    asyncio.run(media.recalculate(store, settings, {-7: SPEC}, {-7: group()}))
    assert store.rows[-7, 1]["media_status"] == "pending"
    asyncio.run(
        media.transfer_queue(
            Client(), store, settings, {-7: SPEC}, {-7: group()}, stats(), Files(), Route()
        )
    )
    assert store.rows[-7, 1]["media_status"] == "done"


def test_quota_is_partial_condition_without_download(settings):
    store = Store([row(settings)])
    fs = Files()

    async def free(root):
        return settings.media_settings.min_free_gb * 1024**3

    fs.free_bytes = free
    client = Client()
    report = asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: SPEC}, {-7: group()}, stats(), fs, Route()
        )
    )
    assert report["media_quota"] == 1
    assert store.rows[-7, 1]["media_status"] == "skipped_quota" and not client.downloads


def test_unknown_size_and_stream_size_mismatch_never_done(settings):
    saved = row(settings, 25)
    store, fs = Store([saved]), Files()
    # Metadata promises 25, stream sends 24.
    client = Client(chunks=(b"x" * 24,), messages={1: message(media=document(size=25))})
    asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: SPEC}, {-7: group()}, stats(), fs, Route()
        )
    )
    assert store.rows[-7, 1]["media_error"] == "size_mismatch" and not fs.files


def test_failed_stream_charges_run_budget_before_next_file(settings, monkeypatch):
    size = 3 * 1024**3 // 4
    settings = replace(settings, media_settings=replace(settings.media_settings, max_per_run_gb=1))
    store = Store([row(settings, size, i) for i in (1, 2)])
    client = Client(messages={i: message(i, media=document(size=size)) for i in (1, 2)})
    calls = []

    async def failed(*args):
        budget = args[-1]
        budget.used += size
        calls.append(True)
        raise asyncssh.SFTPFailure("Synthetic failed stream")

    monkeypatch.setattr(media, "transmit", failed)
    report = asyncio.run(
        media.transfer_queue(
            client, store, settings, {-7: SPEC}, {-7: group()}, stats(), Files(), Route()
        )
    )
    assert len(calls) == 1 and report["media_budget_deferred"] == 1


def test_missing_local_tailscale_never_assumes_lan(settings, monkeypatch):
    import tg_archive.route as route

    monkeypatch.setattr(route.shutil, "which", lambda _: None)
    guard = route.RouteGuard(settings.media_settings)
    assert not asyncio.run(guard.allowed()) and guard.current is None


def test_stage_retains_fsck_result_if_later_database_operation_fails(settings, monkeypatch):
    @asynccontextmanager
    async def filesystem(*args):
        yield Files()

    async def checked(*args):
        return {"fsck_status": "ok"}

    async def failed(*args):
        raise ValueError("Synthetic database failure")

    monkeypatch.setattr(media, "writer_filesystem", filesystem)
    monkeypatch.setattr(media, "reconcile", checked)
    monkeypatch.setattr(media, "recalculate", failed)
    result = asyncio.run(
        media.media_stage(
            Client(), Store([]), settings, [SPEC], {-7: group()}, {"-7": {"status": "ok"}}
        )
    )
    assert result["fsck_status"] == "ok" and result["media_stage_error"] == "ValueError"


def test_photo_source_matches_largest_metadata_variant():
    from telethon import types

    msg = message(
        media=types.MessageMediaPhoto(
            photo=types.Photo(
                id=42,
                access_hash=999,
                file_reference=b"synthetic",
                date=STAMP,
                dc_id=4,
                sizes=[
                    types.PhotoSize(type="x", w=100, h=100, size=500),
                    types.PhotoSizeProgressive(type="y", w=200, h=200, sizes=[100, 300]),
                ],
            )
        )
    )
    source, dc_id = media.download_source(msg)
    assert source.thumb_size == "x" and dc_id == 4 and msg.file.size == 500
