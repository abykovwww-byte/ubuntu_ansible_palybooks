"""Execute the pinned command on the captured schema shape, not an LLM validator."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

import pytest

from security_lab.compose_runtime import _serialize_handoff, _request_goal, MAX_GOAL_CHARS
from security_lab.contracts import Scope, canonical, digest
from security_lab.native_preflight import (NATIVE_PREFLIGHT_PROGRAM, PREFLIGHT_FILENAME,
    NATIVE_PREFLIGHT_SHA256, MAX_PREFLIGHT_RECEIPT_CHARS, PREFLIGHT_GOAL_PREFIX, preflight_command)
from test_compose_runtime import native_packet, scope, payload, Docker, execute_host

REAL_RUN = subprocess.run


def test_preflight_code_is_payload_not_an_oversized_windows_ssh_argument(tmp_path):
    from security_lab.compose_runtime import ComposePentestRuntime, ComposeRuntimeConfig
    runtime=ComposePentestRuntime(ComposeRuntimeConfig(mode='ssh',compose_directory='/srv/apps/pentestgpt',
        runs_directory='/srv/app-data/pentestgpt/runs',ssh_host='operator@owned-host',
        ssh_identity='C:/Users/operator/.ssh/owned-key'))
    command=subprocess.list2cmdline(runtime._command())
    assert len(command)<32767, len(command)
    assert NATIVE_PREFLIGHT_PROGRAM not in command


def bundle(tmp_path, context=None):
    context = context or native_packet()
    text, descriptor = _serialize_handoff(Scope.model_validate(context['scope_manifest']),context,run_id='owned')
    (tmp_path/'osint-context.json').write_bytes(text.encode('utf-8'))
    (tmp_path/'descriptor.json').write_text(canonical(descriptor),encoding='utf-8')
    (tmp_path/PREFLIGHT_FILENAME).write_bytes(NATIVE_PREFLIGHT_PROGRAM.encode('utf-8'))
    argv = shlex.split(preflight_command(descriptor))
    argv[0] = sys.executable
    argv[3] = str(tmp_path/PREFLIGHT_FILENAME)  # relocate fixture, not its binding/goal pins
    return context, descriptor, argv


def test_exact_command_accepts_the_shape_that_blocked_the_real_model(tmp_path):
    packet, descriptor, argv = bundle(tmp_path)
    assert descriptor['schema_version']==1 and packet['schema_version']==2
    assert packet['native_execution']['schema_version']==1
    assert 'context_hash' not in packet['native_execution']
    for key in ('case_id','scope_id','generation','run_id','execution_targets'):
        assert key not in packet['scope_manifest'] and key not in packet['effective_scope']
    assert descriptor['scope_version']==packet['native_execution']['scope_version']
    assert descriptor['execution_targets']==packet['execution_targets']
    result = REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==0, result.stderr
    ready = json.loads(result.stdout)
    assert ready['status']=='ready_for_scoped_checks' and ready['binding_match']
    assert ready['admission']=='controller_reserved' and ready['target_requests']==0
    assert 0 < ready['remaining_run_seconds'] <= ready['reserved_seconds']
    assert len(result.stdout) <= MAX_PREFLIGHT_RECEIPT_CHARS


def test_reserved_run_with_zero_future_allowance_is_ready(tmp_path):
    context = native_packet()
    budget = context['native_execution']['case_allowance']
    budget.update(spent_or_reserved_seconds=float(budget['seconds_limit']),unallocated_seconds=0,
                  used_or_reserved_jobs=budget['jobs_limit'],unallocated_jobs=0)
    context['context_hash']=digest({k:v for k,v in context.items() if k!='context_hash'})
    _, _, argv = bundle(tmp_path,context)
    result = REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==0
    receipt=json.loads(result.stdout)
    assert receipt['unallocated_case_seconds']==receipt['unallocated_case_jobs']==0
    assert receipt['status']=='ready_for_scoped_checks'


def test_redacted_external_data_does_not_require_cyclic_snapshot_hash(tmp_path):
    context=native_packet(data={'api_key':'synthetic-sensitive-value','surface_nodes':[]})
    _, descriptor, argv=bundle(tmp_path,context)
    assert descriptor['redaction_applied']
    assert 'synthetic-sensitive-value' not in (tmp_path/'osint-context.json').read_text('utf-8')
    result=REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==0 and json.loads(result.stdout)['binding_match']


@pytest.mark.parametrize('change', [
    {'scope_version':999}, {'execution_targets':['https://foreign.example.test']},
    {'case_id':'foreign-case'}, {'job_id':'foreign-job'}, {'deadline':1}, {'schema_version':True},
])
def test_wrong_descriptor_is_blocked_without_printing_inputs(tmp_path,change):
    _, descriptor, argv=bundle(tmp_path)
    (tmp_path/'descriptor.json').write_text(canonical({**descriptor,**change}),encoding='utf-8')
    result=REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==2
    assert json.loads(result.stdout)=={'status':'blocked','error_code':'native_preflight_validation_failed','target_requests':0}


def test_context_or_checker_tampering_cannot_be_executed(tmp_path):
    _, _, argv=bundle(tmp_path)
    (tmp_path/'osint-context.json').write_text('tampered',encoding='utf-8')
    result=REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==2 and json.loads(result.stdout)['status']=='blocked'
    _, _, argv=bundle(tmp_path)
    script=tmp_path/PREFLIGHT_FILENAME
    script.write_text("raise RuntimeError('untrusted checker must not run')",encoding='utf-8')
    result=REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode!=0 and 'preflight_code_hash_mismatch' in result.stderr
    assert 'untrusted checker must not run' not in result.stderr


def test_expired_deadline_is_not_reinterpreted_as_iso_timestamp(tmp_path):
    context=native_packet(); native=context['native_execution']
    native.update(deadline=time.time()-1,reserved_at=time.time()-1-native['reserved_seconds'])
    native['reserved_at']=native['deadline']-native['reserved_seconds']
    native['snapshot_at']=native['reserved_at']+1
    native['remaining_seconds_at_snapshot']=native['reserved_seconds']-1
    context['context_hash']=digest({k:v for k,v in context.items() if k!='context_hash'})
    _, _, argv=bundle(tmp_path,context)
    result=REAL_RUN(argv,capture_output=True,text=True,timeout=15)
    assert result.returncode==2 and json.loads(result.stdout)['status']=='blocked'


@pytest.mark.skipif(os.name!='posix',reason='Actual POSIX handoff lifecycle before mocked provider launch')
def test_host_runs_real_checker_before_provider_and_appends_its_receipt(monkeypatch,tmp_path):
    request=payload(tmp_path,'start')
    class RealCheckerDocker(Docker):
        def run(self,command,**kwargs):
            if command[0]=='python3':
                self.calls.append((command,kwargs))
                return REAL_RUN([sys.executable,*command[1:]],**kwargs)
            return super().run(command,**kwargs)
    docker=RealCheckerDocker(request)
    reply=execute_host(monkeypatch,request,docker)
    assert reply['ok']
    receipt=reply['result']['handoff']['native_preflight']
    assert receipt['binding_match'] and receipt['status']=='ready_for_scoped_checks'
    assert receipt['target_requests']==0
    check_index=next(i for i,(command,_) in enumerate(docker.calls) if command[0]=='python3')
    launch_index=next(i for i,(command,_) in enumerate(docker.calls) if '--detach' in command)
    assert check_index < launch_index
    launch=docker.calls[launch_index][0]; goal=launch[launch.index('--goal')+1]
    assert goal.startswith(request['goal']+PREFLIGHT_GOAL_PREFIX)
    assert json.loads(goal.split(PREFLIGHT_GOAL_PREFIX,1)[1])==receipt
    assert len(goal)<=MAX_GOAL_CHARS
    native_file=tmp_path/'workspace/handoffs/owned'/PREFLIGHT_FILENAME
    assert native_file.stat().st_mode & 0o777==0o444
    assert hashlib.sha256(native_file.read_bytes()).hexdigest()==NATIVE_PREFLIGHT_SHA256


@pytest.mark.skipif(os.name!='posix',reason='Actual POSIX cleanup after blocked preflight')
def test_blocked_host_preflight_never_starts_provider(monkeypatch,tmp_path):
    request=payload(tmp_path,'start')
    class FailedCheckerDocker(Docker):
        def run(self,command,**kwargs):
            if command[0]=='python3':
                self.calls.append((command,kwargs))
                return subprocess.CompletedProcess(command,2,'{"status":"blocked"}','')
            return super().run(command,**kwargs)
    docker=FailedCheckerDocker(request)
    reply=execute_host(monkeypatch,request,docker)
    assert not reply['ok'] and reply['error_code']=='native_preflight_failed'
    assert not any('--detach' in command for command,_ in docker.calls)
    assert not (tmp_path/'workspace/handoffs/owned').exists()


@pytest.mark.skipif(os.name!='posix',reason='POSIX ownership guards for changed verifier files')
def test_changed_verifier_is_not_silently_deleted(tmp_path):
    from test_handoff_files import store_type
    workspace=tmp_path/'workspace'; workspace.mkdir()
    text, descriptor=_serialize_handoff(Scope.model_validate(scope()),native_packet(),run_id='owned')
    store=store_type()(str(workspace),'owned','fixture-owner',descriptor,NATIVE_PREFLIGHT_PROGRAM)
    try:
        receipt=store.write(text)
        script=workspace/'handoffs/owned'/PREFLIGHT_FILENAME
        script.chmod(0o644); script.write_text('changed by native actor',encoding='utf-8'); script.chmod(0o444)
        with pytest.raises(RuntimeError,match='preflight program changed'):
            store.verify(receipt['identity'])
        with pytest.raises(RuntimeError): store.cleanup(receipt['identity'])
        assert script.read_text('utf-8')=='changed by native actor'
    finally:
        store.release()
