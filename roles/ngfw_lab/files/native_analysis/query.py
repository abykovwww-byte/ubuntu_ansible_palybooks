"""Influx API read-back with immutable raw receipt and credential-free metadata."""
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen


def build_request(endpoint, version, query, database=None, org=None, credential_env=None, username=None):
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use HTTP(S) endpoint without embedded credentials, query or fragment')
    headers = {}
    secret = os.environ[credential_env] if credential_env else None
    if version == '1':
        # One read statement. Keep writes/admin statements out of this tool.
        if not re.match(r'^\s*(SELECT|SHOW|EXPLAIN)\b', query, re.I) or ';' in query or re.search(r'\bINTO\b', query, re.I):
            raise ValueError('InfluxQL capture permits a single read statement without INTO')
        params = {'q': query, 'epoch': 'ns'}
        if database:
            params['db'] = database
        if secret:
            if username is None:
                raise ValueError('InfluxDB 1 authentication needs username')
            headers['Authorization'] = 'Basic ' + base64.b64encode((username + ':' + secret).encode()).decode()
        return Request(endpoint.rstrip('/') + '/query?' + urlencode(params), headers=headers)
    if version == '2':
        if not org:
            raise ValueError('InfluxDB 2 needs org')
        # Flux is a programming language; capture only through a read-only token.
        if not secret:
            raise ValueError('InfluxDB 2 requires a read-only token in credential_env')
        headers.update({'Authorization': 'Token ' + secret, 'Content-Type': 'application/json', 'Accept': 'application/csv'})
        return Request(endpoint.rstrip('/') + '/api/v2/query?' + urlencode({'org': org}),
                       data=json.dumps({'query': query, 'type': 'flux'}).encode(), headers=headers)
    raise ValueError('Supported API versions: 1 (InfluxQL JSON), 2 (Flux annotated CSV)')


def capture(output, endpoint, version, query, **kwargs):
    """Do not overwrite receipts. HTTP failures do not become empty measurements."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    request = build_request(endpoint, version, query, **kwargs)
    started = datetime.now(timezone.utc).isoformat()
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read()
            content_type = response.headers.get('Content-Type')
        (output / ('raw.json' if version == '1' else 'raw.csv')).write_bytes(raw)
        error = None
        if version == '1':
            parsed = json.loads(raw)
            errors = [r['error'] for r in parsed.get('results', []) if 'error' in r]
            if parsed.get('error'):
                errors.append(parsed['error'])
            if parsed.get('partial') or any(r.get('partial') or any(s.get('partial') for s in r.get('series', []))
                                            for r in parsed.get('results', [])):
                errors.append('Partial result requires explicit continuation capture')
            if errors:
                error = 'InfluxDB response contains query errors; inspect raw receipt'
        metadata = {'schema_version': 1, 'endpoint': endpoint, 'api_version': version, 'query': query,
                    'database': kwargs.get('database'), 'org': kwargs.get('org'),
                    'started_at': started, 'received_at': datetime.now(timezone.utc).isoformat(),
                    'raw_sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                    'content_type': content_type, 'status': 'QUERY_ERROR' if error else 'RECEIVED',
                    'completeness': 'UNKNOWN_NOT_INFERRED_FROM_HTTP_SUCCESS'}
        (output / 'receipt.json').write_text(json.dumps(metadata, indent=2), encoding='utf-8')
        if error:
            raise ValueError(error)
        return metadata
    except Exception as exc:
        # Do not serialize exception text: HTTP errors can echo request credentials.
        (output / 'failure.json').write_text(json.dumps({'status': 'CAPTURE_FAILED', 'error_type': type(exc).__name__,
            'started_at': started, 'failed_at': datetime.now(timezone.utc).isoformat()}), encoding='utf-8')
        raise
