"""Server ChatGPT campaign lane plus an operator verification lane over one case."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from security_lab.compose_service import ComposeRunService
from security_lab.compose_runtime import _request_goal, prepare_handoff_context, MAX_GOAL_CHARS
from security_lab.contracts import Denied, Scope
from security_lab.handoff import build_handoff
from security_lab.research import CampaignService, RESULT_MARKER
from test_controller import NOW, approve, scope_body, control


HOSTS = ['a.example.com', 'b.example.com', 'c.example.com']


def web_scope(**overrides):
    body = scope_body(
        targets=[{'host': host, 'paths': ['/'], 'excluded_paths': [], 'methods': ['GET', 'HEAD']} for host in HOSTS],
        budget={'seconds': 3000, 'rps': 1, 'concurrency': 2, 'jobs': 200})
    body['authorization'] = {**body['authorization'], 'hosts': HOSTS,
                             'valid_from': NOW - 1, 'valid_until': NOW + 86400}
    body.update(overrides)
    return body


def activated(control, **overrides):
    case_id = control.create_case('example.com')['id']
    control.collect_fixture(case_id)
    approve(control, case_id, web_scope(**overrides))
    return case_id


class Controlled:
    """Blocking native runtime; each start's first poll waits for release()."""
    config = SimpleNamespace(max_runtime_seconds=1800)

    def __init__(self):
        self.starts, self.stops = [], []
        self.events, self.final = {}, {}

    async def start(self, *, run_id, scope, osint_context):
        self.starts.append({'run_id': run_id, 'targets': osint_context['execution_targets'],
                            'task_id': (osint_context.get('research_task') or {}).get('task_id'),
                            'context': osint_context, 'scope': scope})
        self.events[run_id] = asyncio.Event()
        return {'status': 'running',
                'handoff': {'native_preflight': {'status': 'ready_for_scoped_checks', 'binding_match': True}}}

    async def release(self, run_id, marker=None):
        self.final[run_id] = marker
        self.events[run_id].set()

    async def poll(self, run_id):
        await self.events[run_id].wait()
        receipts = [{'type': 'command_run', 'exit_code': 0, 'output': self.final.get(run_id) or ''}]
        return {'status': 'completed', 'summary': {'status': 'completed'}, 'receipts': receipts}

    async def stop(self, run_id):
        self.stops.append(run_id)
        return {'access_closed': True, 'handoff_removed': True, 'handoff_cleanup_error': None}

    async def run_check(self, run_id, plan):
        return {'status': 'completed', 'receipts': [{'type': 'command_run', 'exit_code': 0, 'output': ''}]}

    async def run_tool(self, run_id, argv, seconds):
        return {'status': 'completed', 'receipts': [{'type': 'command_run', 'exit_code': 0, 'stdout': ''}]}


def result_line(task_id, *, targets, statement='API may expose an unauthenticated panel',
                followups=None):
    value = {'task_id': task_id,
             'hypotheses': [{'subject': 'panel', 'statement': statement, 'targets': targets,
                             'supporting_observation': 'SPA shell returned for every path',
                             'next_check': 'compare exact API route responses',
                             'would_confirm': 'real data without auth', 'would_refute': 'only a shell'}],
             'followups': followups or []}
    return RESULT_MARKER + json.dumps(value)


async def wait_for(predicate, timeout=5.0):
    for _ in range(int(timeout * 200)):
        if predicate():
            return True
        await asyncio.sleep(.005)
    return False


