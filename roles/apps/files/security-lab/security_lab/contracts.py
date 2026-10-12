"""Canonical, strict engagement contracts; no model-authored approval fields."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer, model_validator


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


_PRIVATE_V4 = tuple(ipaddress.ip_network(value) for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
_PRIVATE_V6 = ipaddress.ip_network("fc00::/7")


def is_private_network_address(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address in _PRIVATE_V6 if address.version == 6 else any(address in network for network in _PRIVATE_V4)


def network_address(value: str) -> str:
    """One unicast public/RFC1918/ULA IP; permission is checked separately."""
    if type(value) is not str or "%" in value:
        raise ValueError("Expected an exact IP address without a zone identifier")
    address = ipaddress.ip_address(value)
    if (address.is_loopback or address.is_link_local or address.is_unspecified
            or address.is_multicast or address.is_reserved):
        raise ValueError("Unsupported special-purpose network address")
    if not address.is_global and not is_private_network_address(str(address)):
        raise ValueError("Only public, RFC1918 or ULA unicast network addresses are supported")
    return str(address)


class NetworkTarget(Contract):
    """Explicit TCP scan permission, independent of HTTP services and paths."""

    host: str
    tcp_ports: list[int] = Field(min_length=1, max_length=1024)
    addresses: list[str] = Field(default_factory=list, max_length=64)

    _host = field_validator("host")(hostname)

    @field_validator("addresses")
    @classmethod
    def exact_address_grants(cls, values: list[str]) -> list[str]:
        return sorted(set(network_address(value) for value in values))

    @model_serializer(mode="wrap")
    def compatible_network_permission(self, handler):
        data = handler(self)
        if not self.addresses:
            data.pop("addresses", None)
        return data

    @field_validator("tcp_ports")
    @classmethod
    def explicit_tcp_ports(cls, values: list[int]) -> list[int]:
        if any(type(value) is not int or not 1 <= value <= 65535 for value in values):
            raise ValueError("Expected explicit integer TCP ports")
        return sorted(set(values))


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
    targets: list[Target] = Field(default_factory=list, max_length=2000)
    network_targets: list[NetworkTarget] = Field(default_factory=list, max_length=2000)
    authorization: Authorization
    expires_at: int
    allowed_actions: list[str] = Field(default_factory=lambda: ["checks"])
    budget: Budget = Field(default_factory=Budget)

    @model_serializer(mode="wrap")
    def compatible_manifest(self, handler):
        data = handler(self)
        # This additive contract preserves canonical hashes of stored v1 HTTP
        # manifests. Never rewrite historical manifests simply to add an empty
        # network permission; nonempty network targets participate in the hash.
        if not self.network_targets:
            data.pop("network_targets", None)
        return data

    @model_validator(mode="after")
    def valid(self) -> "Scope":
        if self.schema_version != 1 or self.allowed_actions != ["checks"]:
            raise ValueError("First scope approves checks only")
        if "checks" not in self.authorization.actions or self.expires_at > self.authorization.valid_until:
            raise ValueError("Scope exceeds authorization")
        if not self.targets and not self.network_targets:
            raise ValueError("Scope requires an explicit HTTP or network target")
        if any(t.host not in self.authorization.hosts for t in [*self.targets, *self.network_targets]):
            raise ValueError("Target missing from authorization")
        if len({(t.host, t.scheme, t.port) for t in self.targets}) != len(self.targets):
            raise ValueError("Duplicate service targets must be merged explicitly")
        if len({t.host for t in self.network_targets}) != len(self.network_targets):
            raise ValueError("Duplicate network targets must be merged explicitly")
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
