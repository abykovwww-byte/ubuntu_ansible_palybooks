"""Owner commands: server fsck and explicit purge; no Telegram connection."""

import re

import asyncssh

from .config import ConfigError
from .database import Store, connect_database
from .fsck import counters
from .media import standalone_fsck
from .paths import within_root
from .sftp import writer_filesystem


async def fsck(settings, *, dry_run=False, confirm_mass=False):
    try:
        connection = await connect_database(settings.root)
    except Exception:
        return counters("aborted_db")
    async with connection:
        return await standalone_fsck(
            Store(connection), settings, dry_run=dry_run, confirm_mass=confirm_mass
        )


async def purge(settings, slug):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise ConfigError("Invalid purge slug")
    root = settings.media_settings.remote_root
    folder = within_root(root, slug)
    connection = await connect_database(settings.root)
    async with connection, writer_filesystem(settings) as fs:
        try:
            files = await fs.list_files(folder)
        except asyncssh.SFTPNoSuchFile:
            files = {}
        # Delete database rows before removing files. A failed cleanup remains repairable by fsck.
        async with connection.transaction():
            deleted = await connection.execute("DELETE FROM tg_chat WHERE slug=%s", (slug,))
        failures = 0
        for relative in files:
            try:
                await fs.remove(folder, relative)
            except asyncssh.SFTPNoSuchFile:
                pass
            except Exception:
                failures += 1
        # Directory cleanup does not affect file consistency; never traverse symlinks.
        if not failures and hasattr(fs, "client"):

            async def directories(path):
                from .sftp import is_dir

                async for entry in fs.client.scandir(path):
                    if entry.filename in {".", ".."}:
                        continue
                    if is_dir(entry.attrs):
                        await directories(within_root(path, entry.filename))
                await fs.client.rmdir(path)

            try:
                await directories(folder)
            except asyncssh.SFTPNoSuchFile:
                pass
        return {
            "slug": slug,
            "database_chats_removed": deleted.rowcount,
            "file_delete_failed": failures,
            "still_configured": any(c.slug == slug for c in settings.chats),
            "notice": "Owner must remove the chat from chats.yaml to prevent re-collection.",
        }
