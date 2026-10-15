import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from security_lab.compose_service import ComposeRunService
from security_lab.compose_runtime import _request_goal, MAX_GOAL_CHARS, prepare_handoff_context
from security_lab.contracts import Denied, Scope, canonical
from security_lab.handoff import build_handoff
from test_controller import NOW, approve, scope_body, control


class Runtime:
    config = SimpleNamespace(max_runtime_seconds=1800)

    def __init__(self, *, close=True, running=False):
        self.calls = []
        self.close, self.running = close, running

    async def start(self, **kwargs):
        self.calls.append(('start', kwargs))
        return {'status': 'running', 'synthetic': False}

    async def poll(self, run_id):
        self.calls.append(('poll', run_id))
        return {'status': 'running' if self.running else 'completed',
                'command_receipts': [{'command': 'nmap', 'stdout': '80/tcp open'}]}

    async def stop(self, run_id):
        self.calls.append(('stop', run_id))
        return {'access_closed': self.close}

    async def run_tool(self, **kwargs):
        self.calls.append(('run_tool', kwargs))
        return {'status': 'completed', 'access_closed': True,
                'receipts': [{'type': 'command_run', 'exit_code': 0, 'stdout': '<nmaprun />'}]}


def activated(control, **overrides):
    case_id = control.create_case('example.com')['id']
    control.collect_fixture(case_id)
    approve(control, case_id, scope_body(targets=[{'host': 'example.com'}], **overrides))
    return case_id


def test_existing_runtime_records_real_trace_without_fake_network_ack(control):
    case_id = activated(control)
    runtime = Runtime()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        job = await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
        assert control.get_job_status(case_id, job['id'])['outcome'] == 'completed'
    asyncio.run(run())
    exported = control.export(case_id)
    body = json.loads(exported['evidence'][-1]['body'])
    assert body['execution_mode'] == 'existing_compose_runtime'
    assert body['network_ack'] is False and body['reduced_scope_enforced'] is False
    assert body['verified_vulnerabilities'] == [] and '80/tcp open' in body['receipt']
    assert body['access_closed'] is True
    events = [row[0] for row in control.db.execute('SELECT event FROM audit WHERE case_id=?', (case_id,))]
    assert 'native_runtime_admitted' in events and 'job_network_acknowledged' not in events


def test_reduced_http_scope_never_starts_provider(control):
    case_id = control.create_case('example.com')['id']
    control.collect_fixture(case_id)
    approve(control, case_id)
    runtime = Runtime()
    async def run():
        with pytest.raises(Denied, match='reduced HTTP'):
            await ComposeRunService(control, runtime).start(case_id)
    asyncio.run(run())
    assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []


@pytest.mark.parametrize('removed', [True, False])
def test_final_evidence_distinguishes_process_closure_and_handoff_cleanup(control, removed):
    case_id = activated(control)

    class CleanupRuntime(Runtime):
        async def stop(self, run_id):
            return {'access_closed': True, 'handoff_removed': removed,
                    'handoff_cleanup_error': None if removed else 'owned_artifact_validation_failed'}

    async def run():
        service = ComposeRunService(control, CleanupRuntime(), poll_seconds=.01)
        job = await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
        receipt_id = control.get_job_status(case_id, job['id'])['evidence_id']
        body = json.loads(control.db.execute('SELECT body FROM evidence WHERE id=?', (receipt_id,)).fetchone()[0])
        assert body['access_closed'] is True
        assert body['handoff_removed'] is removed
        assert body['handoff_cleanup_error'] == (None if removed else 'owned_artifact_validation_failed')
    asyncio.run(run())


def test_stopping_case_joins_exact_native_runtime(control):
    case_id = activated(control)
    runtime = Runtime(running=True)
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        job = await service.start(case_id)
        control.stop(case_id)
        await service.stop_case(case_id)
        assert control.get_job_status(case_id, job['id'])['outcome'] == 'interrupted'
        assert not service.tasks
    asyncio.run(run())
    assert runtime.calls[-1][0] == 'stop'
    assert control.get_case(case_id)['state'] == 'paused'


