"""Bounded credential-free public-source HTTPS; no target acquisition or redirects.

This local collector is not the production worker/egress launcher. It accepts
only program-built source requests, resolves each provider once, rejects every
nonpublic answer and connects to that pinned address with verified provider SNI.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from urllib.parse import parse_qs, urlsplit

from .contracts import Denied, hostname
from .discovery_contracts import SourceRequest, SourceResult

PROVIDERS = {
    'crtsh': ('crt.sh', '/'),
    'wayback': ('web.archive.org', '/cdx/search/cdx'),
    'github_repositories': ('api.github.com', '/search/repositories'),
    'dns': ('dns.google', '/resolve'),
    'rdap_bootstrap': ('data.iana.org', '/rdap/dns.json'),
    'ripestat': ('stat.ripe.net', '/data/network-info/data.json'),
    'internetdb': ('internetdb.shodan.io', None),
}

# Native getaddrinfo cannot be interrupted safely in a Python thread. Slots
# bound both running lookups and queue growth; timed-out lookups never connect.
# Production must isolate acquisition in killable workers to bound DNS process
# lifetime as well (Python may wait for lingering pool threads at shutdown).
_DNS_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='public-osint-dns')
_DNS_SLOTS = threading.BoundedSemaphore(2)
_MARKER = '[REDACTED_CREDENTIAL]'
_SECRET_KEYS = {'apikey', 'accesstoken', 'password', 'clientsecret', 'token',
                'authorization', 'cookie', 'secret', 'privatekey', 'credential'}
_KNOWN_TOKEN = re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9_]{20,255}|github_pat_[A-Za-z0-9_]{20,255}|sk-[A-Za-z0-9_-]{20,255}|AKIA[A-Z0-9]{16})\b')


def _redact_string(value: str) -> str:
    value = _KNOWN_TOKEN.sub(_MARKER, value)
    value = re.sub(r'(?i)(https?://)[^/\s\"\'<>]+@', r'\1[REDACTED_CREDENTIAL]@', value)
    value = re.sub(r'(?i)([?&](?:api[_-]?key|access[_-]?token|password|client[_-]?secret|token|secret)=)[^&#\s\"\'<>]*',
                   r'\1[REDACTED_CREDENTIAL]', value)
    return value


def _hidden(value):
    if isinstance(value, dict):
        return {key: _hidden(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_hidden(item) for item in value]
    return _MARKER


def _redact_json(value):
    if isinstance(value, dict):
        return {key: (_hidden(item) if re.sub(r'[-_\s]', '', key).lower() in _SECRET_KEYS else _redact_json(item))
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    return _redact_string(value) if isinstance(value, str) else value


def body_hash(body: str) -> str:
    return hashlib.sha256(body.encode('utf-8')).hexdigest()


def redact_public_text(body: str) -> str:
    # Redact complete structured secret values (including spaces/containers),
    # plus known formats and URL credentials embedded in public text. Arbitrary
    # unlabeled secrets still require a dedicated scanner; no such claim here.
    try:
        return json.dumps(_redact_json(json.loads(body)), ensure_ascii=False, separators=(',', ':'))
    except (ValueError, TypeError, RecursionError):
        body = _redact_string(body)
        body = re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|password|client[_-]?secret|authorization|cookie)["']?\s*[:=]\s*)(["'])(?:\\.|(?!\2)[^\\])*?\2''',
                      lambda match: match[1] + match[2] + _MARKER + match[2], body)
        return re.sub(r'''(?i)(["']?(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)["']?\s*[:=]\s*)[^"'\s,}]+''',
                      r'\1[REDACTED_CREDENTIAL]', body)


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, timeout: float):
        super().__init__(host, port=443, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, self.port), timeout=self.timeout)
        self.sock = raw  # Expose the socket to the deadline watchdog immediately.
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock.do_handshake()
        except BaseException:
            self.close()
            raw.close()
            raise


class PublicSourceClient:
    def __init__(self, *, timeout: float = 8, max_bytes: int = 1_000_000,
                 resolver=socket.getaddrinfo, connection_factory=PinnedHTTPS, clock=time.time):
        self.timeout, self.max_bytes = timeout, max_bytes
        self.resolver, self.connection_factory, self.clock = resolver, connection_factory, clock
        self._registry_requests: set[str] = set()

    def authorize_registry(self, request: SourceRequest) -> None:
        """Internal orchestrator: selected serving HTTPS URL from IANA bootstrap."""
        p = self._url(request.url)
        if request.source != 'rdap_domain' or not p.path.endswith('/domain/' + hostname(request.subject)):
            raise Denied('Invalid serving RDAP URL')
        if p.query:
            raise Denied('Registry query parameters are unsupported')
        self._registry_requests.add(request.url)

    @staticmethod
    def _url(url):
        p = urlsplit(url)
        if (p.scheme != 'https' or not p.hostname or p.username or p.password
                or p.fragment or p.port not in {None, 443} or '\\' in url
                or any(c in url for c in '\r\n\x00') or '%' in p.path
                or any(segment in {'.', '..'} for segment in p.path.split('/'))):
            raise Denied('Expected an unambiguous HTTPS source URL')
        hostname(p.hostname)
        return p

    def validate_request(self, request: SourceRequest):
        p = self._url(request.url)
        if request.source == 'rdap_domain':
            if request.url not in self._registry_requests:
                raise Denied('Registry URL not selected by the bootstrap')
            return p
        if request.source not in PROVIDERS:
            raise Denied('Source is outside the public-source catalog')
        host, path = PROVIDERS[request.source]
        if p.hostname != host or (path is not None and p.path != path):
            raise Denied('Request is outside the source endpoint')
        if request.source == 'internetdb':
            address = ipaddress.ip_address(request.subject)
            if not address.is_global or p.path != '/' + str(address) or p.query:
                raise Denied('InternetDB requires an observed public IP')
        elif request.source == 'ripestat':
            address = ipaddress.ip_address(request.subject)
            if not address.is_global or parse_qs(p.query) != {'resource': [str(address)]}:
                raise Denied('RIPEstat requires an observed public IP')
        elif request.source == 'dns':
            query = parse_qs(p.query)
            if (query.get('name') != [hostname(request.subject)]
                    or query.get('type', [''])[0] not in {'A', 'AAAA', 'CNAME', 'MX', 'NS', 'TXT', 'SOA'}
                    or len(query.get('type', [])) != 1 or set(query) - {'name', 'type', 'do', 'cd', 'edns_client_subnet'}
                    or query.get('edns_client_subnet', ['0.0.0.0/0']) != ['0.0.0.0/0']):
                raise Denied('Unsupported DNS query')
        elif request.source == 'crtsh':
            if parse_qs(p.query) != {'q': ['%.' + hostname(request.subject)], 'output': ['json']}:
                raise Denied('Unsupported certificate query')
        elif request.source == 'github_repositories':
            if parse_qs(p.query) != {'q': ['"' + hostname(request.subject) + '" in:name,description,readme'],
                                      'per_page': ['100'], 'page': ['1']}:
                raise Denied('Unsupported repository query')
        elif request.source == 'wayback':
            if parse_qs(p.query) != {'url': ['*.' + hostname(request.subject) + '/*'], 'output': ['json'],
                    'fl': ['original,timestamp,statuscode'], 'collapse': ['urlkey'], 'limit': ['200']}:
                raise Denied('Unsupported historical URL query')
        elif request.source == 'rdap_bootstrap' and p.query:
            raise Denied('Bootstrap does not accept query parameters')
        return p

    def fetch(self, request: SourceRequest, *, active=lambda: True, timeout_seconds=None) -> SourceResult:
        started = float(self.clock())
        common = dict(request=request, collected_at=started)
        duration = self.timeout if timeout_seconds is None else min(self.timeout, float(timeout_seconds))
        deadline = time.monotonic() + max(0, duration)
        expired = threading.Event()

        def remaining():
            value = deadline - time.monotonic()
            if value <= 0 or expired.is_set():
                raise TimeoutError()
            return value

        def outcome(status, reason, *, body='', code=None, truncated=False):
            if expired.is_set() or time.monotonic() >= deadline:
                status, reason, body, truncated = 'error', 'source_timeout', '', False
            sanitized = redact_public_text(body)
            if len(sanitized.encode('utf-8')) > self.max_bytes:
                # Replacing very short secrets can enlarge otherwise bounded
                # JSON. Never slice it into a malformed/leaky evidence body.
                status, reason, sanitized, truncated = 'partial', 'sanitization_byte_budget_exhausted', '', True
            if expired.is_set() or time.monotonic() >= deadline:
                status, reason, sanitized, truncated = 'error', 'source_timeout', '', False
            return SourceResult(**common, status=status, reason=reason, body=sanitized,
                                body_hash=body_hash(sanitized), http_status=code, truncated=truncated)

        connection = None
        timer = None
        try:
            p = self.validate_request(request)
            if not active():
                return outcome('not_attempted', 'case_stopped')
            if not _DNS_SLOTS.acquire(timeout=remaining()):
                raise TimeoutError()
            try:
                future = _DNS_POOL.submit(self.resolver, p.hostname, 443, type=socket.SOCK_STREAM)
            except BaseException:
                _DNS_SLOTS.release()
                raise
            future.add_done_callback(lambda _completed: _DNS_SLOTS.release())
            try:
                answers = future.result(timeout=remaining())
            except FutureTimeout:
                future.cancel()
                raise TimeoutError() from None
            addresses = sorted({str(ipaddress.ip_address(a[4][0])) for a in answers})
            if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                raise Denied('Source DNS includes a nonpublic or invalid address')
            if not active():
                return outcome('not_attempted', 'case_stopped')
            connection = self.connection_factory(p.hostname, addresses[0], remaining())

            def interrupt_socket():
                expired.set()
                current = getattr(connection, 'sock', None)
                if current is not None:
                    try:
                        current.shutdown(socket.SHUT_RDWR)
                    except (OSError, AttributeError):
                        pass
                    try:
                        current.close()
                    except OSError:
                        pass

            timer = threading.Timer(remaining(), interrupt_socket)
            timer.daemon = True
            timer.start()
            connection.request('GET', p.path + ('?' + p.query if p.query else ''), headers={
                'User-Agent': 'SecurityLab-PublicOSINT/0.1 (+credential-free)',
                'Accept': 'application/json', 'Accept-Encoding': 'identity',
            })
            response = connection.getresponse()
            code = response.status
            # No redirects, cookies, authorization or automatic retry. Status
            # error bodies may contain credentials/HTML: do not retain them.
            if 300 <= code < 400:
                return outcome('blocked', 'source_redirect_not_followed', code=code)
            if code == 429:
                return outcome('rate_limited', 'source_quota_exhausted', code=code)
            if code in {401, 403}:
                return outcome('blocked', 'source_access_blocked', code=code)
            if code == 404 and request.source == 'internetdb':
                return outcome('empty', 'no_internetdb_information', code=code)
            if code != 200:
                return outcome('error', 'source_http_error', code=code)
            if response.getheader('Content-Encoding', 'identity') not in {'identity', ''}:
                return outcome('error', 'unsupported_source_encoding', code=code)
            remaining()
            read_chunk = getattr(response, 'read1', None)
            if read_chunk is None:
                # Test doubles and bounded fallback. Real HTTPResponse.read1
                # returns each available chunk so stop is observed promptly.
                raw = response.read(self.max_bytes + 1)
            else:
                chunks = []
                size = 0
                while size <= self.max_bytes:
                    if not active():
                        return outcome('not_attempted', 'case_stopped', code=code)
                    remaining()
                    part = read_chunk(min(65536, self.max_bytes + 1 - size))
                    if not part:
                        break
                    chunks.append(part)
                    size += len(part)
                raw = b''.join(chunks)
            remaining()
            if not active():
                return outcome('not_attempted', 'case_stopped', code=code)
            if len(raw) > self.max_bytes:
                # A truncated JSON string can end inside a credential field.
                # Keep the gap receipt, discard incomplete raw content.
                return outcome('partial', 'response_byte_budget_exhausted',
                               code=code, truncated=True)
            body = raw.decode('utf-8', errors='strict')
            return outcome('success', '', body=body, code=code)
        except (TimeoutError, socket.timeout):
            return outcome('error', 'source_timeout')
        except Denied:
            return outcome('blocked', 'source_policy_denied')
        except (OSError, http.client.HTTPException, UnicodeError, ValueError):
            return outcome('error', 'source_connection_or_decode_error')
        finally:
            if timer is not None:
                timer.cancel()
            if connection is not None:
                connection.close()
