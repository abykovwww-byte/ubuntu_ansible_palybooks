import asyncio
import json

import pytest

from security_lab.compose_service import ComposeRunService
from security_lab.compose_runtime import ComposePentestRuntime, _request_goal, prepare_handoff_context
from security_lab.contracts import Denied, Scope
from security_lab.native_targets import select_native_targets
from test_controller import control, scope_body
from test_compose_service import activated, Runtime
from test_compose_runtime import config, Runner, packet, scope, payload, Docker, execute_host


def test_native_mixed_selection_uses_full_original_scope_and_same_case(control):
    case = activated(control, network_targets=[{'host': 'example.com', 'tcp_ports': [22, 5432]}])
    before = control.get_case(case)
    runtime = Runtime()
    selected = ['https://example.com', 'tcp://example.com:5432']
    async def run():
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        await service.start(case, targets=selected)
        while service.tasks:
            await asyncio.sleep(.01)
    asyncio.run(run())
    call = next(value for kind, value in runtime.calls if kind == 'start')
    assert call['osint_context']['execution_targets'] == selected
    assert call['osint_context']['scope_manifest']['network_targets'][0]['tcp_ports'] == [22, 5432]
    assert control.get_case(case)['manifests'] == before['manifests']
    receipt = json.loads(control.export(case)['evidence'][-1]['body'])
    assert receipt['execution_targets'] == selected and receipt['network_scope_enforcement'] == 'prompt_only'


def test_more_than_sixteen_services_requires_explicit_focus_without_scope_rewrite(control):
    case = activated(control, network_targets=[{'host': 'example.com', 'tcp_ports': list(range(1000, 1030))}])
    async def run():
        runtime = Runtime()
        service = ComposeRunService(control, runtime, poll_seconds=.01)
        with pytest.raises(Denied, match='1..16'):
            await service.start(case)
        assert not runtime.calls and not control.get_case(case)['jobs']
        await service.start(case, targets=['tcp://example.com:1001'])
        while service.tasks:
            await asyncio.sleep(.01)
    asyncio.run(run())


def test_foreign_service_selection_is_refused_before_provider_or_budget(control):
    case = activated(control)
    runtime = Runtime()
    async def run():
        with pytest.raises(Denied, match='saved scope'):
            await ComposeRunService(control, runtime).start(case, targets=['https://other.example.com'])
    asyncio.run(run())
    assert not runtime.calls and control.get_case(case)['spent'] == 0


def test_runtime_payload_preserves_all_selected_targets_and_native_service(tmp_path):
    body = scope(targets=[{'host': 'fixture.example.test'}], network_targets=[{'host': 'fixture.example.test', 'tcp_ports': [5432]}])
    context = packet(body)
    context['execution_targets'] = ['https://fixture.example.test', 'tcp://fixture.example.test:5432']
    from security_lab.contracts import digest
    context['context_hash'] = digest({key: value for key, value in context.items() if key != 'context_hash'})
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    asyncio.run(runtime.start(run_id='mixed', scope=body, osint_context=context))
    request = runner.calls[0][1]
    assert request['targets'] == context['execution_targets']
    assert request['agent_service'] == 'pentestgpt-native'
    assert 'case_continuation' in request['goal'] and len(request['goal']) <= 4000
    assert json.loads(request['context_json'])['scope_manifest'] == context['scope_manifest']


@pytest.mark.parametrize('revision', [1, 2])
def test_host_checks_actual_launcher_before_start_and_forwards_every_target(monkeypatch, tmp_path, revision):
    from types import SimpleNamespace
    request = payload(tmp_path, 'start')
    request['targets'] = ['https://fixture.example.test', 'tcp://fixture.example.test:5432']
    request['agent_service'] = 'pentestgpt-native'
    class Store:
        writes = 0
        def __init__(self, *args):
            pass
        def write(self, value):
            Store.writes += 1
            return {'identity':{'device':1,'inode':2}}
        def verify(self, identity):
            return {}
        def release(self):
            pass
        def cleanup(self, identity):
            return True
    class NativeDocker(Docker):
        def run(self, command, **kwargs):
            if 'config' in command:
                self.calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({'services':{'pentestgpt-native':{'volumes':[
                    {'type':'bind','target':'/workspace','source':request['workspace_directory']}]}}}), stderr='')
            if '--capabilities' in command:
                self.calls.append((command, kwargs))
                return SimpleNamespace(returncode=0, stdout=json.dumps({'native_targets_revision':revision}), stderr='')
            return super().run(command, **kwargs)
    docker = NativeDocker(request)
    reply = execute_host(monkeypatch, request, docker, handoff_store=Store)
    launches = [command for command, _ in docker.calls if '--detach' in command]
    if revision == 1:
        assert reply['error_code'] == 'native_image_update_required' and not launches and Store.writes == 0
    else:
        assert reply['ok'] and len(launches) == 1 and Store.writes == 1
        command = launches[0]
        assert [command[i+1] for i, value in enumerate(command) if value == '--target'] == request['targets']
