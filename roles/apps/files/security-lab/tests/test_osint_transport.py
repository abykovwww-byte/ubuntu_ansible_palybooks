import json
import socket
import ssl
import threading
import time

import pytest

from security_lab.discovery_contracts import SourceRequest
from security_lab.osint_transport import PinnedHTTPS, PublicSourceClient, body_hash, redact_public_text


def request(source='dns', url='https://dns.google/resolve?name=fixture.example.test&type=A', subject='fixture.example.test'):
    return SourceRequest(source=source, category='dns', url=url, parser=source, subject=subject)


class Response:
    def __init__(self, status=200, body=b'{"Status":0,"Answer":[]}', encoding='identity'):
        self.status, self.body, self.encoding = status, body, encoding

    def getheader(self, name, default=None):
        return self.encoding if name == 'Content-Encoding' else default

    def read(self, limit):
        return self.body[:limit]


class Connection:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.calls = []

    def request(self, method, path, headers):
        self.calls.append((method, path, headers))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


def client_for(response, addresses=('8.8.8.8',), **options):
    connection = Connection(response)
    pins = []
    def factory(host, address, timeout):
        pins.append((host, address))
        return connection
    client = PublicSourceClient(resolver=lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (a, 443)) for a in addresses],
        connection_factory=factory, **options)
    return client, connection, pins


@pytest.mark.parametrize('addresses', [('127.0.0.1',), ('8.8.8.8', '169.254.169.254'), ('::1',), ('10.1.2.3',)])
def test_source_dns_private_or_mixed_answer_never_connects(addresses):
    client, connection, pins = client_for(Response(), addresses)
    result = client.fetch(request())
    assert result.status == 'blocked'
    assert pins == [] and connection.calls == []


def test_connects_to_public_pin_with_provider_identity_and_no_credentials():
    client, connection, pins = client_for(Response())
    result = client.fetch(request())
    assert result.status == 'success'
    assert result.body_hash == body_hash(result.body)
    assert pins == [('dns.google', '8.8.8.8')]
    method, path, headers = connection.calls[0]
    assert method == 'GET' and path.startswith('/resolve?')
    assert not {'Authorization', 'Cookie'} & headers.keys()
    assert connection.closed


@pytest.mark.parametrize('code,status', [(302, 'blocked'), (403, 'blocked'), (429, 'rate_limited'), (500, 'error')])
def test_http_failure_is_not_empty_and_redirects_are_not_followed(code, status):
    client, connection, _ = client_for(Response(code, b'private diagnostic'))
    result = client.fetch(request())
    assert result.status == status and result.body == ''
    assert len(connection.calls) == 1


def test_limited_response_is_partial_not_successful_empty():
    client, _, _ = client_for(Response(body=b'{' * 21), max_bytes=20)
    result = client.fetch(request())
    assert result.status == 'partial' and result.truncated
    assert result.body == ''


@pytest.mark.parametrize('url', ['https://dns.google.evil.test/resolve?name=fixture.example.test&type=A',
    'http://dns.google/resolve?name=fixture.example.test&type=A',
    'https://dns.google/other', 'https://user:secret@dns.google/resolve',
    'https://dns.google/resolve?name=other.test&type=A',
    'https://dns.google/resolve?name=fixture.example.test&type=AXFR'])
def test_request_cannot_escape_provider_endpoint_or_dns_query(url):
    client, connection, pins = client_for(Response())
    result = client.fetch(request(url=url))
    assert result.status in {'blocked', 'error'}
    assert pins == [] and connection.calls == []


def test_source_timeout_is_error():
    client = PublicSourceClient(resolver=lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError()))
    assert client.fetch(request()).reason == 'source_timeout'


def test_stop_after_resolution_prevents_request():
    client, connection, pins = client_for(Response())
    flags = iter([True, False])
    result = client.fetch(request(), active=lambda: next(flags))
    assert result.status == 'not_attempted' and pins == [] and connection.calls == []


def test_unknown_rdap_registry_is_not_accepted_as_arbitrary_http():
    client, _, pins = client_for(Response())
    rdap = request(source='rdap_domain', url='https://rdap.registry.test/domain/fixture.example.test')
    assert client.fetch(rdap).status == 'blocked'
    client.authorize_registry(rdap)
    assert client.fetch(rdap).status == 'success'
    assert pins == [('rdap.registry.test', '8.8.8.8')]


@pytest.mark.parametrize('source,url', [
    ('crtsh', 'https://crt.sh/?q=other.test&output=json'),
    ('github_repositories', 'https://api.github.com/search/repositories?q=unrelated&per_page=100&page=1'),
    ('wayback', 'https://web.archive.org/cdx/search/cdx?url=other.test/*&output=json'),
    ('rdap_bootstrap', 'https://data.iana.org/rdap/dns.json?callback=arbitrary'),
])
def test_planned_source_queries_cannot_change_seed_or_remove_limits(source, url):
    client, connection, pins = client_for(Response())
    assert client.fetch(request(source=source, url=url)).status == 'blocked'
    assert pins == [] and connection.calls == []


def test_public_credential_redaction_preserves_json_shape():
    body = '{"api_key":"abcdef-secret", "description":"ghp_' + 'X' * 36 + '"}'
    redacted = redact_public_text(body)
    assert 'abcdef-secret' not in redacted and 'ghp_' not in redacted
    assert json.loads(redacted)['api_key'] == '[REDACTED_CREDENTIAL]'


