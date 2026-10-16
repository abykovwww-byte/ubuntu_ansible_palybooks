"""Scheduled launcher: logs only validated counters and configured slugs."""

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from .config import load_settings

CHAT_COUNTS = {
    "fetched",
    "upserted",
    "deleted",
    "service_skipped",
    "redacted",
    "media_uploaded",
    "media_bytes",
    "media_failed",
    "forward_fetched",
    "refresh_fetched",
    "flood_wait_seconds",
}
MEDIA_COUNTS = {
    "fsck_orphans_deleted",
    "fsck_rows_reset",
    "fsck_delete_failed",
    "fsck_bytes_freed",
    "fsck_planned_deletes",
    "fsck_planned_resets",
    "media_deferred_path",
    "media_quota",
    "media_budget_deferred",
    "media_new_failed",
    "media_cleanup_failed",
}
STATUSES = {"ok", "partial", "failed", "new", "backfilling", "lost_access", "rejected"}
FSCK_STATUSES = {
    "ok",
    "incomplete",
    "aborted_db",
    "aborted_listing",
    "aborted_stat",
    "aborted_mass",
}


def safe_report(payload, specs):
    result = {
        "status": payload.get("status") if payload.get("status") in STATUSES else "failed",
        "counts": {},
    }
    source = payload.get("counts", {})
    for spec in specs:
        row = source.get(str(spec.chat_id), {})
        result["counts"][spec.slug] = {
            key: value
            for key, value in row.items()
            if key in CHAT_COUNTS and type(value) is int and value >= 0
        }
        if row.get("status") in STATUSES:
            result["counts"][spec.slug]["status"] = row["status"]
    media = source.get("media", {})
    result["media"] = {
        key: value
        for key, value in media.items()
        if key in MEDIA_COUNTS and type(value) is int and value >= 0
    }
    if media.get("fsck_status") in FSCK_STATUSES:
        result["media"]["fsck_status"] = media["fsck_status"]
    return result


def collector_command(settings, args):
    specs = settings.chats
    command = [sys.executable, "-m", "tg_archive.cli", "run"]
    if getattr(args, "config", None):
        command = [sys.executable, "-m", "tg_archive.cli", "--config", str(args.config), "run"]
    if args.chat:
        specs = [spec for spec in specs if spec.slug == args.chat]
        if not specs:
            raise ValueError("Scheduled chat must already be configured")
        command.extend(["--chat", args.chat])
    if args.no_media:
        command.append("--no-media")
    return command, specs


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--chat", help="Already configured chat slug; default: all selected chats")
    parser.add_argument("--no-media", action="store_true", help="Collect text and metadata only")
    args = parser.parse_args(argv)
    config = args.config or Path(__file__).resolve().parents[2] / "tg-archive.toml"
    config = config.resolve()
    root = config.parent
    started = datetime.now(UTC)
    report = {"started_at": started.isoformat(), "status": "failed"}
    code = 1
    try:
        settings = load_settings(config)
        command, specs = collector_command(settings, args)
        report.update(scope=[spec.slug for spec in specs], no_media=args.no_media)
        child = subprocess.run(
            command,
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=4 * 3600,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        code = child.returncode
        stderr = child.stderr.decode("utf-8", errors="replace")
        for prefix, kind in (
            ("Telegram connection timed out", "telegram_connect_timeout"),
            ("Telegram network failure", "telegram_network_error"),
            ("Another tg-sync holds the lock", "locked"),
            ("Telegram session expired", "session_expired"),
            ("Operation failed; no exception parameters logged", "collector_error"),
        ):
            if stderr.strip().startswith(prefix):
                report["failure_kind"] = kind
                break
        # Never write raw stdout/stderr, even if a future dependency adds unsafe diagnostics.
        report.update(safe_report(json.loads(child.stdout), specs))
    except Exception:
        code = code if code != 0 else 1
        report["status"] = "failed"
        report["notice"] = "Scheduled collector failed; no raw diagnostics logged"
    report.update(finished_at=datetime.now(UTC).isoformat(), exit_code=code)
    folder = root / "reports"
    folder.mkdir(exist_ok=True)
    path = folder / ("sync-" + started.strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
