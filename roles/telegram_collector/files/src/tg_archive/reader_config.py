"""MCP configuration: intentionally independent of collector config and its secrets."""

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict
from ruamel.yaml import YAML

from .fetch_cache import FetchSettings, load_fetch
from .paths import within_root
from .scope import KINDS as KINDS
from .scope import peer_valid, private_ids

SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"


@dataclass(frozen=True)
class ReaderSettings:
    root: Path
    default_since: date
    refresh_days: int
    fetch: FetchSettings
    host: str
    port: int
    remote_root: str
    dsn: str = field(repr=False)
    key: str = field(repr=False)
    approved_private_ids: tuple[int, ...] = ()


def load_reader(config: Path, env_file: Path) -> ReaderSettings:
    config, env_file = config.absolute(), env_file.absolute()
    if env_file.name.casefold() != "mcp.env":
        raise ValueError("MCP accepts only mcp.env")
    secrets = {}
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key not in {"TG_DB_READER_DSN", "TG_SFTP_RO_KEY"} or key in secrets:
            raise ValueError("Unexpected or duplicate MCP credential field")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ValueError("Invalid MCP credential quoting")
            value = value[1:-1]
        secrets[key] = value
    dsn, key = secrets.get("TG_DB_READER_DSN"), secrets.get("TG_SFTP_RO_KEY")
    if not dsn or conninfo_to_dict(dsn).get("user") != "tg_reader" or not key:
        raise ValueError("MCP requires reader credentials")
    data = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    if type(data["default_since"]) is not date:
        raise ValueError("Invalid default_since")
    if type(data["refresh_days"]) is not int or data["refresh_days"] < 0:
        raise ValueError("Invalid refresh_days")
    media = data["media"]
    within_root(media["remote_root"], "path-check")
    if type(media["sftp_port"]) is not int or not 1 <= media["sftp_port"] <= 65535:
        raise ValueError("Invalid sftp_port")
    return ReaderSettings(
        config.parent,
        data["default_since"],
        data["refresh_days"],
        load_fetch(config),
        media["sftp_host"],
        media["sftp_port"],
        media["remote_root"],
        dsn,
        key,
        private_ids(data, config.parent),
    )


def selection(root: Path) -> dict:
    approved = private_ids(
        tomllib.loads((root / "tg-archive.toml").read_text(encoding="utf-8-sig")), root
    )
    data = YAML(typ="rt").load((root / "chats.yaml").read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"chats"} or not isinstance(data["chats"], list):
        raise ValueError("Invalid selection")
    ids, slugs = set(), set()
    for entry in data["chats"]:
        ident, slug = entry.get("id"), entry.get("slug")
        if not peer_valid(ident, entry.get("kind"), approved) or not isinstance(slug, str):
            raise ValueError("Invalid selected chat")
        if not re.fullmatch(SLUG, slug) or ident in ids or slug in slugs:
            raise ValueError("Invalid or duplicate selected chat")
        ids.add(ident)
        slugs.add(slug)
    return data


def snapshot(root: Path) -> tuple[list, str]:
    try:
        rows = json.loads((root / "state/dialogs.json").read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError()
        for row in rows:
            if type(row.get("chat_id")) is not int or not isinstance(row.get("kind"), str):
                raise ValueError()
            if not isinstance(row.get("title"), str):
                raise ValueError()
        dated = bool(rows)
        for row in rows:
            try:
                stamp = datetime.fromisoformat(row.get("snapshot_at", "").replace("Z", "+00:00"))
                dated = dated and stamp.tzinfo is not None
            except (ValueError, AttributeError):
                dated = False
        return rows, "ok" if dated else "undated"
    except (OSError, ValueError):
        return [], "unavailable"
