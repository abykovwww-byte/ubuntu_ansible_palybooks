"""Regression for the real native run that stopped at facade-only gates."""
import asyncio
import copy
import json

import pytest

from security_lab.compose_runtime import ComposePentestRuntime, prepare_handoff_context, _request_goal
from security_lab.contracts import Denied, Scope, digest
from security_lab.handoff import build_handoff
from security_lab.native_handoff import build_native_handoff, validate_native_handoff
from test_controller import control, approve, scope_body, NOW
from test_compose_runtime import Runner, config


TARGETS = ['tcp://example.com:5432']
WORKER = 'existing-pentestgpt-compose'


def reserved(control, *, seconds=90, total=300, jobs=3, spent=10.5):
    case_id = control.create_case('example.com')['id']
    control.collect_fixture(case_id)
    approve(control, case_id, scope_body(targets=[], network_targets=[{'host':'example.com','tcp_ports':[5432]}],
                                         budget={'seconds':total,'jobs':jobs}))
    with control.transaction() as db:
        db.execute('UPDATE cases SET spent=? WHERE id=?', (spent,case_id))
    snapshot, manifest = control.checks_context(case_id)
    typed = build_handoff(snapshot, manifest, [], now=NOW)
    job, token = control.prepare_job(case_id, WORKER, 'target_network', seconds)
    run_id = 'chat-' + job['id']
    record = control.native_execution_record(case_id, job['id'], token, WORKER, 'target_network', run_id, TARGETS)
    context = build_native_handoff(typed, record)
    return case_id, job, token, run_id, manifest, typed, context


def test_real_controller_admission_replaces_facade_gates_with_exact_remaining_allowance(control):
    case, job, token, run, manifest, typed, native = reserved(control)
    assert typed['execution_authorized'] is False and typed['available_tools'] == []
    assert 'qualified workers' in typed['policy']['execution_rule']
    original_typed = copy.deepcopy(typed)
    record = validate_native_handoff(native, run_id=run, now=NOW)
    assert record.admission == 'controller_reserved' and record.job_id == job['id']
    assert record.scope_hash == manifest['hash'] and record.generation == job['generation']
    assert record.deadline == job['deadline'] and record.reserved_seconds == record.remaining_seconds_at_snapshot == 90
    assert record.case_allowance.spent_or_reserved_seconds == 100.5
    assert record.case_allowance.unallocated_seconds == 199
    assert record.case_allowance.used_or_reserved_jobs == 1 and record.case_allowance.unallocated_jobs == 2
    assert 'execution_authorized' not in native and 'available_tools' not in native
    assert record.toolbox == 'existing_pentestgpt_native_shell'
    assert record.independent_network_enforcement is record.independent_tool_qualification is False
    assert typed == original_typed  # facade remains non-executable and unmodified
    serialized = json.dumps(native)
    assert token not in serialized and 'token_hash' not in serialized and 'lease_until' not in serialized


def test_last_case_allocation_still_allows_its_reserved_native_run(control, tmp_path, monkeypatch):
    case, job, token, run, manifest, typed, native = reserved(control, seconds=90, total=90, jobs=1, spent=0)
    assert native['native_execution']['case_allowance']['unallocated_seconds'] == 0
    assert native['native_execution']['case_allowance']['unallocated_jobs'] == 0
    monkeypatch.setattr('security_lab.compose_runtime.time.time', lambda:NOW+5.25)
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    asyncio.run(runtime.start(run_id=run, scope=json.loads(manifest['body']), osint_context=native))
    request = runner.calls[0][1]
    assert request['max_runtime_seconds'] == 84  # absolute deadline, including setup
    delivered = json.loads(request['context_json'])
    assert delivered['native_execution']['deadline'] == NOW+90
    assert delivered['native_execution']['job_id'] == job['id']
    assert 'Zero unallocated' in request['goal'] and 'SEPARATE typed facade' in request['goal']
    assert len(request['goal']) <= 4000


@pytest.mark.parametrize('change', [
    {'job_id':'another-job'}, {'case_id':'another-case'}, {'scope_id':'another-scope'},
    {'scope_hash':'a'*64}, {'generation':999}, {'scope_version':999},
    {'admission':'preflight_only'},
])
def test_controller_seals_only_actual_job_scope_and_accounting(control, change):
    case, job, token, run, manifest, typed, native = reserved(control)
    record = {**native['native_execution'], **change}
    plan = {'tool':'pentestgpt-agent','execution_mode':'existing_compose_runtime','reduced_scope_enforced':False,
            'run_id':run,'execution_targets':TARGETS,'native_execution':record}
    with pytest.raises(Denied, match='actual Controller'):
        control.start_native_job(job['id'],token,WORKER,'target_network',plan)
    assert control.get_job_status(case,job['id'])['status'] == 'prepared'


def test_controller_rejects_self_consistent_but_invented_remaining_budget(control):
    case, job, token, run, manifest, typed, native = reserved(control)
    value = copy.deepcopy(native['native_execution'])
    value['case_allowance']['spent_or_reserved_seconds'] = 90.0
    value['case_allowance']['unallocated_seconds'] = 210
    with pytest.raises(Denied, match='actual Controller'):
        control.start_native_job(job['id'],token,WORKER,'target_network',{
            'tool':'pentestgpt-agent','execution_mode':'existing_compose_runtime','reduced_scope_enforced':False,
            'run_id':run,'execution_targets':TARGETS,'native_execution':value})


@pytest.mark.parametrize('mode', ['typed', 'preview', 'expired', 'wrong_run', 'foreign_target'])
def test_receiving_runtime_rejects_invalid_native_packet_before_provider(control,tmp_path,monkeypatch,mode):
    case, job, token, run, manifest, typed, native = reserved(control)
    monkeypatch.setattr('security_lab.compose_runtime.time.time',lambda:NOW)
    context = copy.deepcopy(native)
    if mode == 'typed':
        context = typed
    elif mode == 'preview':
        context['native_execution']['admission'] = 'preflight_only'
    elif mode == 'expired':
        monkeypatch.setattr('security_lab.compose_runtime.time.time',lambda:NOW+91)
    elif mode == 'wrong_run':
        context['native_execution']['run_id'] = 'foreign-run'
    else:
        context['execution_targets'] = context['native_execution']['execution_targets'] = ['tcp://other.example.com:5432']
    context['context_hash'] = digest({k:v for k,v in context.items() if k != 'context_hash'})
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path),runner=runner)
    with pytest.raises(Denied, match='native execution handoff'):
        asyncio.run(runtime.start(run_id=run,scope=json.loads(manifest['body']),osint_context=context))
    assert runner.calls == [] and runtime._handles == {}


def test_revoked_reservation_cannot_produce_admission_record(control):
    case, job, token, run, manifest, typed, native = reserved(control)
    control.stop(case)
    with pytest.raises(Denied,match='revoked capability'):
        control.native_execution_record(case,job['id'],token,WORKER,'target_network',run,TARGETS)


def test_native_goal_has_one_admission_path_for_http_and_tcp(control):
    case, job, token, run, manifest, typed, native = reserved(control)
    scope = Scope.model_validate_json(manifest['body'])
    descriptor = prepare_handoff_context(scope,native,run_id=run)
    _, goal = _request_goal(scope,descriptor,targets=TARGETS)
    assert 'native_execution.admission=controller_reserved' in goal
    assert 'case_allowance' in goal and 'native_execution.deadline' in goal
    assert 'not another execution-gate task' in goal
    assert 'prompt-only' in goal and 'typed' in goal
    assert descriptor['content_sha256'] in goal and run in goal