def test_uncertain_closure_pauses_case(control):
    case_id = activated(control)
    runtime = Runtime(close=False)
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        job = await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
        assert control.get_job_status(case_id, job['id'])['outcome'] == 'interrupted'
    asyncio.run(run())
    assert control.get_case(case_id)['state'] == 'paused'


def test_nmap_uses_explicit_private_scope_and_host_pins_without_provider(control):
    case_id = activated(control, network_targets=[{'host': 'example.com', 'tcp_ports': [8080],
                                                   'addresses': ['172.20.1.3']}])
    runtime = Runtime()
    async def resolve(host):
        assert host == 'example.com'
        return ['172.20.1.3']
    async def run():
        service = ComposeRunService(control, runtime, resolver=resolve)
        job = await service.start_nmap(case_id, 'example.com', [8080], seconds=15)
        while service.tasks:
            await asyncio.sleep(.01)
        assert control.get_job_status(case_id, job['id'])['outcome'] == 'completed'
    asyncio.run(run())
    assert not any(kind == 'start' for kind, _ in runtime.calls)
    call = next(values for kind, values in runtime.calls if kind == 'run_tool')
    assert call['argv'][0] == '/usr/bin/nmap' and call['argv'][-1] == '172.20.1.3'
    assert '-sT' in call['argv'] and '-sV' not in call['argv']
    evidence = json.loads(control.export(case_id)['evidence'][-1]['body'])
    assert evidence['provider_called'] is False and evidence['native_full_access'] is False
    assert '<nmaprun />' in evidence['receipt']


def test_http_scope_does_not_authorize_nmap_or_trigger_dns(control):
    case_id = activated(control)
    async def unexpected(host):
        raise AssertionError('No DNS before network authorization')
    async def run():
        with pytest.raises(Denied, match='explicit approved network'):
            await ComposeRunService(control, Runtime(), resolver=unexpected).start_nmap(case_id, 'example.com', [443])
    asyncio.run(run())
    assert control.get_case(case_id)['jobs'] == []


def test_cancel_nmap_admission_firstyield_waits_for_owned_runtime_closure(control, monkeypatch):
    case_id = activated(control, network_targets=[{'host':'example.com', 'tcp_ports':[8080],
                                                   'addresses':['172.20.1.3']}])
    async def exercise():
        class HeldRuntime(Runtime):
            def __init__(self):
                super().__init__()
                self.entered = asyncio.Event()
                self.closing = asyncio.Event()
                self.closed = asyncio.Event()
                self.tool_joined = False

            async def run_tool(self, **kwargs):
                self.calls.append(('run_tool', kwargs))
                self.entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    self.tool_joined = True

            async def stop(self, run_id):
                self.calls.append(('stop', run_id))
                self.closing.set()
                await self.closed.wait()
                return {'access_closed':True}

        async def resolve(host):
            return ['172.20.1.3']
        runtime = HeldRuntime()
        service = ComposeRunService(control, runtime, resolver=resolve)
        original_sleep = asyncio.sleep
        starter = None

        async def cancel_at_admission_yield(delay):
            if delay == 0 and asyncio.current_task() is starter:
                # The precise firstyield occurs after task registration. Let
                # the native run enter, then cancel only the caller request.
                await runtime.entered.wait()
                asyncio.current_task().cancel()
            await original_sleep(delay)
        monkeypatch.setattr('security_lab.compose_service.asyncio.sleep', cancel_at_admission_yield)
        starter = asyncio.create_task(service.start_nmap(case_id, 'example.com', [8080], seconds=15))
        await asyncio.wait_for(runtime.closing.wait(), .5)
        assert not starter.done()
        assert runtime.tool_joined
        assert control.export(case_id)['evidence'] == [] or not any(
            row['source'].startswith('worker:') for row in control.export(case_id)['evidence'])
        assert service.preparing is True
        runtime.closed.set()
        with pytest.raises(asyncio.CancelledError):
            await starter
        assert service.tasks == {} and service.preparing is False
        job = control.get_case(case_id)['jobs'][-1]
        assert job['outcome'] == 'interrupted'
        receipt = json.loads(control.export(case_id)['evidence'][-1]['body'])
        assert receipt['access_closed'] is True
        assert runtime.calls[-1] == ('stop', 'nmap-'+job['id'])
    asyncio.run(exercise())


