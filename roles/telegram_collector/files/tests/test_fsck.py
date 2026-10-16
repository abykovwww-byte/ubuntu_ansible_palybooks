import asyncio

import asyncssh
import pytest

from tg_archive.fsck import reconcile


class Store:
    def __init__(self, rows=(), fail_read=False, fail_reset=False):
        self.rows = list(rows)
        self.fail_read = fail_read
        self.fail_reset = fail_reset
        self.resets = []

    async def done_snapshot(self):
        if self.fail_read:
            raise OSError("Synthetic database read failure")
        return self.rows

    async def reset_done(self, rows):
        if self.fail_reset:
            raise OSError("Synthetic transaction failure")
        self.resets.extend(rows)
        return len(rows)


class Files:
    def __init__(self, listing=None, stats=None, fail_listing=False, fail_delete=()):
        self.listing = listing or {}
        self.stats = stats or {}
        self.fail_listing = fail_listing
        self.fail_delete = set(fail_delete)
        self.deleted = []
        self.stat_calls = []

    async def list_files(self, root):
        if self.fail_listing:
            raise asyncssh.SFTPPermissionDenied("Synthetic incomplete listing")
        return self.listing

    async def stat(self, root, path):
        self.stat_calls.append(path)
        result = self.stats.get(path, self.listing.get(path))
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise asyncssh.SFTPNoSuchFile("Synthetic missing file")
        return result

    async def remove(self, root, path):
        if path in self.fail_delete:
            raise OSError("Synthetic removal failure")
        self.deleted.append(path)


def row(path, size=5, msg_id=1):
    return dict(chat_id=-7, msg_id=msg_id, media_path=path, media_size=size)


@pytest.mark.parametrize(
    "failure,status",
    [
        ("db", "aborted_db"),
        ("listing", "aborted_listing"),
        ("stat", "aborted_stat"),
    ],
)
def test_all_reads_precede_every_mutation(failure, status):
    store = Store([row("last")], fail_read=failure == "db")
    fs = Files(
        {"orphan": 10},
        {"last": OSError("Synthetic permission failure")},
        fail_listing=failure == "listing",
    )
    if failure != "stat":
        fs.stats = {"last": 5}
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == status
    assert not fs.deleted and not store.resets


def test_missing_wrong_size_orphan_and_part_repair():
    store = Store([row("missing"), row("wrong", msg_id=2), row("correct", msg_id=3)])
    fs = Files({"wrong": 2, "correct": 5, "orphan": 10, "orphan.part": 3})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "ok" and report["fsck_rows_reset"] == 2
    assert set(fs.deleted) == {"wrong", "orphan", "orphan.part"}
    assert report["fsck_bytes_freed"] == 15


def test_stale_listing_with_correct_stat_is_not_reset():
    store = Store([row("alive")])
    fs = Files({}, {"alive": 5})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "ok" and not store.resets and not fs.deleted


def test_late_stat_failure_keeps_orphans_and_all_done_rows():
    store = Store([row("missing"), row("last", msg_id=2)])
    fs = Files({"orphan.part": 1}, {"last": asyncssh.SFTPPermissionDenied("Synthetic")})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert fs.stat_calls == ["missing", "last"]
    assert report["fsck_status"] == "aborted_stat" and not store.resets and not fs.deleted


def test_reset_failure_preserves_mismatched_and_orphan_files():
    store = Store([row("wrong")], fail_reset=True)
    fs = Files({"wrong": 2, "orphan": 5})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "incomplete" and not fs.deleted


def test_delete_failure_does_not_stop_other_deletions():
    store = Store()
    fs = Files({"a": 2, "b": 3}, fail_delete={"a"})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "incomplete"
    assert fs.deleted == ["b"] and report["fsck_delete_failed"] == 1


@pytest.mark.parametrize("mode", ["empty_db", "empty_root"])
def test_mass_guard_before_first_mutation(mode):
    rows = [row(f"p{i}", msg_id=i) for i in range(21)] if mode == "empty_root" else []
    listing = {f"p{i}": 5 for i in range(21)} if mode == "empty_db" else {}
    store, fs = Store(rows), Files(listing)
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "aborted_mass" and not store.resets and not fs.deleted
    report = asyncio.run(reconcile(store, fs, "/media", confirm_mass=True, dry_run=True))
    assert report["fsck_status"] == "ok" and not store.resets and not fs.deleted


def test_mass_guard_uses_both_thresholds():
    store = Store()
    fs = Files({f"p{i}": 5 for i in range(20)})
    assert asyncio.run(reconcile(store, fs, "/media"))["fsck_status"] == "ok"


@pytest.mark.parametrize("path", ["../outside", "/outside", "x/../../outside", "x\\outside"])
def test_unsafe_path_aborts_listing_without_mutations(path):
    store, fs = Store(), Files({path: 1})
    report = asyncio.run(reconcile(store, fs, "/media"))
    assert report["fsck_status"] == "aborted_listing" and not fs.deleted


def test_dry_run_returns_plan_without_mutations():
    store, fs = Store([row("missing")]), Files({"orphan": 10})
    report = asyncio.run(reconcile(store, fs, "/media", dry_run=True))
    assert report["fsck_planned_resets"] == 1 and report["fsck_planned_deletes"] == 1
    assert report["dry_run"] and not fs.deleted and not store.resets
