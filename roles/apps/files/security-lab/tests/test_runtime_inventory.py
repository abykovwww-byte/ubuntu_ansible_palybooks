import asyncio
import json
import subprocess
from types import SimpleNamespace

from security_lab.compose_runtime import ComposePentestRuntime
from security_lab.native_inventory import INVENTORY_PROGRAM
from test_compose_runtime import config, Runner


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


def test_stock_testssl_inventory_extracts_version_instead_of_empty_banner(monkeypatch,capsys):
    def run(argv,**kwargs):
        if argv[0] == '/usr/local/bin/testssl':
            text = '\n\x1b[1m\n########\x1b[m\n  testssl.sh version 3.2.3 from https://testssl.sh/\n'
        elif argv[0] == '/usr/local/bin/pentestgpt-run':
            text = '{"native_targets_revision":2}'
        else:
            text = 'actual local tool version\n'
        return SimpleNamespace(returncode=0,stdout=text)
    monkeypatch.setattr(subprocess,'run',run)
    exec(compile(INVENTORY_PROGRAM,'trusted-inventory','exec'),{})
    result = json.loads(capsys.readouterr().out)
    assert result['tools']['testssl'] == {'available':True,'version':['testssl.sh version 3.2.3 from https://testssl.sh/']}
    assert {'testssl','openssl','tcpdump','nft'} <= result['tools'].keys()
    assert result['target_requests'] == result['provider_calls'] == 0
