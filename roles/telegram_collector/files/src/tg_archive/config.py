"""Strict local configuration. No environment interpolation or secret reprs."""

from __future__ import annotations

import ipaddress
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from ruamel.yaml import YAML

from .scope import peer_valid, private_ids


class ConfigError(ValueError):
    """Messages contain field names, never values of configuration or secrets."""


def positive(value, name: str, *, zero: bool = False) -> int:
    if type(value) is not int or value < (0 if zero else 1):
        raise ConfigError(f"Invalid {name}")
    return value


def date_only(value, name: str) -> date:
    if type(value) is not date:
        raise ConfigError(f"Invalid {name}: use YYYY-MM-DD")
    return value


@dataclass(frozen=True)
class ChatSpec:
    chat_id: int
    slug: str
    since: date
    media: bool = True
    allow_protected: bool = False
    project: str | None = None
    kind: str | None = None


@dataclass(frozen=True)
class MediaSettings:
    sftp_host: str = "100.117.52.16"
    sftp_port: int = 22
    remote_root: str = "/media"
    transfer_path: str = "lan"
    lan_cidr: str = "192.168.1.0/24"
    max_per_run_gb: int = 20
    max_total_gb: int = 300
    min_free_gb: int = 50
    storage: str = "sftp"


@dataclass(frozen=True)
class Settings:
    root: Path
    default_since: date
    refresh_days: int
    max_per_chat_run: int
    max_file_mb: int
    chats: tuple[ChatSpec, ...]
    api_id: int | None = field(repr=False)
    api_hash: str | None = field(repr=False)
    media_kinds: tuple[str, ...] = ("document", "photo", "video", "audio", "voice")
    media_settings: MediaSettings = field(default_factory=MediaSettings)
    approved_private_ids: tuple[int, ...] = ()

    def require_credentials(self) -> None:
        if not self.api_id or not self.api_hash:
            raise ConfigError("Fill TG_API_ID and TG_API_HASH in sync.env")


def load_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    result = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in result:
            raise ConfigError("Invalid or duplicate key in sync.env")
        if "SESSION" in key:
            raise ConfigError("Telegram sessions must not be stored in sync.env")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise ConfigError("Invalid quoting in sync.env")
            value = value[1:-1]
        result[key] = value
    return result


def load_settings(config: Path) -> Settings:
    config = config.resolve()
    data = tomllib.loads(config.read_text(encoding="utf-8-sig"))
    since = date_only(data.get("default_since"), "default_since")
    refresh = positive(data.get("refresh_days"), "refresh_days", zero=True)
    maximum = positive(data.get("max_per_chat_run"), "max_per_chat_run")
    media, fetch = data.get("media", {}), data.get("fetch", {})
    if media.get("storage", "sftp") not in {"sftp", "local"}:
        raise ConfigError("Invalid media.storage")
    if media.get("transfer_path") not in {"lan", "direct", "any"}:
        raise ConfigError("Invalid media.transfer_path")
    try:
        ipaddress.ip_network(media.get("lan_cidr", ""))
        ipaddress.ip_address(media.get("sftp_host", ""))
    except ValueError:
        raise ConfigError("Invalid media network configuration") from None
    remote = media.get("remote_root")
    if (
        not isinstance(remote, str)
        or not remote.startswith("/")
        or remote == "/"
        or ".." in remote.split("/")
        or "\\" in remote
        or "\0" in remote
    ):
        raise ConfigError("Invalid media.remote_root")
    port = positive(media.get("sftp_port"), "media.sftp_port")
    if port > 65535:
        raise ConfigError("Invalid media.sftp_port")
    for key in ("max_file_mb", "max_per_run_gb", "max_total_gb", "min_free_gb"):
        positive(media.get(key), "media." + key)
    kinds = media.get("kinds")
    allowed = {"document", "photo", "video", "audio", "voice", "video_note"}
    if not isinstance(kinds, list) or not kinds or any(k not in allowed for k in kinds):
        raise ConfigError("Invalid media.kinds")
    if not isinstance(fetch.get("local_dir"), str) or not fetch["local_dir"]:
        raise ConfigError("Invalid fetch.local_dir")
    positive(fetch.get("max_mb"), "fetch.max_mb")
    ttl = fetch.get("ttl_hours")
    if type(ttl) not in (int, float) or not 0 < ttl < float("inf"):
        raise ConfigError("Invalid fetch.ttl_hours")
    secrets = load_env(config.parent / "sync.env")
    api_id, api_hash = secrets.get("TG_API_ID", ""), secrets.get("TG_API_HASH", "")
    if api_id and (not api_id.isascii() or not api_id.isdecimal() or int(api_id) <= 0):
        raise ConfigError("Invalid TG_API_ID")
    if api_hash and not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
        raise ConfigError("Invalid TG_API_HASH")
    approved = private_ids(data, config.parent)
    chats = load_chats(config.parent / "chats.yaml", since, approved)
    return Settings(
        config.parent,
        since,
        refresh,
        maximum,
        media["max_file_mb"],
        chats,
        int(api_id) if api_id else None,
        api_hash or None,
        tuple(kinds),
        MediaSettings(
            **{key: media[key] for key in MediaSettings.__dataclass_fields__ if key in media}
        ),
        approved,
    )


def load_chats(path: Path, default_since: date, approved=()) -> tuple[ChatSpec, ...]:
    if not path.exists():
        return ()
    try:
        data = YAML(typ="safe").load(path.read_text(encoding="utf-8-sig"))
    except Exception:
        raise ConfigError("Invalid chats.yaml") from None
    if not isinstance(data, dict) or set(data) != {"chats"} or not isinstance(data["chats"], list):
        raise ConfigError("Invalid chats.yaml structure")
    result, ids, slugs = [], set(), set()
    for item in data["chats"]:
        if not isinstance(item, dict):
            raise ConfigError("Invalid chat entry")
        chat_id, slug = item.get("id"), item.get("slug")
        if not peer_valid(chat_id, item.get("kind"), approved):
            raise ConfigError("Invalid or unapproved chat peer/type")
        if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
            raise ConfigError("Invalid chat slug")
        if chat_id in ids or slug in slugs:
            raise ConfigError("Duplicate chat id or slug")
        for key in ("media", "allow_protected"):
            if type(item.get(key, key == "media")) is not bool:
                raise ConfigError("Invalid " + key)
        chat_since = item.get("since", default_since)
        if isinstance(chat_since, str):
            try:
                chat_since = date.fromisoformat(chat_since)
            except ValueError:
                raise ConfigError("Invalid chat since") from None
        if isinstance(chat_since, datetime):
            raise ConfigError("Invalid chat since")
        if item.get("project") is not None and not isinstance(item["project"], str):
            raise ConfigError("Invalid chat project")
        result.append(
            ChatSpec(
                chat_id,
                slug,
                date_only(chat_since, "chat since"),
                item.get("media", True),
                item.get("allow_protected", False),
                item.get("project"),
                item.get("kind"),
            )
        )
        ids.add(chat_id)
        slugs.add(slug)
    return tuple(result)