def test_actual_fixture_nodes_and_synthetic_evidence_reach_runtime_with_original_scope_hash(control):
    case_id = activated(control)
    before, manifest = control.checks_context(case_id)
    runtime = Runtime()
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
    asyncio.run(run())
    call = next(values for kind, values in runtime.calls if kind == 'start')
    context = call['osint_context']
    data = context['untrusted_osint_data']
    assert {row['value'] for row in data['surface_nodes']} == {'example.com', 'www.example.com', 'api.example.com'}
    assert context['binding']['hash'] == manifest['hash']
    assert context['generation'] == before['generation']
    assert context == build_handoff(before, manifest, [], now=NOW)
    assert len(data['evidence_index']) == 3
    assert all(row['synthetic'] is True and row['source'] == 'fixture:ct' and len(row['hash']) == 64
               for row in data['evidence_index'])
    assert data['trust'] == 'untrusted_external_data' and context['policy']['poc_allowed'] is False
    assert data['coverage'] == [] and data['gaps']
    assert '"body":' not in canonical(context) and '"token":' not in canonical(context)
    assert call['scope']['budget']['seconds'] < json.loads(manifest['body'])['budget']['seconds']
    descriptor = prepare_handoff_context(Scope.model_validate(call['scope']), context, run_id=call['run_id'])
    _, goal = _request_goal(Scope.model_validate(call['scope']), descriptor)
    assert descriptor['worker_path'] in goal and len(goal) <= MAX_GOAL_CHARS
    assert descriptor['context_hash'] == context['context_hash'] and descriptor['scope_hash'] == manifest['hash']
    receipt = json.loads(control.export(case_id)['evidence'][-1]['body'])
    assert receipt['handoff_context_hash'] == context['context_hash']
    assert receipt['handoff'] == descriptor and len(descriptor['content_sha256']) == 64


def test_full_handoff_preserves_actual_relations_coverage_gaps_and_changes_with_values():
    from test_handoff import snapshot
    exported, manifest = snapshot()
    context = build_handoff(exported, manifest, [], now=NOW)
    data = context['untrusted_osint_data']
    nodes = {row['id']: row for row in data['surface_nodes']}
    link = data['relations'][0]
    assert nodes[link['source_id']]['value'] == 'example.com'
    assert nodes[link['target_id']]['value'] == 'api.example.com'
    assert link['relation'] == 'certificate_name' and link['evidence_ids'] == ['e1']
    assert data['evidence_index'][0]['synthetic'] is True
    assert data['coverage'][0]['status'] == 'partial'
    assert next(row for row in data['gaps'] if row.get('id') == 'e1')['reason'] == 'Budget curtailed response'
    assert data['observations'] and data['relation_observations'] and data['source_receipts']
    assert 'SECRET_' not in json.dumps(context)
    changed = copy.deepcopy(exported)
    changed['surface'][0]['value'] = 'different.cdn.com'
    updated = build_handoff(changed, manifest, [], now=NOW)
    assert len(updated['untrusted_osint_data']['surface_nodes']) == len(data['surface_nodes'])
    assert updated['context_hash'] != context['context_hash']
    assert updated['binding'] == context['binding'] and updated['generation'] == context['generation']


def test_over_one_mib_complete_map_refused_before_job_budget_or_runtime_call(control, monkeypatch):
    case_id = activated(control)
    original = control.checks_context
    snapshot, manifest = original(case_id)
    oversized = copy.deepcopy(snapshot)
    reference = oversized['surface'][0]['evidence_ids']
    oversized['surface'] += [dict(id=f'large-node-{index}', kind='repository',
                                  value=f'https://github.com/fixture/repository-{index}-' + 'x' * 1800,
                                  attribution='candidate', evidence_ids=reference) for index in range(300)]
    monkeypatch.setattr(control, 'checks_context', lambda case: (oversized, manifest))
    runtime = Runtime()
    async def run():
        service = ComposeRunService(control, runtime)
        with pytest.raises(Denied, match='(?i)(size|1.?mi|1048576|limit|bound)'):
            await service.start(case_id)
        assert not service.tasks and service.preparing is False
    asyncio.run(run())
    assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []
    assert control.get_case(case_id)['spent'] == snapshot['spent']