def test_campaign_runs_sequential_segments_imports_hypotheses_and_completes(control):
    case_id = activated(control)
    runtime = Controlled()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        campaign = CampaignService(control, service)
        started = await campaign.start(case_id, {
            'objective': 'map the dev panel surface', 'targets': ['https://a.example.com', 'https://b.example.com'],
            'max_seconds': 900, 'max_jobs': 4, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert started['status'] == 'active'
        assert await wait_for(lambda: runtime.starts)
        call = runtime.starts[0]
        assert call['task_id'] and call['targets'] == ['https://a.example.com', 'https://b.example.com']
        job_id = call['run_id'][len('chat-'):]
        assert control.get_case(case_id)['jobs'][-1]['status'] == 'running'
        await runtime.release(call['run_id'], result_line(call['task_id'], targets=call['targets']))
        assert await wait_for(lambda: not campaign.tasks)
    asyncio.run(run())
    ledger = control.research_view(case_id)
    assert ledger['campaigns'][0]['status'] == 'completed'
    assert ledger['campaigns'][0]['spent'] >= 300
    assert [h['status'] for h in ledger['hypotheses']] == ['hypothesis']
    assert ledger['hypotheses'][0]['evidence_ids'] and ledger['hypotheses'][0]['body']['targets'] == \
        ['https://a.example.com', 'https://b.example.com']
    assert ledger['campaigns'][0]['tasks'][0]['status'] == 'done'
    job = control.get_case(case_id)['jobs'][-1]
    body = json.loads(control.export(case_id)['evidence'][-1]['body'])
    assert body['lane'] == 'research' and body['research_results']


def test_research_and_operator_lanes_run_concurrently_and_conflict_on_same_host(control):
    case_id = activated(control)
    runtime = Controlled()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        campaign = CampaignService(control, service)
        await campaign.start(case_id, {'objective': 'wide pass',
            'targets': ['https://a.example.com', 'https://b.example.com'],
            'max_seconds': 900, 'max_jobs': 4, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert await wait_for(lambda: runtime.starts)
        research_run = runtime.starts[0]['run_id']
        # Operator lane on the untouched third host is admitted concurrently.
        operator = await service.start(case_id, targets=['https://c.example.com'])
        assert operator['lane'] == 'operator'
        assert {s['run_id'] for s in runtime.starts} == {research_run, operator['run_id']}
        # A concurrent call overlapping the running research host is refused.
        with pytest.raises(Denied, match='lane or target host'):
            await service.start(case_id, targets=['https://a.example.com'])
        await runtime.release(operator['run_id'], '')
        assert await wait_for(lambda: operator['id'] not in service.tasks)
        await runtime.release(research_run, result_line(runtime.starts[0]['task_id'],
            targets=runtime.starts[0]['targets']))
        assert await wait_for(lambda: not campaign.tasks)
    asyncio.run(run())
    assert control.research_view(case_id)['campaigns'][0]['status'] == 'completed'


def test_campaign_requires_concurrency_budget_idle_case_and_exact_scope(control):
    case_id = activated(control, budget={'seconds': 3000, 'rps': 1, 'concurrency': 1, 'jobs': 200})
    config = {'objective': 'wide pass', 'targets': ['https://a.example.com'], 'max_seconds': 300,
              'max_jobs': 1, 'segment_seconds': 300}
    with pytest.raises(Denied, match='concurrency'):
        control.begin_research(case_id, config, expected_revision=control.get_case(case_id)['revision'])
    two = activated(control)
    with pytest.raises(Denied, match='exceed the approved scope'):
        control.begin_research(two, {**config, 'targets': ['https://z.example.com']},
            expected_revision=control.get_case(two)['revision'])
    job, _ = control.prepare_job(two, 'fixture-worker', 'target_http', 30)
    with pytest.raises(Denied, match='idle checks case'):
        control.begin_research(two, config, expected_revision=control.get_case(two)['revision'])
    control.finish_job(two, job['id'], outcome='failed', result={}, synthetic=True)
    assert control.begin_research(two, config, expected_revision=control.get_case(two)['revision'])['status'] == 'active'


def test_hypothesis_claim_and_review_require_version_token_and_case_evidence(control):
    case_id = activated(control)
    runtime = Controlled()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        campaign = CampaignService(control, service)
        await campaign.start(case_id, {'objective': 'wide pass', 'targets': ['https://a.example.com'],
            'max_seconds': 900, 'max_jobs': 1, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert await wait_for(lambda: runtime.starts)
        run_id = runtime.starts[0]['run_id']
        await runtime.release(run_id, result_line(runtime.starts[0]['task_id'],
            targets=runtime.starts[0]['targets']))
        assert await wait_for(lambda: not campaign.tasks)
    asyncio.run(run())
    hyp = control.research_view(case_id)['hypotheses'][0]
    claim = control.claim_hypothesis(case_id, hyp['id'], hyp['version'])
    assert claim['version'] == hyp['version'] + 1
    with pytest.raises(Denied, match='version conflict'):
        control.claim_hypothesis(case_id, hyp['id'], hyp['version'])
    with pytest.raises(Denied, match='already claimed'):
        control.claim_hypothesis(case_id, hyp['id'], claim['version'])
    with pytest.raises(Denied, match='replay'):
        control.review_hypothesis(case_id, hyp['id'], claim['claim_token'], hyp['version'],
            'verified', 'confirmed', [])
    with pytest.raises(Denied, match='evidence'):
        control.review_hypothesis(case_id, hyp['id'], claim['claim_token'], claim['version'],
            'verified', 'confirmed', [])
    evidence = control.export(case_id)['evidence'][-1]['id']
    with pytest.raises(Denied, match='does not belong'):
        control.review_hypothesis(case_id, hyp['id'], claim['claim_token'], claim['version'],
            'refuted', 'no data', ['deadbeef'])
    done = control.review_hypothesis(case_id, hyp['id'], claim['claim_token'], claim['version'],
        'refuted', 'only a shell, no unauthenticated data', [evidence])
    assert done['status'] == 'refuted' and done['version'] == claim['version'] + 1
    with pytest.raises(Denied, match='replay'):
        control.review_hypothesis(case_id, hyp['id'], claim['claim_token'], done['version'],
            'refuted', 'again', [evidence])


def test_goal_and_context_carry_journal_and_instruction_but_not_authority(control):
    case_id = activated(control)
    runtime = Controlled()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        campaign = CampaignService(control, service)
        await campaign.start(case_id, {'objective': 'wide pass', 'targets': ['https://a.example.com'],
            'max_seconds': 900, 'max_jobs': 1, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert await wait_for(lambda: runtime.starts)
        await runtime.release(runtime.starts[0]['run_id'],
            result_line(runtime.starts[0]['task_id'], targets=runtime.starts[0]['targets']))
        assert await wait_for(lambda: not campaign.tasks)
    asyncio.run(run())
    # A second campaign over the same host now carries the prior journal.
    runtime2 = Controlled()

    async def run2():
        service = ComposeRunService(control, runtime2, poll_seconds=.01)
        campaign = CampaignService(control, service)
        await campaign.start(case_id, {'objective': 'wide pass', 'targets': ['https://a.example.com'],
            'max_seconds': 900, 'max_jobs': 1, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert await wait_for(lambda: runtime2.starts)
        call = runtime2.starts[0]
        data = call['context']['untrusted_osint_data']
        assert data['research_trust'] == 'model_proposals_not_verified_findings'
        assert any(row['body']['subject'] == 'panel' for row in data['research_journal'])
        brief = call['context']['research_task']
        assert brief['task_id'] == call['task_id']
        assert 'RESEARCH CAMPAIGN task ' + call['task_id'] in brief['instruction']
        assert 'SECURITY_LAB_RESEARCH_RESULT' in brief['instruction']
        assert 'no authentication, exploitation, data change, persistence or PoC' in brief['instruction']
        assert call['context']['policy']['research_rule']
        # The research brief lives in the context file; the goal stays within the cap.
        scope = Scope.model_validate(call['scope'])
        descriptor = prepare_handoff_context(scope, call['context'], run_id=call['run_id'])
        assert descriptor['content_sha256']
        _, goal = _request_goal(scope, descriptor, targets=call['targets'])
        assert len(goal) <= MAX_GOAL_CHARS and 'research' not in goal.lower()
        await runtime2.release(call['run_id'], '')
        assert await wait_for(lambda: not campaign.tasks)
    asyncio.run(run2())


def test_pause_records_status_without_starting_anything(control):
    case_id = activated(control)
    runtime = Controlled()

    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        campaign = CampaignService(control, service)
        await campaign.start(case_id, {'objective': 'wide pass', 'targets': ['https://a.example.com'],
            'max_seconds': 900, 'max_jobs': 1, 'segment_seconds': 300},
            expected_revision=control.get_case(case_id)['revision'])
        assert await wait_for(lambda: runtime.starts)
        paused = await campaign.pause(case_id)
        assert paused['campaigns'][0]['status'] == 'paused'
        assert not campaign.tasks
        assert runtime.stops
    asyncio.run(run())
    assert control.research_view(case_id)['campaigns'][0]['status'] == 'paused'
