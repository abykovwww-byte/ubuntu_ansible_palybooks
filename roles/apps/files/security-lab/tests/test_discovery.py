import asyncio

from test_controller import control
from security_lab.discovery import collect_public_sources
from security_lab.osint_fixture import FixtureSourceClient


def collect(controller, seed='fixture.example.test', **kwargs):
    case_id = controller.create_case(seed)['id']
    run = controller.begin_discovery(case_id)
    client = FixtureSourceClient(seed)
    outcome = asyncio.run(collect_public_sources(controller, case_id, run['id'],
        client=client, interval=0, **kwargs))
    return controller.export(case_id), client, outcome


def test_end_to_end_parsers_graph_dns_and_ip_metadata_preserve_no_scope(control):
    data, client, outcome = collect(control, max_hosts=10)
    sources = {r['source'] for r in data['source_receipts']}
    assert {'crtsh', 'wayback', 'github_repositories', 'dns', 'rdap_bootstrap', 'rdap_domain', 'ripestat', 'internetdb'} <= sources
    assert all(r['synthetic'] for r in data['source_receipts'])
    assert len(data['links']) > 15 and len(data['surface']) > 15
    assert data['jobs'] == [] and data['manifests'] == []
    assert outcome['status'] == 'completed'
    dns_hosts = {r.subject for r in client.requests if r.source == 'dns'}
    assert 'api.fixture.example.test' in dns_hosts
    assert 'ns.provider.example.test' not in dns_hosts
    assert all(not n['ownership_proven'] for n in data['surface'])


def test_request_budget_records_omission_without_extra_requests(control):
    data, client, outcome = collect(control, max_requests=1)
    assert len(client.requests) == 1
    assert outcome['status'] == 'partial'
    assert any(r['status'] == 'not_attempted' for r in data['source_receipts'])
    assert len(data['source_receipts']) < 10


def test_many_ct_candidates_do_not_exhaust_receipt_budget(control):
    import json
    from security_lab.osint_transport import body_hash
    class WideFixture(FixtureSourceClient):
        def fetch(self, request, **kwargs):
            result = super().fetch(request, **kwargs)
            if request.source == 'crtsh':
                body = json.dumps([{'name_value': f'h{i}.{self.seed}'} for i in range(450)])
                return result.model_copy(update={'body': body, 'body_hash': body_hash(body)})
            return result
    case_id = control.create_case('fixture.example.test')['id']
    run = control.begin_discovery(case_id)
    client = WideFixture('fixture.example.test')
    outcome = asyncio.run(collect_public_sources(control, case_id, run['id'], client=client, interval=0))
    data = control.get_case(case_id)
    assert outcome['status'] == 'partial'
    assert len(data['source_receipts']) < 100
    assert sum(r['reason'] == 'hostname_pivot_budget_exhausted' for r in data['source_receipts']) == 1
    assert any(r['source'] == 'ripestat' for r in data['source_receipts'])


def test_stop_during_request_rejects_late_response_and_future_pivots(control):
    case_id = control.create_case('fixture.example.test')['id']
    run = control.begin_discovery(case_id)
    class StopFixture(FixtureSourceClient):
        def fetch(self, request, **kwargs):
            result = super().fetch(request, **kwargs)
            control.stop(case_id)
            return result
    client = StopFixture('fixture.example.test')
    outcome = asyncio.run(collect_public_sources(control, case_id, run['id'], client=client, interval=0))
    data = control.get_case(case_id)
    assert data['state'] == 'paused' and data['source_receipts'] == []
    assert outcome['status'] == 'cancelled' and len(client.requests) == 1


def test_expired_stage_budget_records_unattempted_sources_and_makes_no_requests(control):
    times = iter([0] + [2] * 100)
    data, client, outcome = collect(control, max_seconds=1, monotonic=lambda: next(times))
    assert client.requests == [] and outcome['status'] == 'partial'
    assert all(r['status'] == 'not_attempted' for r in data['source_receipts'])
    assert {r['source'] for r in data['source_receipts']} == {'crtsh', 'wayback', 'github_repositories', 'rdap_bootstrap', 'dns'}
