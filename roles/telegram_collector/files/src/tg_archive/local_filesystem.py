"""Local media adapter with the same complete-listing and atomic-rename contract."""

import os
import shutil
import stat
from pathlib import Path

import asyncssh

from .paths import within_root


def checked(path):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("Local media root must be absolute")
    for item in [*reversed(path.parents), path]:
        try:
            info = item.lstat()
        except FileNotFoundError:
            raise asyncssh.SFTPNoSuchFile("Media entry absent") from None
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("Symlinks are forbidden in media paths")
    return path


class PartFile:
    def __init__(self, path):
        self.path = path

    async def __aenter__(self):
        descriptor = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o640
        )
        self.file = os.fdopen(descriptor, "wb")
        return self

    async def write(self, data):
        self.file.write(data)

    async def __aexit__(self, *args):
        try:
            self.file.flush()
            os.fsync(self.file.fileno())
        finally:
            self.file.close()


class LocalFilesystem:
    async def list_files(self, root):
        base = checked(root)
        if not base.is_dir():
            raise ValueError("Media root is not a directory")
        files = {}

        def visit(folder):
            for path in folder.iterdir():
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    visit(path)
                elif stat.S_ISREG(info.st_mode):
                    files[path.relative_to(base).as_posix()] = info.st_size
                else:
                    raise ValueError("Symlink or special file in media tree")

        visit(base)
        return files

    async def stat(self, root, relative):
        path = checked(within_root(root, relative))
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Expected a regular media file")
        return info.st_size

    async def remove(self, root, relative):
        path = checked(within_root(root, relative))
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("Expected a regular media file")
        path.unlink()

    async def mkdir_parents(self, root, relative):
        base = checked(root)
        parent = Path(within_root(root, relative)).parent
        for part in [
            base,
            *[
                base.joinpath(*parent.relative_to(base).parts[:i])
                for i in range(1, len(parent.relative_to(base).parts) + 1)
            ],
        ]:
            try:
                part.mkdir(mode=0o750)
            except FileExistsError:
                pass
            if not stat.S_ISDIR(part.lstat().st_mode):
                raise ValueError("Media parent is not a real directory")

    def open_part(self, root, relative):
        path = Path(within_root(root, relative))
        checked(path.parent)
        return PartFile(path)

    async def rename(self, root, source, target):
        source_path = checked(within_root(root, source))
        target_path = Path(within_root(root, target))
        checked(target_path.parent)
        if target_path.is_symlink():
            raise ValueError("Symlink target is forbidden")
        os.replace(source_path, target_path)
        descriptor = os.open(target_path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    async def free_bytes(self, root):
        return shutil.disk_usage(checked(root)).free
