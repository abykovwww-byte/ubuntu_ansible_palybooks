"""Local, disposable copies only. Shared with the collector; no source credentials."""

import os
import re
import stat
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .locking import AlreadyRunning, sync_lock


@dataclass(frozen=True)
class FetchSettings:
    root: Path
    max_mb: int
    ttl_hours: float


def load_fetch(config: Path) -> FetchSettings:
    data = tomllib.loads(config.read_text(encoding="utf-8-sig"))["fetch"]
    root = Path(os.path.expandvars(data["local_dir"]))
    if not root.is_absolute() or "%" in str(root) or "$" in str(root):
        raise ValueError("Invalid fetch.local_dir")
    maximum, ttl = data["max_mb"], data["ttl_hours"]
    if type(maximum) is not int or maximum <= 0:
        raise ValueError("Invalid fetch.max_mb")
    if type(ttl) not in (int, float) or not 0 < ttl < float("inf"):
        raise ValueError("Invalid fetch.ttl_hours")
    return FetchSettings(root, maximum, ttl)


def checked_path(path: Path) -> None:
    """Reject every symlink/junction ancestor, including a pre-existing target."""
    for item in (*reversed(path.parents), path):
        try:
            attrs = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(attrs.st_mode) or getattr(attrs, "st_file_attributes", 0) & 0x400:
            raise ValueError("Reparse points are not permitted in fetch paths")
        if stat.S_ISREG(attrs.st_mode) and attrs.st_nlink != 1:
            raise ValueError("Hard links are not permitted in fetch paths")


def mark_web(path: Path) -> None:
    if os.name != "nt":
        raise ValueError("Mark-of-the-Web requires Windows/NTFS")
    checked_path(path)
    with Path(str(path) + ":Zone.Identifier").open("w", encoding="ascii") as handle:
        handle.write("[ZoneTransfer]\nZoneId=3\n")
    if "ZoneId=3" not in Path(str(path) + ":Zone.Identifier").read_text(encoding="ascii"):
        raise ValueError("Mark-of-the-Web verification failed")


def cleanup(settings: FetchSettings, *, now: float | None = None) -> int:
    checked_path(settings.root)
    if not settings.root.exists():
        return 0
    try:
        with sync_lock(settings.root):
            return cleanup_locked(settings, now=now)
    except AlreadyRunning:
        # An in-progress fetch holds the same OS lock. It performs cleanup itself.
        return 0


def cleanup_locked(settings: FetchSettings, *, now: float | None = None) -> int:
    cutoff = (time.time() if now is None else now) - settings.ttl_hours * 3600
    removed = 0
    for directory in settings.root.iterdir():
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", directory.name):
            continue
        checked_path(directory)
        if not directory.is_dir() or directory.name == "state":
            continue
        for path in directory.iterdir():
            checked_path(path)
            if path.is_file() and re.match(r"[1-9][0-9]*-", path.name):
                if path.stat().st_mtime <= cutoff:
                    path.unlink()
                    removed += 1
    return removed
