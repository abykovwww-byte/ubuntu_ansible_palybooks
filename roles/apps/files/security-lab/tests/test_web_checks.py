import importlib.util
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied
from security_lab.native_checks import compile_native_check, parse_native_check
from test_controller import NOW
from test_native_checks import scoped
from test_compose_runtime import Docker, execute_host, payload


WORKER = Path(__file__).resolve().parents[2] / 'pentestgpt' / 'web_check.py'
loader = importlib.util.spec_from_file_location('web_check', WORKER)
worker = importlib.util.module_from_spec(loader)
loader.loader.exec_module(worker)


@pytest.mark.parametrize('tool', ['playwright', 'ffuf', 'zap'])
def test_web_profiles_compile_no_shell_and_same_worker_admits_the_policy(tool):
    plan = compile_native_check({'tool': tool, 'url': 'https://example.com/public'}, scoped(),
                                ['172.20.1.3'], stage='checks', now=NOW)
    assert plan.profile == 'target_http' and plan.task_stage == 'checks'
    assert plan.argv[:2] == ('/opt/web-tools-venv/bin/python', '/usr/local/libexec/pentestgpt-web-check.py')
    policy = json.loads(plan.argv[-1])
    worker.validate_policy(policy)
    assert worker.permits(policy, 'https://example.com/public/api?q=1')
    for url in ('https://example.com/public/admin', 'https://example.com/publicity',
                'https://example.com/public/%2e%2e/admin', 'https://other.example.com/public',
                'https://example.com:8443/public', 'file:///public'):
        assert not worker.permits(policy, url)
    assert not worker.permits(policy, 'https://example.com/public', 'POST')
    with pytest.raises(ValidationError):
        parse_native_check({'tool': tool, 'url': 'https://example.com/public', 'script': 'arbitrary code'})


def test_ffuf_entire_wordlist_and_control_are_checked_before_dns():
    with pytest.raises(Denied, match='candidate'):
        compile_native_check({'tool': 'ffuf', 'url': 'https://example.com/public', 'paths': ['admin']},
                              scoped(), ['172.20.1.3'], stage='checks', now=NOW)
    for bad in ['../private', '/absolute', 'foo%2fbar', '-H x:y', 'a?token=x', 'a\nb']:
        with pytest.raises(ValidationError):
            parse_native_check({'tool': 'ffuf', 'url': 'https://example.com/public', 'paths': [bad]})


def test_zap_plan_imports_one_har_without_target_requests_or_active_scan(tmp_path):
    plan = compile_native_check({'tool': 'zap', 'url': 'https://example.com/public'}, scoped(),
                                ['172.20.1.3'], stage='checks', now=NOW)
    jobs = worker.zap_plan(json.loads(plan.argv[-1]), tmp_path)['jobs']
    assert [job['type'] for job in jobs] == ['passiveScan-config', 'import', 'passiveScan-wait', 'report']
    assert jobs[1]['parameters']['sendRequests'] is False


@pytest.mark.parametrize('kind', ['playwright', 'ffuf', 'zap'])
def test_host_mounts_only_fresh_job_artifacts_and_pins_hosts(monkeypatch, tmp_path, kind):
    request = payload(tmp_path, 'tool')
    Path(request['runs_directory']).mkdir()
    request.update(kind=kind, argv=['/opt/web-tools-venv/bin/python', '/usr/local/libexec/pentestgpt-web-check.py'],
                   seconds=60, web_host='example.com', web_address='2001:4860:4860::8888')
    docker = Docker(request)
    result = execute_host(monkeypatch, request, docker)
    assert result['ok'] and result['result']['access_closed']
    command = next(c for c, _ in docker.calls if '--entrypoint' in c)
    assert '--add-host' not in command
    assert command[command.index('--volume')+1] == str(Path(request['runs_directory']) / 'owned') + ':/web-artifacts:rw'
    hosts = Path(request['runs_directory']) / 'owned' / '.dns-hosts'
    assert hosts.read_text('ascii') == '127.0.0.1 localhost\n::1 localhost\n2001:4860:4860::8888 example.com\n'
    assert str(hosts)+':/etc/hosts:ro' in command
    assert 'pentest-web-tools' in command
    assert result['result']['artifact_directory'] == str(Path(request['runs_directory']) / 'owned')
    assert execute_host(monkeypatch, request, docker)['ok'] is False  # never overwrite/replay artifacts


def test_host_rejects_existing_artifact_root_before_container_launch(monkeypatch, tmp_path):
    request = payload(tmp_path, 'tool')
    request.update(kind='ffuf', argv=['fixed'], seconds=60, web_host='example.com', web_address='8.8.8.8')
    root = Path(request['runs_directory'])
    root.mkdir()
    (root / 'owned').mkdir()
    docker = Docker(request)
    assert execute_host(monkeypatch, request, docker)['ok'] is False
    assert not any('--entrypoint' in command for command, _ in docker.calls)