def test_large_full_map_retains_late_candidate_evidence_histories_and_gap_before_runtime(control, monkeypatch):
    case_id = activated(control)
    original, manifest = control.checks_context(case_id)
    snapshot = copy.deepcopy(original)
    evidence = snapshot['evidence'][0]
    refs = json.dumps([evidence['id']])
    snapshot['surface'] += [dict(id=f'node-{index:03}', kind='hostname', value=f'discovered-{index}.example.com',
                                attribution='candidate', evidence_ids=refs) for index in range(92)]
    late_id, late_value = 'z-last-candidate', 'late-unapproved-resource.example.com'
    attribution = ['candidate', 'unknown']
    freshness = ['historical', 'unknown']
    snapshot['surface'].append(dict(id=late_id, kind='hostname', value=late_value,
        attribution='candidate', evidence_ids=refs, attribution_history=attribution, freshness_history=freshness))
    snapshot['observations'].append(dict(id='late-observation', run_id='fixture-run', node_id=late_id,
        receipt_id=evidence['id'], attribution='candidate', freshness='historical', pointer='$.late', collected_at=NOW))
    snapshot['links'].append(dict(id='late-link', source_id=snapshot['surface'][0]['id'], target_id=late_id,
                                 relation='passive_subdomain_candidate', evidence_ids=refs))
    snapshot['link_observations'].append(dict(id='late-relation-history', run_id='fixture-run', link_id='late-link',
        receipt_id=evidence['id'], basis='Historical passive match; no ownership proof', freshness='historical',
        pointer='$.late', collected_at=NOW))
    snapshot['source_receipts'].append(dict(id=evidence['id'], case_id=case_id, run_id='fixture-run',
        source='fixture:ct', category='subdomains', request_url='tool://offline-fixture/domain/example.com',
        subject='example.com', parser='fixture', parser_version='1', collected_at=NOW, status='partial',
        http_status=None, body_hash=evidence['hash'], reason='Distinct source pagination gap retained',
        truncated=True, synthetic=True, schema_version=1, body='SECRET_RAW_BODY'))
    expected = build_handoff(snapshot, manifest, [], now=NOW)
    assert len(canonical(expected)) > 4000
    monkeypatch.setattr(control, 'checks_context', lambda case: (snapshot, manifest))
    runtime = Runtime()
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
    asyncio.run(run())
    call = next(values for kind, values in runtime.calls if kind == 'start')
    assert call['osint_context'] == expected
    data = call['osint_context']['untrusted_osint_data']
    late = next(row for row in data['surface_nodes'] if row['id'] == late_id)
    assert late['value'] == late_value and late['evidence_ids'] == [evidence['id']]
    assert late['attribution_history'] == attribution and late['freshness_history'] == freshness
    assert any(row['node_id'] == late_id and row['freshness'] == 'historical' for row in data['observations'])
    assert any(row['link_id'] == 'late-link' and 'no ownership proof' in row['basis']
               for row in data['relation_observations'])
    assert any(row.get('reason') == 'Distinct source pagination gap retained' for row in data['gaps'])
    assert data['evidence_index'] and data['hypotheses'] and data['coverage']
    assert 'SECRET_RAW_BODY' not in canonical(call['osint_context'])
    assert call['osint_context']['scope_manifest'] == json.loads(manifest['body'])
    assert len(call['scope']['targets']) == 1 and call['scope']['targets'][0]['host'] == 'example.com'
    assert call['osint_context']['policy']['poc_allowed'] is False
    ledger = json.loads(control.export(case_id)['evidence'][-1]['body'])
    assert ledger['target'] == 'https://example.com'
    assert ledger['handoff']['size_bytes'] > 4000 and len(ledger['handoff']['content_sha256']) == 64
    assert ledger['handoff']['context_hash'] == expected['context_hash']
    assert ledger['handoff']['scope_id'] == manifest['id'] and ledger['handoff']['scope_hash'] == manifest['hash']
    assert ledger['handoff']['generation'] == snapshot['generation']


