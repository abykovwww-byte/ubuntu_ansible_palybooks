"""Host-only complete DNS snapshots for immutable HTTP destination policies.

There is intentionally no default network resolver or worker activation. A
protected launcher injects an async resolver and retains one TargetResolver per
event loop. All addresses must pass before a snapshot is returned. Revalidation
compares complete sets; any change closes permission instead of expanding pins.
TTL is unknown because address-only resolver output cannot prove a DNS TTL.
Timeouts do not wait for an uncooperative resolver to acknowledge cancellation:
its result is discarded and it retains one of the bounded lookup slots until
done. Production requires a killable resolver worker for process-lifetime proof.
"""
from __future__ import annotations

import asyncio
import ipaddress
import time
from typing import Awaitable, Callable, Literal

from pydantic import Field, field_validator, model_validator

from .contracts import Contract, Denied, Scope, digest, hostname
from .http_policy import Upstream

Resolver = Callable[[str, int], Awaitable[list[str] | tuple[str, ...]]]
LAB_REFERENCE = 'lab:isolated-docker-fixture'
LAB_HOSTS = {'fixture.example.test', 'fixture6.example.test'}


def _address(value: str) -> str:
    if not isinstance(value, str) or '%' in value:
        raise ValueError('Expected an unambiguous literal IP address')
    return str(ipaddress.ip_address(value))


def _unsafe(value: str) -> bool:
    address = ipaddress.ip_address(value)
    return address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast or address.is_reserved


class ResolvedService(Contract):
    host: str
    port: int = Field(ge=1, le=65535)
    addresses: tuple[str, ...] = Field(min_length=1, max_length=16)
    _host = field_validator('host')(hostname)

    @field_validator('addresses')
    @classmethod
    def canonical_addresses(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({_address(value) for value in values}))


class ResolutionSnapshot(Contract):
    schema_version: Literal[1] = 1
    scope_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    resolved_at: float = Field(ge=0, allow_inf_nan=False)
    upstreams: tuple[ResolvedService, ...] = Field(min_length=1, max_length=2000)
    snapshot_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    ttl_seconds: None = None

    @model_validator(mode='after')
    def canonical_and_bound(self):
        services = [(item.host, item.port) for item in self.upstreams]
        if services != sorted(set(services)):
            raise ValueError('Snapshot services must be canonical and unique')
        if self.snapshot_hash != digest(self.model_dump(mode='json', exclude={'snapshot_hash'})):
            raise ValueError('Resolution snapshot hash mismatch')
        return self

    def policy_upstreams(self) -> list[Upstream]:
        """Copy immutable pins into the existing host-owned policy contract."""
        try:
            saved = ResolutionSnapshot.model_validate_json(self.model_dump_json())
        except ValueError:
            raise Denied('Resolution snapshot integrity failure') from None
        return [Upstream(host=item.host, port=item.port, addresses=list(item.addresses)) for item in saved.upstreams]


