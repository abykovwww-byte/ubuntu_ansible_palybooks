"""Fixed single-service toolbox checks; no shell, credentials or model flags."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import re
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit, urlencode

from pydantic import Field, TypeAdapter, field_validator

from .contracts import Contract, Denied, Scope, HTTPRequest, ActionPlan, hostname, is_private_network_address, network_address
from .tool_catalog import CurlCheck, NmapCheck, _check_stage, build_check
from .web_checks import BrowserCheck, FFUFCheck, ZAPCheck, WEB_CHECKS, web_policy
from .contracts import canonical


class ServiceCheck(Contract):
    tool: Literal['nmap_service'] = 'nmap_service'
    host: str
    port: int = Field(ge=1, le=65535)
    seconds: int = Field(default=30, ge=1, le=60)
    _host = field_validator('host')(hostname)


class TLSCheck(Contract):
    tool: Literal['tls'] = 'tls'
    host: str
    port: int = Field(default=443, ge=1, le=65535)
    starttls: Literal['none', 'postgres'] = 'none'
    seconds: int = Field(default=15, ge=1, le=60)
    _host = field_validator('host')(hostname)


NativeCheck = Annotated[Union[CurlCheck, HTTPRequest, ServiceCheck, TLSCheck, BrowserCheck, FFUFCheck, ZAPCheck], Field(discriminator='tool')]
_ADAPTER = TypeAdapter(NativeCheck)


def parse_native_check(value: dict):
    return _ADAPTER.validate_python(value, strict=True)


def check_subject(spec, scope: Scope) -> str:
    """Authorization is checked before DNS or any runtime operation."""
    if isinstance(spec, WEB_CHECKS):
        web_policy(spec, scope)
        return urlsplit(spec.url).hostname
    if isinstance(spec, (CurlCheck, HTTPRequest)):
        if not any(target.permits(spec.url, spec.method) for target in scope.targets):
            raise Denied('HTTP check outside approved service, path or method')
        return urlsplit(spec.url).hostname
    if not any(target.host == spec.host and spec.port in target.tcp_ports for target in scope.network_targets):
        raise Denied('Service/TLS check requires explicit approved network host and port')
    return spec.host


@dataclass(frozen=True)
class NativePlan:
    tool: str
    profile: str
    argv: tuple[str, ...]
    seconds: int
    specification: dict
    scope: dict
    addresses: tuple[str, ...]
    task_stage: str = 'checks'


def compile_native_check(specification: dict, scope: Scope, addresses: list[str], *, stage: str, now: float,
                         task_stage: str = 'checks') -> NativePlan:
    spec = parse_native_check(specification)
    scope = Scope.model_validate(scope.model_dump(mode='json'), strict=True)
    if task_stage == 'poc':
        scope.check_window(now)
        if stage not in {'actions_approved', 'poc_running', 'checks_review'} or 'poc' not in scope.authorization.actions:
            raise Denied('PoC requires separate action authorization')
        if not isinstance(spec, HTTPRequest):
            raise Denied('Only a saved HTTP request is executable as a PoC action')
        if now + spec.seconds > min(scope.expires_at, scope.authorization.valid_until):
            raise Denied('Action request exceeds authorization window')
    elif task_stage == 'checks':
        _check_stage(scope, stage, now, spec.seconds)
        if isinstance(spec, HTTPRequest) and spec.method == 'POST':
            raise Denied('POST requires a separately activated exact action request')
    else:
        raise Denied('Unsupported native execution stage')
    host = check_subject(spec, scope)
    if len(addresses) != 1:
        raise Denied('A native service check requires one host-selected address')
    address = network_address(addresses[0])
    grant = next((target for target in scope.network_targets if target.host == host), None)
    if grant and grant.addresses and address not in grant.addresses:
        raise Denied('DNS address exceeds explicit address grants')
    if is_private_network_address(address) and (grant is None or address not in grant.addresses):
        raise Denied('Private service checks require an explicit address grant')
    bracketed = '[' + address + ']' if ipaddress.ip_address(address).version == 6 else address
    if isinstance(spec, WEB_CHECKS):
        policy = web_policy(spec, scope)
        argv = ('/opt/web-tools-venv/bin/python', '/usr/local/libexec/pentestgpt-web-check.py',
                '--policy', canonical(policy))
        profile = 'target_http'
    elif isinstance(spec, HTTPRequest):
        parsed = urlsplit(spec.url)
        url = spec.url + ('?' + urlencode(spec.query) if spec.query else '')
        argv = ('/usr/bin/curl', '--disable', '--globoff', '--silent', '--show-error', '--http1.1',
                '--noproxy', '*', '--proto', '=' + parsed.scheme, '--max-redirs', '0', '--retry', '0',
                '--connect-timeout', str(min(5, spec.seconds)), '--max-time', str(spec.seconds),
                '--max-filesize', '65536', '--include', '--resolve', f'{host}:{parsed.port}:{bracketed}',
                '--write-out', '\\nSECURITY_LAB_HTTP_STATUS:%{http_code}\\n')
        argv += ('--head',) if spec.method == 'HEAD' else ('--request', spec.method)
        for key, value in spec.headers.items():
            argv += ('--header', f'{key}: {value}')
        if spec.body is not None:
            argv += ('--data-raw', spec.body)
        argv += ('--url', url)
        profile = 'target_http'
    elif isinstance(spec, CurlCheck):
        base = build_check(spec, scope, stage=stage, now=now)
        parsed = urlsplit(spec.url)
        argv = (*base.argv[:-2], '--verbose', '--resolve', f'{host}:{parsed.port}:{bracketed}', *base.argv[-2:])
        profile = 'target_http'
    elif isinstance(spec, ServiceCheck):
        base = build_check(NmapCheck(host=host, addresses=[address], ports=[spec.port], seconds=spec.seconds),
                           scope, stage=stage, now=now, pinned_addresses={host: [address]})
        # Fixed version-light, one service, no caller-selected NSE. Nmap can
        # run its built-in version-category scripts during -sV. Its port rate does
        # not independently bound version-probe traffic; the wall deadline does.
        argv = (*base.argv[:6], '-sV', '--version-light', *base.argv[6:])
        profile = 'target_network'
    else:
        argv = ('/usr/bin/openssl', 's_client', '-connect', f'{bracketed}:{spec.port}',
                '-servername', host, '-verify_hostname', host, '-verify_return_error', '-brief', '-no_ign_eof')
        if spec.starttls != 'none':
            argv += ('-starttls', spec.starttls)
        profile = 'target_network'
    return NativePlan(spec.tool, profile, tuple(argv), spec.seconds, spec.model_dump(mode='json'),
                      scope.model_dump(mode='json'), (address,), task_stage)


def native_check_catalog(configured: bool) -> list[dict]:
    checks = [{'name': 'compose:' + name, 'tool': name, 'available': configured, 'qualified': False,
             'allowed_actions': ['checks'], 'execution_mode': 'existing_compose_runtime',
             'mcp_tool': 'start_service_check', 'schema': _ADAPTER.json_schema(),
             'limitations': limits, 'live_qualification': 'requires actual CLI receipt on the applied tools image'}
            for name, limits in (
                ('curl', 'One pinned GET/HEAD; TLS verification; no query, redirects, auth or custom payloads'),
                ('http', 'Pinned GET/HEAD/OPTIONS with query and public headers; POST/body only from an activated exact action plan'),
                ('nmap_service', 'One scoped TCP port, version-light; no caller-selected NSE; built-in version scripts may run; version probes are not RPS-enforced'),
                ('tls', 'One TLS handshake, optionally PostgreSQL SSLRequest; no authentication or application data'),
                ('playwright', 'Fresh Chromium context; scoped GET/HEAD, request/rate/time bounds; no redirects, credentials, writes or WebSockets; host artifacts'),
                ('ffuf', 'At most 32 admitted relative paths plus control; one thread, scope RPS; no redirects/recursion/calibration; host artifacts'),
                ('zap', 'One TLS-verified curl GET; ZAP offline HAR import and passive scan with bundled add-ons; no spider, active scan, login or runtime updates; host artifacts'))]
    return checks + [{'name':'compose:saved_http_action', 'available':configured, 'qualified':False,
        'allowed_actions':['poc'], 'mcp_tool':'start_poc_action', 'manifest_schema':ActionPlan.model_json_schema(),
        'limitations':'Executes one exact saved action.request after separate action activation; POST/body allowed only within HTTP scope. '
                      'No credentials or redirects; response verification is an observation, not vulnerability proof.'}]


def evaluate_action_response(receipt: dict, expected: dict | None) -> dict:
    """An observable predicate is not a vulnerability/impact verifier."""
    result = {'poc_observation': 'inconclusive', 'vulnerability_verified': False}
    rows = receipt.get('receipts', [])
    if receipt.get('status') != 'completed' or receipt.get('receipts_truncated') or len(rows) != 1:
        return result
    row = rows[0]
    status = re.search(r'\nSECURITY_LAB_HTTP_STATUS:(\d{3})\n?$', row.get('stdout', ''))
    if row.get('exit_code') != 0 or status is None:
        return result
    result['http_status'] = int(status.group(1))
    if not expected:
        return {**result, 'poc_observation': 'response_recorded'}
    if row.get('response_body', {}).get('representation') == 'binary_summary' and expected.get('contains') is not None:
        # A binary summary is evidence of bytes, never a substitute body marker.
        return result
    response = row['stdout'][:status.start()].replace('\r\n', '\n')
    body = ''
    while response.startswith('HTTP/1.') and '\n\n' in response:
        headers, _, remaining = response.partition('\n\n')
        if re.match(r'HTTP/1\.[01] 1\d\d\b', headers):
            response = remaining
            continue
        body = remaining
        break
    matched = (expected.get('status') is None or expected['status'] == result['http_status']) and (
        expected.get('contains') is None or expected['contains'] in body)
    return {**result, 'poc_observation': 'expected_observation_matched' if matched else 'expected_observation_not_matched'}