def test_complete_nested_secret_values_and_embedded_url_credentials_are_removed():
    original = {"records": [{"api_key": "secret with spaces and \"quotes\"", "client-secret": {"part": ["one two", "three"]},
                             "url": "https://alice:p%40ss@public.example.test/path?access_token=short&other=visible"}],
                "note": "ordinary public material"}
    text = redact_public_text(json.dumps(original))
    result = json.loads(text)
    assert result['records'][0]['api_key'] == '[REDACTED_CREDENTIAL]'
    assert result['records'][0]['client-secret']['part'] == ['[REDACTED_CREDENTIAL]', '[REDACTED_CREDENTIAL]']
    assert 'alice' not in text and 'p%40ss' not in text and 'short' not in text
    assert '?access_token=[REDACTED_CREDENTIAL]&other=visible' in result['records'][0]['url']
    assert result['note'] == original['note']


def test_partial_nonjson_secret_text_is_redacted_without_leaking_spaces():
    text = redact_public_text('prefix api_key="secret with spaces" url=https://name:pass@public.test/?token=xy')
    assert 'secret' not in text and 'with spaces' not in text and 'name:pass' not in text and 'token=xy' not in text


def test_receipt_hash_covers_sanitized_body():
    client, _, _ = client_for(Response(body=b'{"api_key":"long secret with spaces"}'))
    result = client.fetch(request())
    assert 'long secret' not in result.body
    assert result.body_hash == body_hash(result.body)


def test_redaction_expansion_respects_evidence_byte_budget():
    client, _, _ = client_for(Response(body=b'{"api_key":"x"}'), max_bytes=20)
    result = client.fetch(request())
    assert result.status == 'partial' and result.truncated
    assert result.reason == 'sanitization_byte_budget_exhausted'
    assert result.body == '' and result.body_hash == body_hash('')


def test_native_dns_timeout_never_connects_and_has_bounded_wait():
    release = threading.Event()
    completed = threading.Event()
    connection_attempts = []
    def slow_resolver(*args, **kwargs):
        try:
            release.wait(1)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))]
        finally:
            completed.set()
    client = PublicSourceClient(resolver=slow_resolver,
                                connection_factory=lambda *args: connection_attempts.append(args))
    try:
        began = time.monotonic()
        result = client.fetch(request(), timeout_seconds=0.05)
        assert time.monotonic() - began < 0.8
        assert result.status == 'error' and result.reason == 'source_timeout'
        assert connection_attempts == [] and result.body == ''
    finally:
        release.set()
        assert completed.wait(1)
    assert connection_attempts == []


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_overall_deadline_watchdog_closes_socket_and_discards_slow_response(stage):
    interrupted = threading.Event()
    class Sock:
        def shutdown(self, _direction):
            interrupted.set()
        def close(self):
            interrupted.set()
    class SlowResponse(Response):
        def read1(self, _limit):
            assert interrupted.wait(1)
            raise OSError('synthetic interrupted read')
    class SlowConnection(Connection):
        sock = Sock()
        def getresponse(self):
            if stage == 'headers':
                assert interrupted.wait(1)
                raise OSError('synthetic interrupted headers')
            return self.response
    connection = SlowConnection(SlowResponse())
    client = PublicSourceClient(resolver=lambda *args, **kwargs:
        [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 443))],
        connection_factory=lambda *args: connection)
    began = time.monotonic()
    result = client.fetch(request(), timeout_seconds=0.05)
    assert time.monotonic() - began < 0.8
    assert interrupted.is_set() and connection.closed
    assert result.status == 'error' and result.reason == 'source_timeout' and result.body == ''


def test_stop_between_body_chunks_discards_collected_prefix():
    stop = threading.Event()
    class ChunkResponse(Response):
        def read1(self, _limit):
            stop.set()
            return b'{"api_key":"unreturned-secret'
    client, connection, _ = client_for(ChunkResponse())
    result = client.fetch(request(), active=lambda: not stop.is_set())
    assert result.status == 'not_attempted' and result.reason == 'case_stopped'
    assert result.body == '' and connection.closed


def test_pinned_https_validates_tls_and_wraps_provider_sni_on_exact_ip(monkeypatch):
    captured = []
    class Raw:
        closed = False
        def close(self):
            self.closed = True
    raw = Raw()
    def connect(address, timeout):
        captured.append(('connect', address, timeout))
        return raw
    monkeypatch.setattr(socket, 'create_connection', connect)
    connection = PinnedHTTPS('dns.google', '8.8.8.8', 0.5)
    assert connection._context.check_hostname is True
    assert connection._context.verify_mode == ssl.CERT_REQUIRED
    class Context:
        def wrap_socket(self, socket_value, **options):
            assert connection.sock is raw
            captured.append(('wrap', socket_value, options))
            return self
        def do_handshake(self):
            captured.append(('handshake',))
        def close(self):
            raw.close()
    connection._context = Context()
    connection.connect()
    assert captured[0] == ('connect', ('8.8.8.8', 443), 0.5)
    assert captured[1][2] == {'server_hostname': 'dns.google', 'do_handshake_on_connect': False}
    assert captured[2] == ('handshake',)
    connection.close()
    assert raw.closed


def test_pinned_tls_handshake_failure_closes_raw_socket(monkeypatch):
    class Raw:
        closed = False
        def close(self):
            self.closed = True
    raw = Raw()
    monkeypatch.setattr(socket, 'create_connection', lambda *args, **kwargs: raw)
    class Context:
        def wrap_socket(self, socket_value, **options):
            raise ssl.SSLError('synthetic invalid certificate')
    connection = PinnedHTTPS('dns.google', '8.8.8.8', 0.5)
    connection._context = Context()
    with pytest.raises(ssl.SSLError):
        connection.connect()
    assert raw.closed and connection.sock is None