class TargetResolver:
    def __init__(self, resolver: Resolver, *, timeout_seconds: float = 3, max_concurrency: int = 2,
                 clock=time.time, monotonic=time.monotonic, lab_private_addresses: tuple[str, ...] = ()):
        if not callable(resolver) or not 0 < timeout_seconds <= 10 or type(max_concurrency) is not int or not 1 <= max_concurrency <= 4:
            raise ValueError('A bounded host-owned async resolver configuration is required')
        self.resolver, self.timeout_seconds = resolver, float(timeout_seconds)
        self.clock, self.monotonic = clock, monotonic
        self.slots = asyncio.BoundedSemaphore(max_concurrency)
        self.pending: set[asyncio.Task] = set()
        self.lab_private_addresses = frozenset(_address(value) for value in lab_private_addresses)
        if any(_unsafe(value) or not ipaddress.ip_address(value).is_private or ipaddress.ip_address(value).is_global
               for value in self.lab_private_addresses):
            raise ValueError('Lab exceptions must be exact safe private fixture addresses')

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    def _remaining(self, deadline):
        remaining = deadline - self.monotonic()
        if remaining <= 0:
            raise Denied('Target DNS timeout')
        return remaining

    def _validate_answers(self, scope: Scope, host: str, values) -> tuple[str, ...]:
        if not isinstance(values, (tuple, list)) or not values or len(values) > 16:
            raise Denied('Target DNS empty, malformed or over answer budget')
        try:
            addresses = tuple(sorted({_address(value) for value in values}))
        except (ValueError, TypeError):
            raise Denied('Target DNS invalid literal address') from None
        lab = scope.authorization.reference == LAB_REFERENCE and host in LAB_HOSTS
        for value in addresses:
            address = ipaddress.ip_address(value)
            if _unsafe(value) or (not address.is_global and not (
                    lab and address.is_private and value in self.lab_private_addresses)):
                raise Denied('Target DNS includes an unapproved nonpublic or unsafe address')
        return addresses

    async def _lookup(self, scope: Scope, host: str, port: int, deadline: float) -> ResolvedService:
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=self._remaining(deadline))
        except TimeoutError:
            raise Denied('Target DNS timeout') from None
        async def invoke():
            return await self.resolver(host, port)
        task = asyncio.create_task(invoke())
        self.pending.add(task)
        def finished(done):
            self.pending.discard(done)
            self.slots.release()
            self._consume(done)
        task.add_done_callback(finished)
        try:
            done, _ = await asyncio.wait({task}, timeout=self._remaining(deadline))
            if not done:
                task.cancel()
                raise Denied('Target DNS timeout')
            try:
                values = task.result()
            except asyncio.CancelledError:
                raise Denied('Target DNS lookup cancelled') from None
            except Exception:
                raise Denied('Target DNS lookup failed') from None
            self._remaining(deadline)
            return ResolvedService(host=host, port=port, addresses=self._validate_answers(scope, host, values))
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    async def resolve(self, scope: Scope) -> ResolutionSnapshot:
        """Whole-scope snapshot or Denied; never return a successful partial set."""
        if not isinstance(scope, Scope):
            raise Denied('Canonical HTTP Scope required')
        scope.check_window(self.clock())
        scope_hash = digest(scope.model_dump(mode='json'))
        services = sorted({(target.host, target.port) for target in scope.targets})
        deadline = self.monotonic() + self.timeout_seconds
        tasks = [asyncio.create_task(self._lookup(scope, host, port, deadline)) for host, port in services]
        try:
            answers = await asyncio.gather(*tasks)
            self._remaining(deadline)
            observed = float(self.clock())
            scope.check_window(observed)
            if digest(scope.model_dump(mode='json')) != scope_hash:
                raise Denied('Scope changed during target resolution')
            payload = {'schema_version': 1, 'scope_hash': scope_hash, 'resolved_at': observed,
                       'upstreams': [item.model_dump(mode='json') for item in answers], 'ttl_seconds': None}
            return ResolutionSnapshot(scope_hash=scope_hash, resolved_at=observed,
                                      upstreams=tuple(answers), snapshot_hash=digest(payload))
        except BaseException:
            for task in tasks:
                if not task.done():
                    task.cancel()
                task.add_done_callback(self._consume)
            raise

    async def revalidate(self, scope: Scope, snapshot: ResolutionSnapshot) -> ResolutionSnapshot:
        """Fresh complete answer sets must equal existing pins; otherwise deny."""
        if not isinstance(snapshot, ResolutionSnapshot):
            raise Denied('Host-owned resolution snapshot required')
        try:
            saved = ResolutionSnapshot.model_validate_json(snapshot.model_dump_json())
        except ValueError:
            raise Denied('Resolution snapshot integrity failure') from None
        if saved.scope_hash != digest(scope.model_dump(mode='json')):
            raise Denied('Resolution snapshot belongs to a different scope')
        expected = {(target.host, target.port) for target in scope.targets}
        if {(item.host, item.port) for item in saved.upstreams} != expected:
            raise Denied('Resolution snapshot does not cover the complete scope')
        if saved.resolved_at > self.clock():
            raise Denied('Resolution clock rollback or future snapshot')
        fresh = await self.resolve(scope)
        if fresh.upstreams != saved.upstreams:
            raise Denied('Target DNS answer set changed; stop job and reassess policy')
        return fresh
