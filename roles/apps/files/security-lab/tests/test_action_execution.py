"""Saved-action lifecycle; all execution here is an injected offline runtime."""
import asyncio
import json

import pytest

from security_lab.compose_service import ComposeRunService
from security_lab.contracts import Denied, HTTPRequest, Scope, digest
from security_lab.native_checks import compile_native_check, evaluate_action_response
from test_controller import control, NOW, approve, scope_body
from test_compose_service import Runtime


def executable_action():
    return {'finding_id': 'F02', 'host': 'example.com', 'description': 'Controlled test response',
            'expected_effect': 'One response containing the test marker', 'stop_condition': 'Stop after one response',
            'seconds': 30, 'request': {'tool': 'http', 'url': 'https://example.com/public/echo', 'method': 'POST',
                'headers': {'content-type': 'application/json'}, 'body': '{"value":"owned-marker"}', 'seconds': 15},
            'verification': {'status': 200, 'contains': 'owned-marker'}}


def prepare(control, *, activate=True):
    case = control.create_case('example.com')['id']
    body = scope_body(targets=[{'host': 'example.com', 'paths': ['/public'], 'methods': ['GET', 'HEAD', 'OPTIONS', 'POST']}],
                      network_targets=[{'host': 'example.com', 'tcp_ports': [443], 'addresses': ['172.20.1.3']}])
    body['authorization']['actions'] = ['checks', 'poc']
    approve(control, case, body)
    job, token = control.prepare_job(case, 'fixture', 'target_http', 1)
    # Real service tests cover worker admission; the initial checks review is
    # created by a completed synthetic checks job, not an action approval.
    control.finish_job(case, job['id'], outcome='failed', result={'synthetic': True}, synthetic=True)
    plan = control.propose(case, 'actions', {'scope_version': 1, 'expires_at': NOW + 300,
                                           'actions': [executable_action()]})
    if activate:
        control.activate_from_task(case, plan['id'], 'actions')
    return case, plan


async def resolve(host):
    return ['172.20.1.3']


class ActionRuntime(Runtime):
    async def run_check(self, *, run_id, plan):
        self.calls.append(('run_check', plan))
        return {'status': 'completed', 'receipts_truncated': False, 'receipts': [{'exit_code': 0,
            'stdout': 'HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n\r\nowned-marker\nSECURITY_LAB_HTTP_STATUS:200\n'}]}


def test_only_activated_immutable_request_executes_and_is_not_automatically_replayed(control):
    case, action = prepare(control, activate=False)
    runtime = ActionRuntime()
    async def run():
        service = ComposeRunService(control, runtime, resolver=resolve)
        with pytest.raises(Denied, match='current approved action'):
            await service.start_action(case, action['id'], 'F02')
        assert runtime.calls == []
        control.activate_from_task(case, action['id'], 'actions')
        launched = await service.start_action(case, action['id'], 'F02')
        while service.tasks:
            await asyncio.sleep(.01)
        assert control.get_job_status(case, launched['id'])['outcome'] == 'completed'
        with pytest.raises(Denied, match='already attempted'):
            await service.start_action(case, action['id'], 'F02')
    asyncio.run(run())
    request = next(plan for kind, plan in runtime.calls if kind == 'run_check')
    assert request.task_stage == 'poc' and '--data-raw' in request.argv
    evidence = json.loads(control.export(case)['evidence'][-1]['body'])
    assert evidence['action_manifest_id'] == action['id'] and evidence['action_hash'] == action['hash']
    assert evidence['finding_id'] == 'F02' and evidence['poc_observation'] == 'expected_observation_matched'
    assert evidence['vulnerability_verified'] is False and evidence['verified_vulnerabilities'] == []
    assert evidence['access_closed'] is True
    assert control.get_case(case)['state'] == 'checks_review'


def test_plain_checks_cannot_execute_post_even_if_http_scope_lists_post(control):
    case, action = prepare(control, activate=False)
    async def run():
        with pytest.raises(Denied, match='POST requires'):
            await ComposeRunService(control, ActionRuntime(), resolver=resolve).start_check(case, executable_action()['request'])
    asyncio.run(run())
    assert len(control.get_case(case)['jobs']) == 1


def test_action_scope_change_during_dns_refuses_execution_before_runtime(control):
    case, action = prepare(control)
    runtime = ActionRuntime()
    async def race(host):
        body = scope_body()
        approve(control, case, body)
        return ['172.20.1.3']
    async def run():
        with pytest.raises(Denied):
            await ComposeRunService(control, runtime, resolver=race).start_action(case, action['id'], 'F02')
    asyncio.run(run())
    assert runtime.calls == []


def test_action_stop_joins_runtime_and_does_not_resume_poc(control):
    case, action = prepare(control)
    class HeldRuntime(ActionRuntime):
        async def run_check(self, **kwargs):
            self.calls.append(('run_check', kwargs['plan']))
            await asyncio.Event().wait()
    runtime = HeldRuntime()
    async def run():
        service = ComposeRunService(control, runtime, resolver=resolve)
        job = await service.start_action(case, action['id'], 'F02')
        control.stop(case)
        await service.stop_case(case)
        assert control.get_job_status(case, job['id'])['outcome'] == 'interrupted'
    asyncio.run(run())
    assert runtime.calls[-1][0] == 'stop' and control.get_case(case)['state'] == 'paused'
    assert control.resume(case)['state'] == 'scope_approved'
    with pytest.raises(Denied, match='activated actions stage'):
        control.action_context(case, action['id'], 'F02')


