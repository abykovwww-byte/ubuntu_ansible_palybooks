import asyncio
import json
from types import SimpleNamespace

from security_lab.compose_runtime import ComposePentestRuntime
from test_compose_runtime import config, Runner
from test_compose_runtime import Docker, execute_host, payload
from security_lab.native_inventory import INVENTORY_PROGRAM
from security_lab.compose_runtime import _SSH_HOST_PROGRAM, _HOST_SCRIPT


def test_inventory_uses_only_clean_tools_and_reports_deployed_capability_without_provider(tmp_path):
    class InventoryRunner(Runner):
        async def __call__(self, command, *, payload, timeout):
            value = json.loads(payload)
            self.calls.append((command, value, timeout))
            if value['operation'] == 'stop':
                result = {'access_closed':True}
            else:
                data = {'tools':{'curl':{'available':True}},'launcher':{'native_targets_revision':1},
                        'target_requests':0,'provider_calls':0}
                result = {'status':'completed','access_closed':True,'native_service_applied':False,
                          'receipts':[{'stdout':json.dumps(data),'exit_code':0}]}
            return 0, json.dumps({'ok':True,'result':result}).encode(), b''
    runner = InventoryRunner()
    result = asyncio.run(ComposePentestRuntime(config(tmp_path), runner=runner).inventory())
    assert result['launcher']['native_targets_revision'] == 1 and result['native_service_applied'] is False
    assert result['access_closed'] and result['target_requests'] == result['provider_calls'] == 0
    assert [request['operation'] for _,request,_ in runner.calls] == ['tool','stop']
    assert runner.calls[0][1]['kind'] == 'inventory' and runner.calls[0][1]['tool_service'] == 'pentest-tools'


def test_zap_inventory_has_writable_home_and_reports_scanner_not_java_version(monkeypatch, capsys):
    commands = []
    def run(argv, **kwargs):
        commands.append(argv)
        if argv[0] == '/usr/local/bin/pentestgpt-run':
            output = '{"native_targets_revision":2}'
        elif argv[0] == '/opt/zap/zap.sh':
            assert argv[argv.index('-dir')+1].startswith('/tmp/')
            output = 'Found Java version 17.0.20.1\nUsing JVM args: -Xmx256m\n2.17.0\n'
        else:
            output = 'fixture version'
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr('subprocess.run', run)
    exec(INVENTORY_PROGRAM, {})
    tools = json.loads(capsys.readouterr().out)['tools']
    assert tools['zap'] == {'available':True, 'version':['2.17.0']}


def test_inventory_uses_web_tmpfs_when_applied_and_falls_back_on_old_compose(monkeypatch, tmp_path):
    request = payload(tmp_path, 'tool')
    request.update(kind='inventory',argv=['fixed-inventory'],seconds=60)
    class WebDocker(Docker):
        def run(self, command, **kwargs):
            result = super().run(command, **kwargs)
            if command[1] == 'compose' and 'config' in command:
                result.stdout = json.dumps({'services':{'pentest-web-tools':{},'pentestgpt-native':{}}})
            return result
    docker = WebDocker(request)
    result = execute_host(monkeypatch, request, docker)
    assert result['ok'] and result['result']['web_service_applied']
    assert 'pentest-web-tools' in next(c for c,_ in docker.calls if '--entrypoint' in c)
    legacy = Docker(request)
    result = execute_host(monkeypatch, request, legacy)
    assert result['ok'] and not result['result']['web_service_applied']
    assert 'pentest-tools' in next(c for c,_ in legacy.calls if '--entrypoint' in c)


def test_ssh_compact_transport_executes_the_exact_fixed_host_program():
    executed = []
    # Capture compile's source rather than executing Docker/network operations.
    namespace = {'compile': lambda source, filename, mode: executed.append(source) or compile('pass', filename, mode)}
    exec(_SSH_HOST_PROGRAM, namespace)
    assert executed[0] == _HOST_SCRIPT.encode('utf-8')
