"""Explicit offline provider fixture running the same parsers/pivots as collection."""
import json
import time
from urllib.parse import parse_qs, urlsplit

from .contracts import hostname
from .discovery_contracts import SourceResult
from .osint_transport import PublicSourceClient, body_hash


class FixtureSourceClient(PublicSourceClient):
    synthetic = True

    def __init__(self, seed, clock=time.time):
        super().__init__(clock=clock)
        self.seed = hostname(seed)
        self.requests = []

    def fetch(self, request, *, active=lambda: True, timeout_seconds=None):
        self.validate_request(request)
        self.requests.append(request)
        seed = self.seed
        if request.source == 'crtsh':
            payload = [{'name_value': seed + '\napi.' + seed + '\n*.www.' + seed, 'id': 101}]
        elif request.source == 'wayback':
            payload = [['original', 'timestamp', 'statuscode'],
                       ['https://docs.' + seed + '/api/openapi.json', '20250101000000', '200']]
        elif request.source == 'github_repositories':
            payload = {'total_count': 1, 'incomplete_results': False, 'items': [{
                'full_name': 'fixture-osint/domain-map',
                'html_url': 'https://github.com/fixture-osint/domain-map',
                'description': 'SYNTHETIC reference to ' + seed,
            }]}
        elif request.source == 'dns':
            rr = parse_qs(urlsplit(request.url).query)['type'][0]
            values = {'A': (1, '8.8.8.8'), 'MX': (15, '10 mail.' + seed + '.'),
                      'NS': (2, 'ns.provider.example.test.'), 'TXT': (16, '"v=spf1 -all"'),
                      'SOA': (6, 'ns.provider.example.test. admin.' + seed + '. 1 3600 600 86400 300')}
            payload = {'Status': 0, 'Answer': []}
            if rr in values:
                kind, data = values[rr]
                payload['Answer'] = [{'name': request.subject + '.', 'type': kind, 'data': data}]
        elif request.source == 'rdap_bootstrap':
            payload = {'services': [[[seed.split('.')[-1]], ['https://rdap.fixture.example.test/']]]}
        elif request.source == 'rdap_domain':
            payload = {'objectClassName': 'domain', 'ldhName': seed, 'status': ['active'],
                       'events': [{'eventAction': 'registration', 'eventDate': '2025-01-01T00:00:00Z'}]}
        elif request.source == 'ripestat':
            payload = {'status': 'ok', 'data': {'prefix': '8.8.8.0/24', 'asns': [15169]}}
        elif request.source == 'internetdb':
            payload = {'ip': request.subject, 'ports': [443], 'hostnames': ['indexed.' + seed],
                       'cpes': [], 'vulns': [], 'tags': ['SYNTHETIC']}
        else:
            raise ValueError('Unknown fixture source')
        body = json.dumps(payload, ensure_ascii=False)
        return SourceResult(request=request, collected_at=float(self.clock()), status='success',
            http_status=200, body=body, body_hash=body_hash(body), synthetic=True)
