"""Host-owned per-job policy. This contract is never an MCP tool argument."""

from __future__ import annotations

import ipaddress
import re
import time
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, Denied, Scope, digest, hostname, safe_path


class Upstream(Contract):
    host: str
    port: int = Field(ge=1, le=65535)
    addresses: list[str] = Field(min_length=1, max_length=16)

    _host = field_validator("host")(hostname)

    @field_validator("addresses")
    @classmethod
    def addresses_valid(cls, values: list[str]) -> list[str]:
        return sorted(set(str(ipaddress.ip_address(v)) for v in values))


class HTTPJobPolicy(Contract):
    schema_version: int = 1
    job_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    generation: int = Field(ge=1)
    scope_hash: str
    scope: Scope
    upstreams: list[Upstream] = Field(min_length=1, max_length=2000)
    worker_addresses: list[str] = Field(min_length=1, max_length=2)
    # Only the trusted lab launcher may set exact private fixture addresses.
    # Loopback, link-local, metadata and unspecified addresses are never allowed.
    lab_private_addresses: list[str] = Field(default_factory=list, max_length=16)
    deadline: float
    max_requests: int = Field(default=500, ge=1, le=10000)

    @model_validator(mode="after")
    def valid(self) -> "HTTPJobPolicy":
        if self.schema_version != 1 or self.scope_hash != digest(self.scope.model_dump(mode="json")):
            raise ValueError("Policy does not match canonical scope")
        services = {(t.host, t.port) for t in self.scope.targets}
        if {(u.host, u.port) for u in self.upstreams} != services or len(self.upstreams) != len(services):
            raise ValueError("Exactly one pinned upstream is required per approved service")
        if self.deadline > min(self.scope.expires_at, self.scope.authorization.valid_until):
            raise ValueError("Job deadline exceeds scope/authorization")
        for value in self.worker_addresses + self.lab_private_addresses:
            ip = ipaddress.ip_address(value)
            if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                raise ValueError("Unsafe worker/lab address")
        for upstream in self.upstreams:
            for address in upstream.addresses:
                ip = ipaddress.ip_address(address)
                if not ip.is_global and (not ip.is_private or address not in self.lab_private_addresses):
                    raise ValueError("Non-public upstream requires exact private lab authorization")
                if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                    raise ValueError("Unsafe upstream address")
        if set(self.worker_addresses) & {a for u in self.upstreams for a in u.addresses}:
            raise ValueError("Worker cannot also be an upstream")
        return self

    def check(self, now: float) -> None:
        self.scope.check_window(now)
        if now >= self.deadline:
            raise Denied("Job deadline expired")

    def upstream(self, host: str, port: int) -> Upstream:
        host = hostname(host)
        for u in self.upstreams:
            if (u.host, u.port) == (host, port):
                return u
        raise Denied("Upstream outside approved services")

    def pin(self, host: str, port: int, resolved: list[str]) -> str:
        u = self.upstream(host, port)
        answers = {str(ipaddress.ip_address(v)) for v in resolved}
        if not answers or not answers <= set(u.addresses):
            raise Denied("DNS changed outside pinned addresses; review required")
        return sorted(answers)[0]


class Lease(Contract):
    job_id: str
    generation: int
    scope_hash: str
    sequence: int = Field(ge=1)
    valid_until: float
    revoked: bool = False


class LeaseClock:
    """Bound wall-clock leases to monotonic time; rollback cannot extend access."""

    def __init__(self, policy: HTTPJobPolicy, *, clock=time.time, monotonic=time.monotonic):
        self.policy, self.clock, self.monotonic = policy, clock, monotonic
        self.sequence, self.valid_until, self.monotonic_until = 0, 0.0, 0.0
        self.revoked = False

    def update(self, lease: Lease) -> None:
        if self.revoked or (lease.job_id, lease.generation, lease.scope_hash) != (
            self.policy.job_id, self.policy.generation, self.policy.scope_hash
        ) or lease.sequence <= self.sequence:
            raise Denied("Lease identity/sequence changed or revoked")
        now = self.clock()
        remaining = lease.valid_until - now
        if not lease.revoked and not 0 < remaining <= 30:
            raise Denied("Lease must expire within thirty seconds")
        self.policy.check(now)
        self.sequence, self.valid_until = lease.sequence, min(lease.valid_until, self.policy.deadline)
        self.monotonic_until = self.monotonic() + min(remaining, self.policy.deadline - now)
        self.revoked = lease.revoked

    def check(self) -> None:
        self.policy.check(self.clock())
        if self.revoked or self.clock() >= self.valid_until or self.monotonic() >= self.monotonic_until:
            raise Denied("Lease revoked/expired")


def authority(value: str, default_port: int) -> tuple[str, int]:
    if not value or any(c in value for c in "/\\@?#% \t\r\n"):
        raise Denied("Ambiguous service authority")
    p = urlsplit("//" + value)
    if not p.hostname or p.username or p.password or p.path:
        raise Denied("Invalid authority")
    return hostname(p.hostname), p.port or default_port


def request_permitted(policy: HTTPJobPolicy, *, scheme: str, host: str, port: int,
                      method: str, raw_path: bytes, host_headers: list[str],
                      http_authority: str = "", connect: tuple[str, int] | None = None,
                      sni: str | None = None) -> None:
    """Check every identity before an upstream request, including HTTP/2 authority."""
    try:
        host = hostname(host)
        if scheme not in {"http", "https"} or not 1 <= port <= 65535:
            raise Denied("Unsupported scheme/port")
        default = 443 if scheme == "https" else 80
        if len(host_headers) > 1 or (not host_headers and not http_authority):
            raise Denied("Missing or duplicate Host/authority")
        for value in host_headers + ([http_authority] if http_authority else []):
            if authority(value, default) != (host, port):
                raise Denied("Host/authority mismatch")
        if scheme == "https":
            if connect != (host, port) or not sni or hostname(sni) != host:
                raise Denied("CONNECT/SNI/request service mismatch")
        elif connect is not None or sni is not None:
            raise Denied("Plain HTTP inside a TLS tunnel")
        path = raw_path.decode("ascii", errors="strict")
        # Reject path parameters and whitespace/control interpretations as well
        # as encodings/dot-segments; query remains query, never a scope path.
        if re.search(r"[\x00-\x20\x7f]", path) or "#" in path or ";" in path.partition("?")[0]:
            raise Denied("Ambiguous raw request path")
        safe_path(path.partition("?")[0])
        url = f"{scheme}://{host}:{port}{path}"
        if not any(t.permits(url, method) for t in policy.scope.targets):
            raise Denied("Request outside path/method scope")
    except (ValueError, UnicodeError) as exc:
        raise Denied(str(exc)) from exc
