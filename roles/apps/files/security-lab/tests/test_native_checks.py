import asyncio
from dataclasses import replace
import json
import time

import pytest
from pydantic import ValidationError

from security_lab.compose_runtime import ComposePentestRuntime
from security_lab.compose_service import ComposeRunService
from security_lab.contracts import Denied, Scope
from security_lab.native_checks import compile_native_check, parse_native_check
from test_controller import control, NOW, scope_body
from test_compose_service import activated, Runtime
from test_compose_runtime import config, Runner, payload, Docker, execute_host


def scoped():
    return Scope.model_validate(scope_body(targets=[{'host': 'example.com', 'paths': ['/public'],
                                                    'excluded_paths': ['/public/admin']}],
        network_targets=[{'host': 'example.com', 'tcp_ports': [443, 5432], 'addresses': ['172.20.1.3']}]))


@pytest.mark.parametrize('spec', [
    {'tool': 'curl', 'url': 'https://example.com/public', 'method': 'HEAD'},
    {'tool': 'nmap_service', 'host': 'example.com', 'port': 5432},
    {'tool': 'tls', 'host': 'example.com', 'port': 5432, 'starttls': 'postgres'},
])
def test_fixed_plans_pin_exact_service_and_do_not_allow_model_selected_scripts_or_shell(spec):
    plan = compile_native_check(spec, scoped(), ['172.20.1.3'], stage='checks_review', now=NOW)
    assert '172.20.1.3' in ' '.join(plan.argv)
    assert '--script' not in plan.argv and '/bin/sh' not in plan.argv
    if plan.tool == 'curl':
        assert '--resolve' in plan.argv and '--max-redirs' in plan.argv
        assert '--insecure' not in plan.argv and '-L' not in plan.argv
    elif plan.tool == 'nmap_service':
        assert '-sV' in plan.argv and '--version-light' in plan.argv
    else:
        assert plan.argv[-2:] == ('-starttls', 'postgres') and '-verify_return_error' in plan.argv
    with pytest.raises(ValidationError):
        parse_native_check({**spec, 'argv': ['/bin/sh', '-c', 'id']})


@pytest.mark.parametrize('spec', [
    {'tool': 'curl', 'url': 'https://example.com/public/admin'},
    {'tool': 'curl', 'url': 'https://example.com/outside'},
    {'tool': 'nmap_service', 'host': 'example.com', 'port': 22},
    {'tool': 'tls', 'host': 'other.example.com', 'port': 443},
])
def test_out_of_scope_paths_services_and_ports_are_denied(spec):
    with pytest.raises(Denied):
        compile_native_check(spec, scoped(), ['172.20.1.3'], stage='checks', now=NOW)


@pytest.mark.parametrize('address', ['127.0.0.1', '169.254.169.254', '172.20.1.4', '8.8.8.8'])
def test_dns_cannot_escape_approved_address_grants(address):
    with pytest.raises((Denied, ValueError)):
        compile_native_check({'tool': 'tls', 'host': 'example.com'}, scoped(), [address], stage='checks', now=NOW)


def test_http_only_scope_never_authorizes_service_probe_or_dns(control):
    case_id = activated(control)
    async def unexpected(host):
        raise AssertionError('Unauthorized request must not resolve a host')
    async def run():
        with pytest.raises(Denied, match='network host and port'):
            await ComposeRunService(control, Runtime(), resolver=unexpected).start_check(case_id,
                {'tool': 'nmap_service', 'host': 'example.com', 'port': 5432})
    asyncio.run(run())
    assert control.get_case(case_id)['jobs'] == []


@pytest.mark.parametrize('tool', ['curl', 'nmap_service', 'tls'])
def test_service_job_records_command_observation_and_closure_without_provider(control, tool):
    case_id = activated(control, network_targets=[{'host': 'example.com', 'tcp_ports': [5432],
                                                  'addresses': ['172.20.1.3']}])
    class ServiceRuntime(Runtime):
        async def run_check(self, *, run_id, plan, deadline=None):
            self.calls.append(('run_check', plan))
            return {'status': 'completed', 'receipts': [{'argv': plan.argv, 'exit_code': 0, 'stdout': 'fixture service observation'}]}
    runtime = ServiceRuntime()
    spec = {'tool': tool, 'url': 'https://example.com/public'} if tool == 'curl' else {
        'tool': tool, 'host': 'example.com', 'port': 5432}
    async def resolve(host):
        return ['172.20.1.3']
    async def run():
        service = ComposeRunService(control, runtime, resolver=resolve)
        job = await service.start_check(case_id, spec)
        while service.tasks:
            await asyncio.sleep(.01)
        assert control.get_job_status(case_id, job['id'])['outcome'] == 'completed'
    asyncio.run(run())
    body = json.loads(control.export(case_id)['evidence'][-1]['body'])
    assert body['tool'] == tool and body['provider_called'] is False
    assert body['access_closed'] is True and body['verified_vulnerabilities'] == []
    assert 'fixture service observation' in body['receipt']
    assert not any(kind == 'start' for kind, _ in runtime.calls)


def test_runtime_recompiles_plan_and_rejects_mutated_argv_before_launch(tmp_path):
    now = int(time.time())
    manifest = scoped().model_dump(mode='json')
    manifest['authorization'].update(valid_from=now - 1, valid_until=now + 600)
    manifest['expires_at'] = now + 600
    plan = compile_native_check({'tool': 'tls', 'host': 'example.com', 'port': 5432, 'starttls': 'postgres'},
        Scope.model_validate(manifest), ['172.20.1.3'], stage='checks', now=now)
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path), runner=runner)
    with pytest.raises(Denied, match='altered'):
        asyncio.run(runtime.run_check(run_id='altered', plan=replace(plan, argv=('/bin/sh', '-c', 'id'))))
    assert runner.calls == []
    result = asyncio.run(runtime.run_check(run_id='fixed', plan=plan))
    assert result['provider_called'] is False and result['native_full_access'] is False
    assert runner.calls[0][1]['argv'] == list(plan.argv)


@pytest.mark.parametrize('kind', ['curl', 'nmap_service'])
def test_actual_host_program_uses_tools_service_and_timeout(monkeypatch, tmp_path, kind):
    request = payload(tmp_path, 'tool')
    request.update(kind=kind, argv=['/usr/bin/fixed-test-tool'], seconds=10)
    docker = Docker(request)
    result = execute_host(monkeypatch, request, docker)
    assert result['ok'] and result['result']['access_closed']
    command = next(command for command, _ in docker.calls if '--entrypoint' in command)
    assert command[-5:] == ['pentest-tools', '--signal=TERM', '--kill-after=10s', '10', '/usr/bin/fixed-test-tool']