def test_scope_tampering_is_rejected_before_file_preflight_or_job(control, monkeypatch):
    case_id = activated(control)
    snapshot, manifest = control.checks_context(case_id)
    changed = dict(manifest)
    body = json.loads(changed['body'])
    body['budget']['seconds'] -= 1
    changed['body'] = canonical(body)
    monkeypatch.setattr(control, 'checks_context', lambda case: (snapshot, changed))
    runtime = Runtime()
    async def run():
        with pytest.raises(Denied, match='hash mismatch'):
            await ComposeRunService(control, runtime).start(case_id)
    asyncio.run(run())
    assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []


def test_stale_map_generation_is_rejected_before_runtime_launch(control, monkeypatch):
    case_id = activated(control)
    snapshot, manifest = control.checks_context(case_id)
    snapshot = copy.deepcopy(snapshot)
    snapshot['generation'] += 1
    monkeypatch.setattr(control, 'checks_context', lambda case: (snapshot, manifest))
    runtime = Runtime()
    async def run():
        with pytest.raises(Denied, match='binding'):
            await ComposeRunService(control, runtime).start(case_id)
    asyncio.run(run())
    assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []


def test_goal_preflight_reserves_maximum_run_path_before_job_and_actual_path_cannot_grow(control, monkeypatch):
    case_id = activated(control)
    runtime = Runtime()
    preflights = []
    real_goal = _request_goal
    def capture_goal(scope, descriptor, **options):
        assert control.get_case(case_id)['jobs'] == [] and runtime.calls == []
        assert len(descriptor['run_id']) == 64
        target, goal = real_goal(scope, descriptor, **options)
        preflights.append((descriptor, goal))
        return target, goal
    monkeypatch.setattr('security_lab.compose_service._request_goal', capture_goal)
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        await service.start(case_id)
        while service.tasks:
            await asyncio.sleep(.01)
    asyncio.run(run())
    assert len(preflights) == 1
    call = next(values for kind, values in runtime.calls if kind == 'start')
    descriptor = prepare_handoff_context(Scope.model_validate(call['scope']), call['osint_context'],
                                         run_id=call['run_id'])
    _, actual_goal = real_goal(Scope.model_validate(call['scope']), descriptor)
    assert len(call['run_id']) < 64
    assert len(actual_goal) <= len(preflights[0][1]) <= MAX_GOAL_CHARS
    assert descriptor['worker_path'] in actual_goal
    assert descriptor['content_sha256'] == preflights[0][0]['content_sha256']


def test_bad_map_evidence_reference_refused_before_native_job(control, monkeypatch):
    case_id = activated(control)
    snapshot, manifest = control.checks_context(case_id)
    broken = copy.deepcopy(snapshot)
    broken['surface'][0]['evidence_ids'] = '["not-a-case-receipt"]'
    monkeypatch.setattr(control, 'checks_context', lambda case: (broken, manifest))
    runtime = Runtime()
    async def run():
        with pytest.raises(Denied, match='missing evidence'):
            await ComposeRunService(control, runtime).start(case_id)
    asyncio.run(run())
    assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []


@pytest.mark.parametrize('spent,expected_seconds', [(0.0, 300), (.25, 299), (240.01, None)])
def test_sqlite_real_spent_yields_integer_conservatively_floored_runtime_budget(control, spent, expected_seconds):
    case_id = activated(control, budget={'seconds': 300})
    with control.transaction() as db:
        db.execute('UPDATE cases SET spent=? WHERE id=?', (spent, case_id))
    assert type(control.checks_context(case_id)[0]['spent']) is float
    runtime = Runtime()
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        if expected_seconds is None:
            with pytest.raises(Denied, match='at least 60 seconds'):
                await service.start(case_id)
        else:
            await service.start(case_id)
            while service.tasks:
                await asyncio.sleep(.01)
    asyncio.run(run())
    if expected_seconds is None:
        assert runtime.calls == [] and control.get_case(case_id)['jobs'] == []
    else:
        call = next(values for kind, values in runtime.calls if kind == 'start')
        assert type(call['scope']['budget']['seconds']) is int
        assert call['scope']['budget']['seconds'] == expected_seconds
        assert expected_seconds <= 300 - spent
        Scope.model_validate(call['scope'], strict=True)
