import asyncio
import json
import os
from pathlib import Path
import stat
import time

import pytest
from pydantic import ValidationError

from security_lab.contracts import ActionPlan, Denied, Scope
from security_lab.native_checks import compile_native_check, parse_native_check
from security_lab.compose_service import ComposeRunService
from security_lab.web_sessions import WEB_SESSION_PROGRAM
from test_action_execution import prepare, resolve, ActionRuntime
from test_controller import control, NOW, scope_body


def manifest():
    body=scope_body(targets=[{'host':'example.com','paths':['/public'],
        'excluded_paths':['/public/denied'],'methods':['GET','POST','PATCH','PUT','DELETE']}],
        network_targets=[{'host':'example.com','tcp_ports':[443],'addresses':['172.20.1.3']}])
    body['authorization']['actions']=['checks','poc']
    return Scope.model_validate(body)


def request(tool):
    if tool=='writes':
        return {'tool':'writes','url':'https://example.com/',
            'steps':[{'name':'create','method':'POST','path':'/public/items','json_body':{'name':'fixture'},'capture':{'item':'/id'}}],
            'cleanup':[{'name':'remove','method':'DELETE','path':'/public/items/{item}','expect_status':204}]}
    return {'tool':tool,'url':'https://example.com/public/socket'}


@pytest.mark.parametrize('tool',['websocket','socketio','writes'])
def test_interactive_is_immutable_action_only_and_case_bound(tool):
    data=request(tool)
    with pytest.raises(Denied,match='activated exact action'):
        compile_native_check(data,manifest(),['172.20.1.3'],stage='checks_review',now=NOW)
    plan=compile_native_check(data,manifest(),['172.20.1.3'],stage='actions_approved',now=NOW,
                              task_stage='poc',case_id='a'*32)
    assert plan.argv[:2]==('/opt/web-tools-venv/bin/python','/usr/local/libexec/pentestgpt-interactive.py')
    policy=json.loads(plan.argv[-1])
    assert policy['case_id']=='a'*32 and policy['target']['excluded_paths']==['/public/denied']
    with pytest.raises(ValidationError):
        parse_native_check({**data,'headers':{'authorization':'never-in-model-input'}})
    with pytest.raises(ValidationError):
        parse_native_check({**data,'session_ref':'../private'})


def test_workflow_all_paths_methods_cleanup_and_bindings_are_admitted():
    base=request('writes')
    for change in [
        {'cleanup':[{'name':'remove','method':'DELETE','path':'/outside'}]},
        {'cleanup':[{'name':'remove','method':'DELETE','path':'/public/denied'}]},
        {'steps':[{'name':'bad','method':'POST','path':'/public/{not_captured}'}]},
        {'steps':[{'name':'bad','method':'POST','path':'/public/items','headers':{'host':'other.test'}}]},
    ]:
        with pytest.raises((Denied,ValidationError)):
            compile_native_check({**base,**change},manifest(),['172.20.1.3'],stage='actions_approved',now=NOW,
                                  task_stage='poc',case_id='a'*32)
    with pytest.raises(ValidationError): parse_native_check({**base,'cleanup':[]})


def test_socketio_polling_needs_post_and_reserved_events_are_rejected():
    scope=manifest().model_dump(mode='json')
    scope['targets'][0]['methods']=['GET']
    with pytest.raises(Denied,match='polling'):
        compile_native_check({**request('socketio'),'transport':'polling_upgrade'},Scope.model_validate(scope),
            ['172.20.1.3'],stage='actions_approved',now=NOW,task_stage='poc',case_id='a'*32)
    with pytest.raises(ValidationError):
        parse_native_check({**request('socketio'),'events':[{'name':'disconnect'}]})
    assert parse_native_check({'tool':'websocket','url':'wss://example.com/public/ws'}).url=='https://example.com:443/public/ws'


@pytest.mark.parametrize('tool',['websocket','socketio','writes'])
def test_new_profiles_use_existing_action_lifecycle_and_cannot_be_replayed(control,tool):
    case,previous=prepare(control,activate=False)
    # The fixture case grants /public POST; use an explicit non-persistent echo workflow.
    data=request(tool)
    if tool=='writes':
        data['cleanup_required']=False
        data['cleanup']=[]
    plan=control.propose(case,'actions',{'scope_version':1,'expires_at':NOW+300,'actions':[{
        'finding_id':'INTERACTIVE','host':'example.com','description':'Owned acceptance action',
        'expected_effect':'One bounded protocol observation','stop_condition':'End at limits',
        'request':data}]})
    control.activate_from_task(case,plan['id'],'actions')
    runtime=ActionRuntime()
    async def run():
        service=ComposeRunService(control,runtime,resolver=resolve)
        job=await service.start_action(case,plan['id'],'INTERACTIVE')
        while service.tasks: await asyncio.sleep(.01)
        assert control.get_job_status(case,job['id'])['evidence_id']
        with pytest.raises(Denied,match='already attempted'):
            await service.start_action(case,plan['id'],'INTERACTIVE')
        async def no_dns(host): raise AssertionError('Checks cannot reach DNS')
        with pytest.raises(Denied,match='activated exact action'):
            await ComposeRunService(control,runtime,resolver=no_dns).start_check(case,data)
    asyncio.run(run())
    saved=next(p for kind,p in runtime.calls if kind=='run_check')
    assert saved.case_id==case and saved.task_stage=='poc'


@pytest.mark.skipif(os.name!='posix',reason='Private session broker uses POSIX no-follow dirfds')
def test_private_session_grants_are_exact_case_origin_expiry_and_never_in_artifacts(tmp_path):
    ns={}
    exec(WEB_SESSION_PROGRAM,ns)
    runs=tmp_path/'runs'; runs.mkdir()
    grants=tmp_path/'web-sessions'; grants.mkdir(mode=0o700)
    owner=__import__('secrets').token_hex(24)
    p={'owner':owner,'run_id':'owned','kind':'websocket','session_ref':'test_session',
       'web_case_id':'a'*32,'web_origin':'https://example.com:443','runs_directory':str(runs)}
    data={'schema_version':1,'case_ids':['a'*32],'origins':['https://example.com:443'],
          'expires_at':time.time()+300,'headers':{'authorization':'Bearer fixture-secret'}}
    path=grants/'test_session.json'
    path.write_text(json.dumps(data)); path.chmod(0o600)
    options=ns['prepare_web_session'](p)
    assert 'fixture-secret' not in str(options) and not list(runs.iterdir())
    assert options[-1].endswith(':/run/web-session.json:ro')
    assert ns['release_web_session'](p)
    assert not ns['web_session_root'](p).exists()
    for change in [{'web_case_id':'b'*32},{'web_origin':'https://other.test:443'}]:
        with pytest.raises(RuntimeError,match='web_session_not_authorized'):
            ns['prepare_web_session'](p|change)
    path.chmod(0o644)
    with pytest.raises(RuntimeError,match='web_session_permissions'): ns['prepare_web_session'](p)


def test_historical_http_action_serialization_remains_identical():
    from security_lab.contracts import digest
    old={'schema_version':1,'scope_version':1,'expires_at':NOW+300,'actions':[{
        'finding_id':'OLD','host':'example.com','description':'legacy','expected_effect':'legacy','stop_condition':'legacy','seconds':300}]}
    assert digest(old)==digest(ActionPlan.model_validate(old).model_dump(mode='json'))
