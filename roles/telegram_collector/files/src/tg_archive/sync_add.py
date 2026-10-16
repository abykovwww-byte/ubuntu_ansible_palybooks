"""Snapshot-only resolution and append-only selection. No Telegram/network imports."""

import os
import re
import tempfile
import tomllib
from datetime import date
from urllib.parse import urlparse

from ruamel.yaml import YAML

from .locking import sync_lock
from .reader_config import KINDS, SLUG, selection, snapshot
from .scope import GROUP_KINDS, peer_valid, private_ids


def resolve(value: str, rows: list, approved=()) -> dict:
    value = value.strip()
    if not value or len(value) > 512:
        raise ValueError("Invalid chat selector")
    ident, username = None, None
    if re.fullmatch(r"-?[0-9]+", value):
        ident = int(value)
        if ident == 0 or abs(ident) > 2**63 - 1:
            raise ValueError("Invalid chat id")
    elif value.startswith("@"):
        username = value[1:]
    elif re.match(r"(?i)(?:https?://)?(?:www\.)?t\.me/", value):
        url = urlparse(value if "://" in value else "https://" + value)
        parts = url.path.strip("/").split("/")
        if url.hostname not in {"t.me", "www.t.me"} or url.query or url.fragment:
            raise ValueError("Invalid Telegram link")
        if parts[0] == "c" and len(parts) in {3, 4} and all(p.isdecimal() for p in parts[1:]):
            if int(parts[1]) <= 0:
                raise ValueError("Invalid private link")
            ident = -(10**12 + int(parts[1]))
        elif len(parts) == 2 and parts[1].isdecimal():
            username = parts[0]
        else:
            raise ValueError("Use a message link")
    else:
        matches = [r for r in rows if r.get("title") == value]
        if len(matches) > 1:
            return {"status": "ambiguous", "candidates": matches}
        if not matches:
            return {"status": "unresolved", "reason": "Title absent from dialog snapshot"}
        return verified(matches[0], approved)
    if username is not None:
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            raise ValueError("Invalid username")
        matches = [r for r in rows if (r.get("username") or "").casefold() == username.casefold()]
    else:
        matches = [
            r
            for r in rows
            if r.get("chat_id") == ident
            or (
                ident > 0
                and r.get("raw_channel_id") == ident
                and r.get("kind") in GROUP_KINDS - {"group"}
            )
        ]
    if len(matches) > 1:
        return {"status": "ambiguous", "candidates": matches}
    if matches:
        return verified(matches[0], approved)
    if ident is not None and ident < -(10**12) and abs(ident) <= 2**63 - 1:
        return {"id": ident, "verified": False}
    if ident is not None:
        return {"status": "rejected", "reason": "Unknown id is not a verified channel"}
    # Numeric peer ids are mandatory in chats.yaml; usernames alone cannot supply one.
    return {"status": "unresolved", "reason": "Refresh snapshot or provide private channel id/link"}


def verified(row: dict, approved=()) -> dict:
    if row.get("kind") not in KINDS or not peer_valid(
        row.get("chat_id"), row.get("kind"), approved
    ):
        return {"status": "rejected", "reason": "Peer/type is not owner-approved"}
    result = {"id": row["chat_id"], "verified": True}
    if row["chat_id"] > 0:
        result["kind"] = row["kind"]
    return result


def add(settings, chat: str, project: str | None, since: str | None, slug: str | None) -> dict:
    if project is not None and (not project.strip() or len(project) > 200):
        raise ValueError("Invalid project")
    start = date.fromisoformat(since) if since else settings.default_since
    if slug is not None and (len(slug) > 100 or not re.fullmatch(SLUG, slug)):
        raise ValueError("Invalid slug")
    with sync_lock(settings.root):
        data = selection(settings.root)
        rows, status = snapshot(settings.root)
        approved = private_ids(
            tomllib.loads((settings.root / "tg-archive.toml").read_text(encoding="utf-8-sig")),
            settings.root,
        )
        resolved = resolve(chat, rows, approved)
        if "id" not in resolved:
            return {**resolved, "snapshot_status": status}
        existing = next((e for e in data["chats"] if e["id"] == resolved["id"]), None)
        if existing:
            return {"status": "existing", "entry": dict(existing)}
        slug = slug or "chat-" + str(abs(resolved["id"]))
        if any(e["slug"] == slug for e in data["chats"]):
            raise ValueError("Slug already selected")
        entry = {
            "id": resolved["id"],
            "slug": slug,
            "since": start,
            "added_by": "llm",
            "added_at": date.today(),
            "verified": resolved["verified"],
        }
        if project is not None:
            entry["project"] = project
        if "kind" in resolved:
            entry["kind"] = resolved["kind"]
        data["chats"].append(entry)
        fd, filename = tempfile.mkstemp(prefix=".chats-", suffix=".tmp", dir=settings.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                YAML(typ="rt").dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(filename, settings.root / "chats.yaml")
        finally:
            if os.path.exists(filename):
                os.unlink(filename)
        return {
            "status": "added",
            "entry": entry,
            "next_sync": f"Следующий прогон расписания или tg-sync run --chat {slug}",
        }
