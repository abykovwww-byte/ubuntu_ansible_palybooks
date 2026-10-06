"""Canonical, strict engagement contracts; no model-authored approval fields."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Denied(ValueError):
    """A request did not satisfy a trust, scope or execution boundary."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def hostname(value: str) -> str:
    value = value.strip().rstrip(".").lower()
    if not value or any(c in value for c in "/:@?#\\%\x00"):
        raise ValueError("Expected a hostname, without URL, credentials or wildcard")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("Seed must be a domain, not an IP address")
    value = value.encode("idna").decode("ascii")
    if len(value) > 253 or "." not in value or any(
        not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in value.split(".")
    ):
        raise ValueError("Invalid hostname")
    return value


def safe_path(value: str) -> str:
    if not value.startswith("/") or any(c in value for c in "\\\x00\r\n?#"):
        raise ValueError("Expected an unambiguous absolute URL path")
    decoded = unquote(value, errors="strict")
    # Conservative rejection: no double encoding or alternative slash/dot interpretation.
    if decoded != value or "//" in value or any(p in {".", ".."} for p in value.split("/")):
        raise ValueError("Encoded or ambiguous path is unsupported")
    return value


def path_contains(prefix: str, path: str) -> bool:
    prefix, path = safe_path(prefix), safe_path(path)
    return prefix == "/" or path == prefix.rstrip("/") or path.startswith(prefix.rstrip("/") + "/")


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Target(Contract):
    host: str
    scheme: str = "https"
    port: int = Field(default=443, ge=1, le=65535)
    paths: list[str] = Field(default_factory=lambda: ["/"], min_length=1, max_length=50)
    excluded_paths: list[str] = Field(default_factory=list, max_length=50)
    methods: list[str] = Field(default_factory=lambda: ["GET", "HEAD"], min_length=1, max_length=8)

    _host = field_validator("host")(hostname)

    @field_validator("scheme")
    @classmethod
    def scheme_supported(cls, value: str) -> str:
        if value not in {"http", "https"}:
            raise ValueError("Only HTTP and HTTPS targets supported in this contract version")
        return value

    @field_validator("paths", "excluded_paths")
    @classmethod
    def paths_supported(cls, values: list[str]) -> list[str]:
        return sorted(set(safe_path(v) for v in values))

    @field_validator("methods")
    @classmethod
    def methods_supported(cls, values: list[str]) -> list[str]:
        if any(v not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"} for v in values):
            raise ValueError("Unsupported HTTP method")
        return sorted(set(values))

    def permits(self, url: str, method: str) -> bool:
        p = urlsplit(url)
        try:
            if p.username or p.password or p.fragment or not p.hostname:
                return False
            if hostname(p.hostname) != self.host or p.scheme != self.scheme:
                return False
            if (p.port or (443 if p.scheme == "https" else 80)) != self.port:
                return False
            path = safe_path(p.path or "/")
            return method in self.methods and any(path_contains(v, path) for v in self.paths) and not any(
                path_contains(v, path) for v in self.excluded_paths
            )
        except (ValueError, UnicodeError):
            return False


class Budget(Contract):
    seconds: int = Field(default=7200, ge=1, le=7200)
    rps: int = Field(default=2, ge=1, le=10)
    concurrency: int = Field(default=2, ge=1, le=4)
    jobs: int = Field(default=100, ge=1, le=200)


class Authorization(Contract):
    schema_version: int = 1
    reference: str = Field(min_length=1, max_length=500)
    owner: str = Field(min_length=1, max_length=200)
    basis: str = Field(min_length=1, max_length=2000)
    hosts: list[str] = Field(min_length=1, max_length=2000)
    actions: list[str] = Field(default_factory=lambda: ["checks"])
    valid_from: int
    valid_until: int

    @field_validator("hosts")
    @classmethod
    def hosts_supported(cls, values: list[str]) -> list[str]:
        return sorted(set(hostname(v) for v in values))

    @model_validator(mode="after")
    def valid(self) -> "Authorization":
        if self.schema_version != 1 or self.valid_until <= self.valid_from:
            raise ValueError("Unsupported authorization version or invalid window")
        if not self.actions or any(v not in {"checks", "poc"} for v in self.actions):
            raise ValueError("Unsupported authorized action")
        return self


class Scope(Contract):
    schema_version: int = 1
    targets: list[Target] = Field(min_length=1, max_length=2000)
    authorization: Authorization
    expires_at: int
    allowed_actions: list[str] = Field(default_factory=lambda: ["checks"])
    budget: Budget = Field(default_factory=Budget)

    @model_validator(mode="after")
    def valid(self) -> "Scope":
        if self.schema_version != 1 or self.allowed_actions != ["checks"]:
            raise ValueError("First scope approves checks only")
        if "checks" not in self.authorization.actions or self.expires_at > self.authorization.valid_until:
            raise ValueError("Scope exceeds authorization")
        if any(t.host not in self.authorization.hosts for t in self.targets):
            raise ValueError("Target missing from authorization")
        if len({(t.host, t.scheme, t.port) for t in self.targets}) != len(self.targets):
            raise ValueError("Duplicate service targets must be merged explicitly")
        return self

    def check_window(self, now: float) -> None:
        if not self.authorization.valid_from <= now < min(self.expires_at, self.authorization.valid_until):
            raise Denied("Scope or authorization not currently valid")


class Action(Contract):
    finding_id: str = Field(min_length=1, max_length=100)
    host: str
    description: str = Field(min_length=1, max_length=2000)
    expected_effect: str = Field(min_length=1, max_length=2000)
    stop_condition: str = Field(min_length=1, max_length=1000)
    seconds: int = Field(default=300, ge=1, le=1800)

    _host = field_validator("host")(hostname)


class ActionPlan(Contract):
    schema_version: int = 1
    scope_version: int = Field(ge=1)
    actions: list[Action] = Field(min_length=1, max_length=20)
    expires_at: int

    @model_validator(mode="after")
    def valid(self) -> "ActionPlan":
        if self.schema_version != 1 or len({v.finding_id for v in self.actions}) != len(self.actions):
            raise ValueError("Unsupported action schema or duplicate finding")
        return self
