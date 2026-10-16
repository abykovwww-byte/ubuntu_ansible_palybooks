"""SFTP reader and verified temporary copies; no Telegram/collector credentials."""

import hashlib
import os
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import asyncssh

from .fetch_cache import checked_path, cleanup_locked, mark_web
from .locking import sync_lock
from .paths import safe_name, within_root
from .reader import attachment
from .reader_config import SLUG


async def copy_file(settings, row):
    metadata = attachment(row, settings.fetch.max_mb * 1024**2) if row else None
    if metadata is None:
        return {"status": "refused", "reason": "no_attachment", "attachment": metadata}
    if not metadata["fetchable"]:
        return {
            "status": "refused",
            "reason": "too_large" if row["media_status"] == "done" else "not_downloaded",
            "attachment": metadata,
        }
    expected = row["media_sha256"]
    if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
        return {"status": "refused", "reason": "missing_checksum", "attachment": metadata}
    slug = row["slug"]
    if not re.fullmatch(SLUG, slug) or row["msg_id"] <= 0:
        raise ValueError("Invalid file identity")
    relative = row["media_path"]
    remote = within_root(settings.remote_root, relative)
    if PurePosixPath(relative).parts[0] != slug:
        raise ValueError("Media path does not belong to chat")
    ext = row["media_ext"]
    ext = ext if ext and re.fullmatch(r"[a-z0-9]{1,16}", ext) else None
    name = row["media_name"] or ("file." + ext if ext else "file")
    target = settings.fetch.root / slug / safe_name(f"{row['msg_id']}-{safe_name(name)}")
    checked_path(target)
    with sync_lock(settings.fetch.root):
        cleanup_locked(settings.fetch)
        target.parent.mkdir(parents=True, exist_ok=True)
        checked_path(target)
        part = target.with_name(target.name + "." + uuid.uuid4().hex + ".part")
        try:
            async with asyncssh.connect(
                settings.host,
                port=settings.port,
                username="tgmedia-ro",
                client_keys=[settings.key],
                known_hosts=str(Path.home() / ".ssh/known_hosts"),
                agent_path=None,
                connect_timeout=15,
                login_timeout=15,
                keepalive_interval=15,
                keepalive_count_max=3,
            ) as conn:
                async with conn.start_sftp_client() as client:
                    root = PurePosixPath(settings.remote_root)
                    paths = [
                        str(PurePosixPath(*root.parts[:i])) for i in range(2, len(root.parts) + 1)
                    ]
                    parts = PurePosixPath(relative).parts
                    paths += [
                        within_root(settings.remote_root, str(PurePosixPath(*parts[:i])))
                        for i in range(1, len(parts))
                    ]
                    for path in paths:
                        if (await client.lstat(path)).type != asyncssh.FILEXFER_TYPE_DIRECTORY:
                            raise ValueError("Remote parent is not a directory")
                    attrs = await client.lstat(remote)
                    if (
                        attrs.type != asyncssh.FILEXFER_TYPE_REGULAR
                        or attrs.size != row["media_size"]
                    ):
                        return {
                            "status": "refused",
                            "reason": "size_mismatch",
                            "attachment": metadata,
                        }
                    digest, size = hashlib.sha256(), 0
                    with part.open("xb") as output:
                        async with client.open(remote, "rb") as source:
                            while chunk := await source.read(
                                min(1024**2, row["media_size"] - size + 1)
                            ):
                                size += len(chunk)
                                if size > row["media_size"]:
                                    raise ValueError("Remote file grew during transfer")
                                digest.update(chunk)
                                output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if size != row["media_size"] or digest.hexdigest() != expected:
                        return {
                            "status": "refused",
                            "reason": "checksum_mismatch",
                            "attachment": metadata,
                        }
            mark_web(part)
            checked_path(target)
            os.replace(part, target)
            if "ZoneId=3" not in Path(str(target) + ":Zone.Identifier").read_text(encoding="ascii"):
                target.unlink()
                raise ValueError("Mark-of-the-Web lost during rename")
            now = datetime.now(UTC)
            os.utime(target, (time.time(), now.timestamp()))
            return {
                "status": "ok",
                "path": str(target),
                "attachment": metadata,
                "expires_at": now + timedelta(hours=settings.fetch.ttl_hours),
                "notice": "Недоверенный файл. Можно читать; нельзя запускать, распаковывать архивы "
                "или включать макросы. Удаляется при первой очистке после expires_at.",
            }
        finally:
            part.unlink(missing_ok=True)
