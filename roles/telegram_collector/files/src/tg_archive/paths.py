"""Shared pure path rules: no Telegram imports, no platform MIME heuristics."""

import re
from pathlib import PurePosixPath


def safe_name(name: str) -> str:
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).rstrip(" .")
    if not name or name in {".", ".."}:
        name = "file"
    if re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", name):
        name = "_" + name
    suffix = PurePosixPath(name).suffix
    if len(suffix.encode("utf-8")) > 32:
        suffix = ""
    stem = name[: -len(suffix)] if suffix else name
    stem = stem.encode("utf-8")[: 200 - len(suffix.encode("utf-8"))].decode(
        "utf-8", errors="ignore"
    )
    return stem + suffix


def within_root(root: str, relative: str) -> str:
    # A stored path is relative, never an OS-dependent absolute/drive path.
    if (
        not relative
        or relative.startswith("/")
        or "\\" in relative
        or "\0" in relative
        or any(p in {"", ".", ".."} for p in relative.split("/"))
    ):
        raise ValueError("Unsafe relative media path")
    root_path = PurePosixPath(root)
    if (
        not root_path.is_absolute()
        or str(root_path) == "/"
        or ".." in root_path.parts
        or "\\" in root
        or "\0" in root
    ):
        raise ValueError("Unsafe media root")
    path = root_path / relative
    if not path.is_relative_to(root_path):
        raise ValueError("Media path outside root")
    return str(path)


def media_path(spec, row):
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", spec.slug) or row["msg_id"] <= 0:
        raise ValueError("Invalid media path components")
    if row["media_name"]:
        name = safe_name(f"{row['msg_id']}-{safe_name(row['media_name'])}")
    else:
        ext = row["media_ext"]
        if ext is not None and not re.fullmatch(r"[a-z0-9]{1,16}", ext):
            ext = None
        name = str(row["msg_id"]) + ("." + ext if ext else "")
    return f"{spec.slug}/{row['sent_at']:%Y-%m}/{name}"
