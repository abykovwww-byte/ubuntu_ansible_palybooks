"""Host-owned per-job policy. This contract is never an MCP tool argument."""

from __future__ import annotations

import ipaddress

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, Denied, Scope, digest, hostname


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