def test_action_expiration_denies_before_dns_and_job_reservation(control):
    case, action = prepare(control)
    control.clock = lambda: NOW + 301
    runtime = ActionRuntime()
    async def unexpected(host):
        raise AssertionError('Expired action must not resolve a host')
    async def run():
        with pytest.raises(Denied, match='expired'):
            await ComposeRunService(control, runtime, resolver=unexpected).start_action(case, action['id'], 'F02')
    asyncio.run(run())
    assert runtime.calls == [] and len(control.get_case(case)['jobs']) == 1


def test_response_marker_in_headers_cannot_satisfy_body_predicate():
    receipt = {'status': 'completed', 'receipts': [{'exit_code': 0,
        'stdout': 'HTTP/1.1 200 OK\nX-Info: owned-marker\n\nunrelated\nSECURITY_LAB_HTTP_STATUS:200\n'}]}
    assert evaluate_action_response(receipt, {'contains': 'owned-marker'})['poc_observation'] == 'expected_observation_not_matched'


def test_real_mcp_protocol_can_execute_only_the_saved_request(tmp_path):
    import time
    from mcp.shared.memory import create_connected_server_and_client_session
    from security_lab.mcp_server import build_server
    async def run():
        runtime = ActionRuntime()
        server = build_server(tmp_path / 'protocol-cases', 'operator', pentest_runtime=runtime, pentest_resolver=resolve)
        async with create_connected_server_and_client_session(server) as sdk:
            async def call(name, arguments):
                result = await sdk.call_tool(name, arguments)
                assert not result.isError, result.content
                return result.structuredContent
            case = (await call('create_case', {'seed':'example.com'}))['id']
            now = int(time.time())
            body = scope_body(targets=[{'host':'example.com','paths':['/public'],'methods':['GET','POST']}],
                network_targets=[{'host':'example.com','tcp_ports':[443],'addresses':['172.20.1.3']}], expires_at=now+600)
            body['authorization'].update(valid_from=now-1,valid_until=now+600,actions=['checks','poc'])
            scope = await call('propose_scope', {'case_id':case,'manifest':body})
            await call('activate_scope_from_task', {'case_id':case,'manifest_id':scope['id']})
            job = await call('start_service_check', {'case_id':case,'specification':{'tool':'http','url':'https://example.com/public'}})
            for _ in range(100):
                status = await call('get_job_status', {'case_id':case,'job_id':job['id']})
                if status['evidence_id']:
                    break
                await asyncio.sleep(.01)
            plan = await call('propose_actions', {'case_id':case,'manifest':{'scope_version':1,'expires_at':now+300,
                                                                         'actions':[executable_action()]}})
            await call('activate_actions_from_task', {'case_id':case,'manifest_id':plan['id']})
            job = await call('start_poc_action', {'case_id':case,'manifest_id':plan['id'],'finding_id':'F02'})
            for _ in range(100):
                status = await call('get_job_status', {'case_id':case,'job_id':job['id']})
                if status['evidence_id']:
                    break
                await asyncio.sleep(.01)
            evidence = await call('get_source_evidence', {'case_id':case,'receipt_id':status['evidence_id'],'offset':0})
            assert json.loads(evidence['body'])['poc_observation'] == 'expected_observation_matched'
            repeated = await sdk.call_tool('start_poc_action', {'case_id':case,'manifest_id':plan['id'],'finding_id':'F02'})
            assert repeated.isError
    asyncio.run(run())


def test_old_descriptive_actions_keep_their_hash_and_cannot_be_executed(control):
    case, action = prepare(control)
    from security_lab.contracts import ActionPlan
    old = {'scope_version': 1, 'expires_at': NOW + 300, 'actions': [executable_action()]}
    del old['actions'][0]['request']
    del old['actions'][0]['verification']
    old['schema_version'] = 1
    assert digest(old) == digest(ActionPlan.model_validate(old).model_dump(mode='json'))


@pytest.mark.parametrize('change', [{'receipts_truncated': True}, {'status': 'failed'}, {'receipts': []}])
def test_partial_or_failed_receipt_never_matches_expected_poc(change):
    receipt = {'status': 'completed', 'receipts_truncated': False, 'receipts': [{'exit_code': 0,
        'stdout': 'HTTP/1.1 200 OK\n\nowned-marker\nSECURITY_LAB_HTTP_STATUS:200\n'}]}
    assert evaluate_action_response({**receipt, **change}, {'status': 200, 'contains': 'owned-marker'})['poc_observation'] == 'inconclusive'


def test_api_parameters_are_separate_arguments_and_cannot_override_host_headers_or_redirect_policy():
    from pydantic import ValidationError
    scope = Scope.model_validate(scope_body(targets=[{'host': 'example.com', 'paths': ['/public'], 'methods': ['OPTIONS', 'GET']}]))
    spec = {'tool': 'http', 'url': 'https://example.com/public/api', 'query': {'next': 'https://other.test/a', 'q': 'one & two'},
            'headers': {'Accept': 'application/json'}, 'method': 'GET'}
    plan = compile_native_check(spec, scope, ['8.8.8.8'], stage='checks', now=NOW)
    assert plan.argv[-1].endswith('next=https%3A%2F%2Fother.test%2Fa&q=one+%26+two')
    assert '--location' not in plan.argv and 'example.com:443:8.8.8.8' in plan.argv
    for key in ('Host', 'Authorization', 'Cookie', 'Proxy-Authorization'):
        with pytest.raises(ValidationError):
            HTTPRequest.model_validate({**spec, 'headers': {key: 'invalid'}})
    with pytest.raises(ValidationError):
        HTTPRequest.model_validate({**spec, 'headers': {'Origin': 'https://a.test\r\nHost: other'}})
