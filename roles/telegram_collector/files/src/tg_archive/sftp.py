"""Bounded SFTP adapter; rejects symlinks and special files before mutations."""

from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath

import asyncssh

from .config import ConfigError, load_env
from .paths import within_root


def is_dir(attrs):
    return attrs.type == asyncssh.FILEXFER_TYPE_DIRECTORY


def is_file(attrs):
    return attrs.type == asyncssh.FILEXFER_TYPE_REGULAR


class Filesystem:
    def __init__(self, client):
        self.client = client

    async def list_files(self, root):
        root = str(PurePosixPath(root))
        if not is_dir(await self.client.lstat(root)):
            raise ValueError("Media root is not a real directory")
        files = {}

        async def visit(relative=""):
            parent = root if not relative else within_root(root, relative)
            async for entry in self.client.scandir(parent):
                if entry.filename in {".", ".."}:
                    continue
                if "/" in entry.filename or "\\" in entry.filename:
                    raise ValueError("Unsafe listing entry")
                name = (relative + "/" if relative else "") + entry.filename
                within_root(root, name)
                if is_dir(entry.attrs):
                    await visit(name)
                elif is_file(entry.attrs) and entry.attrs.size is not None:
                    files[name] = entry.attrs.size
                else:
                    raise ValueError("Symlink or special file in media tree")

        await visit()
        return files

    async def stat(self, root, relative):
        attrs = await self.client.lstat(within_root(root, relative))
        if not is_file(attrs) or attrs.size is None:
            raise ValueError("Expected a regular file")
        return attrs.size

    async def remove(self, root, relative):
        await self.client.remove(within_root(root, relative))

    async def mkdir_parents(self, root, relative):
        parent = PurePosixPath(relative).parent
        prefixes = [
            root,
            *[
                within_root(root, str(PurePosixPath(*parent.parts[:i])))
                for i in range(1, len(parent.parts) + 1)
            ],
        ]
        for prefix in prefixes:
            try:
                attrs = await self.client.lstat(prefix)
            except asyncssh.SFTPNoSuchFile:
                await self.client.mkdir(prefix, attrs=asyncssh.SFTPAttrs(permissions=0o750))
                attrs = await self.client.lstat(prefix)
            if not is_dir(attrs):
                raise ValueError("Media parent is not a real directory")

    def open_part(self, root, relative):
        return self.client.open(
            within_root(root, relative), "wb", attrs=asyncssh.SFTPAttrs(permissions=0o640)
        )

    async def rename(self, root, source, target):
        await self.client.posix_rename(within_root(root, source), within_root(root, target))

    async def free_bytes(self, root):
        attrs = await self.client.statvfs(root)
        return attrs.bavail * (attrs.frsize or attrs.bsize)


@asynccontextmanager
async def writer_filesystem(settings):
    if getattr(settings.media_settings, "storage", "sftp") == "local":
        from .local_filesystem import LocalFilesystem

        yield LocalFilesystem()
        return
    key = load_env(settings.root / "sync.env").get("TG_SFTP_RW_KEY")
    if not key:
        raise ConfigError("Fill TG_SFTP_RW_KEY in sync.env")
    media = settings.media_settings
    async with asyncssh.connect(
        media.sftp_host,
        port=media.sftp_port,
        username="tgmedia-rw",
        client_keys=[key],
        known_hosts=str(Path.home() / ".ssh/known_hosts"),
        agent_path=None,
        connect_timeout=15,
        login_timeout=15,
        keepalive_interval=15,
        keepalive_count_max=3,
    ) as conn:
        async with conn.start_sftp_client() as client:
            yield Filesystem(client)
