"""Canonical, strict engagement contracts; no model-authored approval fields."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_serializer, model_validator


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


INITIAL_CASE_SECONDS = 7200
MAX_CASE_SECONDS = 86400


class Budget(Contract):
    # Higher cumulative limits require the Controller's explicit extension path.
    seconds: int = Field(default=INITIAL_CASE_SECONDS, ge=1, le=MAX_CASE_SECONDS)
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


class HTTPRequest(Contract):
    """One explicit request; payload/parameters are data, never CLI options."""
    tool: Literal['http'] = 'http'
    url: str = Field(min_length=1, max_length=2000)
    method: Literal['GET', 'HEAD', 'OPTIONS', 'POST'] = 'GET'
    query: dict[str, str] = Field(default_factory=dict, max_length=20)
    headers: dict[str, str] = Field(default_factory=dict, max_length=8)
    body: str | None = Field(default=None, max_length=8192)
    seconds: int = Field(default=15, ge=1, le=60)

    @field_validator('url')
    @classmethod
    def canonical_http_url(cls, value):
        if any(ord(c) <= 32 or ord(c) >= 127 for c in value) or any(c in value for c in '\\%?#'):
            raise ValueError('Use an unambiguous HTTP URL; pass query separately')
        parsed = urlsplit(value)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('HTTP URL without credentials required')
        host = hostname(parsed.hostname)
        port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
        if not 1 <= port <= 65535:
            raise ValueError('Invalid HTTP port')
        return f'{parsed.scheme}://{host}:{port}{safe_path(parsed.path or "/")}'

    @field_validator('headers')
    @classmethod
    def public_headers(cls, value):
        allowed = {'accept', 'content-type', 'origin', 'x-requested-with', 'accept-language'}
        result = {}
        for key, item in value.items():
            key = key.lower()
            if key not in allowed or key in result or len(item) > 1000 or any(ord(c) < 32 or ord(c) == 127 for c in item):
                raise ValueError('Unsupported, duplicate or invalid public HTTP header')
            result[key] = item
        return dict(sorted(result.items()))

    @field_validator('query')
    @classmethod
    def bounded_parameters(cls, value):
        if any(not key or len(key) > 100 or len(item) > 1000 or
               any(ord(c) < 32 or ord(c) == 127 for c in key + item) for key, item in value.items()):
            raise ValueError('Invalid or oversized query parameter')
        return dict(sorted(value.items()))

    @model_validator(mode='after')
    def body_method(self):
        if self.body is not None and self.method != 'POST':
            raise ValueError('A request body requires POST')
        if self.body is not None and '\x00' in self.body:
            raise ValueError('NUL in HTTP body is unsupported')
        return self


class InteractiveAction(Contract):
    """A saved action, never a checks command or an arbitrary browser script."""
    url: str = Field(min_length=1, max_length=2000)
    seconds: int = Field(default=60, ge=15, le=180)
    session_ref: str | None = Field(default=None, pattern=r'^[a-z][a-z0-9_-]{0,63}$')
    _url = field_validator('url')(HTTPRequest.canonical_http_url.__func__)


class SocketMessage(Contract):
    text: str = Field(max_length=8192)
    expect_contains: str | None = Field(default=None, min_length=1, max_length=200)


class WebSocketAction(InteractiveAction):
    tool: Literal['websocket'] = 'websocket'
    messages: list[SocketMessage] = Field(default_factory=list, max_length=16)
    max_frames: int = Field(default=64, ge=1, le=128)
    max_bytes: int = Field(default=65536, ge=1, le=262144)
    message_seconds: int = Field(default=5, ge=1, le=15)
    listen_seconds: int = Field(default=1, ge=0, le=10)

    @field_validator('url',mode='before')
    @classmethod
    def websocket_url(cls, value):
        if isinstance(value,str):
            if value.lower().startswith('wss://'): return 'https://'+value[6:]
            if value.lower().startswith('ws://'): return 'http://'+value[5:]
        return value


class SocketEvent(Contract):
    name: str = Field(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_.:-]+$')
    data: JsonValue = Field(default_factory=dict)
    expect_ack: bool = True
    ack_contains: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def bounded(self):
        if self.name in {'connect', 'disconnect', 'connect_error', '*'}:
            raise ValueError('Reserved Socket.IO event')
        if len(canonical(self.data).encode()) > 8192 or (self.ack_contains and not self.expect_ack):
            raise ValueError('Oversized event or ack predicate without acknowledgement')
        return self


class SocketIOAction(InteractiveAction):
    tool: Literal['socketio'] = 'socketio'
    namespace: str = Field(default='/', pattern=r'^/(?:[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*)?$')
    transport: Literal['websocket', 'polling', 'polling_upgrade'] = 'websocket'
    events: list[SocketEvent] = Field(default_factory=list, max_length=16)
    max_frames: int = Field(default=64, ge=8, le=128)
    max_bytes: int = Field(default=65536, ge=1, le=262144)
    max_requests: int = Field(default=32, ge=1, le=64)
    message_seconds: int = Field(default=5, ge=1, le=15)
    listen_seconds: int = Field(default=1, ge=0, le=10)


class UploadPart(Contract):
    field: str = Field(pattern=r'^[A-Za-z][A-Za-z0-9_-]{0,63}$')
    filename: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$')
    content_base64: str = Field(max_length=32768)
    content_type: str = Field(default='application/octet-stream', pattern=r'^[a-z0-9.+-]+/[a-z0-9.+-]+$')

    @field_validator('content_base64')
    @classmethod
    def inline_bytes(cls, value):
        import base64
        base64.b64decode(value, validate=True)
        return value


class WriteStep(Contract):
    name: str = Field(pattern=r'^[a-z][a-z0-9_-]{0,39}$')
    method: Literal['GET', 'HEAD', 'OPTIONS', 'POST', 'PUT', 'PATCH', 'DELETE']
    path: str = Field(min_length=1, max_length=1000)
    query: dict[str, str] = Field(default_factory=dict, max_length=20)
    headers: dict[str, str] = Field(default_factory=dict, max_length=8)
    json_body: JsonValue | None = None
    form: dict[str, str] = Field(default_factory=dict, max_length=20)
    files: list[UploadPart] = Field(default_factory=list, max_length=2)
    capture: dict[str, str] = Field(default_factory=dict, max_length=8)
    expect_status: int | None = Field(default=None, ge=100, le=599)

    @field_validator('path')
    @classmethod
    def literal_or_bound_path(cls, value):
        safe_path(value)
        for part in value.split('/'):
            if ('{' in part or '}' in part) and not re.fullmatch(r'\{[a-z][a-z0-9_]{0,39}\}', part):
                raise ValueError('A binding must occupy one complete path segment')
        return value

    @field_validator('headers')
    @classmethod
    def public_and_csrf_headers(cls, values):
        if len({k.lower() for k in values}) != len(values):
            raise ValueError('Duplicate case-insensitive header')
        csrf = {k.lower(): v for k, v in values.items() if k.lower() in {'x-csrf-token', 'x-xsrf-token'}}
        public = HTTPRequest.public_headers.__func__(cls, {k:v for k,v in values.items() if k.lower() not in csrf})
        if any(len(v)>1000 or any(ord(c)<32 or ord(c)==127 for c in v) for v in csrf.values()):
            raise ValueError('Invalid CSRF header')
        return {**public, **csrf}

    _query = field_validator('query', 'form')(HTTPRequest.bounded_parameters.__func__)

    @model_validator(mode='after')
    def bounded_step(self):
        if self.json_body is not None and (self.form or self.files):
            raise ValueError('Choose JSON or form/multipart')
        if self.method in {'GET', 'HEAD', 'OPTIONS', 'DELETE'} and (self.json_body is not None or self.form or self.files):
            raise ValueError('Bodies require POST, PUT or PATCH')
        if len(canonical(self.model_dump(mode='json')).encode()) > 65536:
            raise ValueError('Request exceeds the body budget')
        if any(not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', key) or not value.startswith('/')
               or len(value)>200 for key,value in self.capture.items()):
            raise ValueError('Capture requires a variable name and JSON pointer')
        return self


class WritesAction(InteractiveAction):
    tool: Literal['writes'] = 'writes'
    steps: list[WriteStep] = Field(min_length=1, max_length=12)
    cleanup: list[WriteStep] = Field(default_factory=list, max_length=8)
    cleanup_required: bool = True
    cleanup_seconds: int = Field(default=5, ge=1, le=8)

    @model_validator(mode='after')
    def workflow(self):
        if urlsplit(self.url).path != '/':
            raise ValueError('Workflow url is the origin root; steps declare exact paths')
        if self.cleanup_required and not self.cleanup:
            raise ValueError('Declare cleanup or explicitly set cleanup_required=false')
        if len({s.name for s in [*self.steps,*self.cleanup]}) != len(self.steps)+len(self.cleanup):
            raise ValueError('Step names must be unique')
        return self


class ResponseExpectation(Contract):
    status: int | None = Field(default=None, ge=100, le=599)
    contains: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode='after')
    def nonempty(self):
        if self.status is None and self.contains is None:
            raise ValueError('An observable response predicate is required')
        return self


class Action(Contract):
    finding_id: str = Field(min_length=1, max_length=100)
    host: str
    description: str = Field(min_length=1, max_length=2000)
    expected_effect: str = Field(min_length=1, max_length=2000)
    stop_condition: str = Field(min_length=1, max_length=1000)
    seconds: int = Field(default=300, ge=1, le=1800)
    request: Annotated[Union[HTTPRequest, WebSocketAction, SocketIOAction, WritesAction], Field(discriminator='tool')] | None = None
    verification: ResponseExpectation | None = None

    _host = field_validator("host")(hostname)

    @model_serializer(mode='wrap')
    def preserve_historical_hash(self, handler):
        data = handler(self)
        for key in ('request', 'verification'):
            if data[key] is None:
                data.pop(key)
        return data

    @model_validator(mode='after')
    def request_matches_action(self):
        if self.request is not None:
            if urlsplit(self.request.url).hostname != self.host or self.request.seconds > self.seconds:
                raise ValueError('Action request exceeds its host or time budget')
        if self.verification is not None and self.request is None:
            raise ValueError('Verification needs an executable action request')
        if self.verification is not None and not isinstance(self.request, HTTPRequest):
            raise ValueError('Interactive actions declare their own message/step predicates')
        return self


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
