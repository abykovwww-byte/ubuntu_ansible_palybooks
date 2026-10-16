"""Two-phase consistency repair. Every read precedes every mutation."""

import asyncssh

from .paths import within_root


def counters(status="ok"):
    return dict(
        fsck_status=status,
        fsck_orphans_deleted=0,
        fsck_rows_reset=0,
        fsck_delete_failed=0,
        fsck_bytes_freed=0,
        fsck_planned_deletes=0,
        fsck_planned_resets=0,
    )


async def reconcile(store, fs, root, *, dry_run=False, confirm_mass=False):
    report = counters()
    try:
        rows = await store.done_snapshot()
    except Exception:
        return counters("aborted_db")
    try:
        files = await fs.list_files(root)
        for path in files:
            within_root(root, path)
        for row in rows:
            within_root(root, row["media_path"])
    except Exception:
        return counters("aborted_listing")
    known = {row["media_path"] for row in rows}
    deletes = {path: size for path, size in files.items() if path not in known}
    resets = []
    try:
        for row in rows:
            path, expected = row["media_path"], row["media_size"]
            if expected is not None and files.get(path) == expected:
                continue
            try:
                actual = await fs.stat(root, path)
            except asyncssh.SFTPNoSuchFile:
                resets.append(row)
                continue
            if actual != expected:
                resets.append(row)
                deletes[path] = actual
    except Exception:
        return counters("aborted_stat")
    report.update(fsck_planned_deletes=len(deletes), fsck_planned_resets=len(resets))
    mass = (len(deletes) > 20 and len(deletes) > len(files) * 0.1) or (
        len(resets) > 20 and len(resets) > len(rows) * 0.1
    )
    if mass and not confirm_mass:
        report["fsck_status"] = "aborted_mass"
        return report
    if dry_run:
        report["dry_run"] = True
        return report
    try:
        report["fsck_rows_reset"] = await store.reset_done(resets)
    except Exception:
        report["fsck_status"] = "incomplete"
        return report
    for path, size in deletes.items():
        try:
            await fs.remove(root, path)
        except asyncssh.SFTPNoSuchFile:
            pass
        except Exception:
            report["fsck_delete_failed"] += 1
            continue
        report["fsck_orphans_deleted"] += 1
        report["fsck_bytes_freed"] += size
    if report["fsck_delete_failed"]:
        report["fsck_status"] = "incomplete"
    return report
