import asyncio
import json
import time

import pytest
from pydantic import ValidationError

from security_lab.compose_runtime import ComposePentestRuntime, _SSH_HOST_PROGRAM, _HOST_SCRIPT
from security_lab.compose_service import ComposeRunService
from security_lab.contracts import Denied, Scope
from security_lab.native_checks import compile_native_check, parse_native_check
from test_native_checks import scoped
from test_compose_runtime import config, Runner
from test_compose_service import activated, Runtime
from test_controller import control, NOW


def compile_tls(**changes):
    return compile_native_check({'tool':'tls','host':'example.com',**changes},scoped(),
                                ['172.20.1.3'],stage='checks',now=NOW)


def test_openssl_uses_separate_ip_sni_and_certificate_identity():
    plan = compile_tls(sni=None,alpn=['h2','http/1.1'],min_version='TLSv1.3',max_version='TLSv1.3',
                       ciphersuites='TLS_AES_128_GCM_SHA256')
    argv = plan.argv
    assert argv[argv.index('-connect')+1] == '172.20.1.3:443'
    assert '-noservername' in argv and '-servername' not in argv
    assert argv[argv.index('-verify_hostname')+1] == 'example.com'
    assert argv[argv.index('-alpn')+1] == 'h2,http/1.1'
    assert argv[argv.index('-ciphersuites')+1] == 'TLS_AES_128_GCM_SHA256'
    assert '-verify_return_error' in argv and '-ign_eof' in argv
    assert all(flag in argv for flag in ('-brief','-state','-msg','-debug'))


@pytest.mark.parametrize('changes',[
    {'min_version':'TLSv1.3','max_version':'TLSv1.2'}, {'ciphers':'DEFAULT:@SECLEVEL=0'},
    {'ciphersuites':'-provider evil'}, {'sni':'-noservername'}, {'alpn':['h2','h2']},
    {'attempts':4}, {'attempt_seconds':61}, {'seconds':181}, {'argv':['/bin/sh']},
    {'capture_interface':'any'}, {'image':'untrusted'}, {'verify':False},
])
def test_tls_rejects_unsafe_or_ambiguous_parameters(changes):
    with pytest.raises((ValidationError,ValueError)):
        compile_tls(**changes)


def test_alternate_sni_needs_explicit_service_and_same_ip_grant():
    with pytest.raises(Denied,match='Alternate SNI'):
        compile_tls(sni='alias.example.com')
    body = scoped().model_dump(mode='json')
    body['authorization']['hosts'].append('alias.example.com')
    body['network_targets'].append({'host':'alias.example.com','tcp_ports':[443],'addresses':['172.20.1.4']})
    for allowed in (False,True):
        if allowed:
            body['network_targets'][-1]['addresses'] = ['172.20.1.3']
        def compile_alias():
            return compile_native_check({'tool':'tls','host':'example.com','sni':'alias.example.com'},
                Scope.model_validate(body),['172.20.1.3'],stage='checks',now=NOW)
        if allowed:
            assert '-servername' in compile_alias().argv
        else:
            with pytest.raises(Denied,match='same explicit address'):
                compile_alias()


@pytest.mark.parametrize('checks,flag',[('protocols','--protocols'),('server_defaults','--server-defaults')])
def test_testssl_never_runs_default_full_scan_or_unpinned_dns(checks,flag):
    plan = compile_native_check({'tool':'testssl','host':'example.com','checks':checks},scoped(),
                                ['172.20.1.3'],stage='checks',now=NOW)
    assert plan.argv[0] == '/usr/local/bin/testssl' and flag in plan.argv
    assert plan.argv[plan.argv.index('--ip')+1] == '172.20.1.3'
    assert plan.argv[plan.argv.index('--nodns')+1] == 'none'
    assert plan.argv[plan.argv.index('--openssl')+1] == '/usr/bin/openssl'
    assert '--connect-timeout' in plan.argv and '--socket-timeout' not in plan.argv
    assert '-4' in plan.argv
    assert not {'--full','--vulnerable','--phone-out','--proxy','--file'} & set(plan.argv)
    with pytest.raises(ValidationError):
        parse_native_check({'tool':'testssl','host':'example.com','checks':'full'})


def test_runtime_passes_original_deadline_and_bounds_attempts_before_host_launch(tmp_path):
    body = scoped().model_dump(mode='json')
    now = time.time()
    body['expires_at'] = int(now)+600
    body['authorization'].update(valid_from=int(now)-1,valid_until=int(now)+600)
    plan = compile_native_check({'tool':'tls','host':'example.com','attempts':2,'seconds':90},
        Scope.model_validate(body),['172.20.1.3'],stage='checks',now=now)
    runner = Runner()
    runtime = ComposePentestRuntime(config(tmp_path),runner=runner)
    deadline = now+80
    result = asyncio.run(runtime.run_check(run_id='owned-tls',plan=plan,deadline=deadline))
    request = runner.calls[0][1]
    assert request['deadline'] == deadline and request['attempts'] == 2
    assert request['address'] == '172.20.1.3' and request['port'] == 443
    assert result['scope_enforcement'] == 'per_run_nft_pinned_tcp_endpoint'
    assert not result['provider_called'] and not result['native_full_access']
    for deadline in (now-1,now+91,float('nan')):
        with pytest.raises(Denied,match='deadline|expired'):
            asyncio.run(runtime.run_check(run_id='bad-deadline',plan=plan,deadline=deadline))
    assert len(runner.calls) == 1


def test_host_script_fits_windows_ssh_and_decompresses_to_exact_trusted_program():
    import ast
    import base64
    import zlib
    # Verify the constant instead of running the host program or reading auth.
    tree = ast.parse(_SSH_HOST_PROGRAM)
    encoded = next(node.value for node in ast.walk(tree) if isinstance(node,ast.Constant)
                   and isinstance(node.value,str) and len(node.value)>1000)
    assert zlib.decompress(base64.b64decode(encoded)).decode('utf-8') == _HOST_SCRIPT
    assert len(_SSH_HOST_PROGRAM) < 20000


def test_interrupted_diagnostic_recovers_artifact_refs_from_stop(control):
    case = activated(control,network_targets=[{'host':'example.com','tcp_ports':[443],
                                             'addresses':['172.20.1.3']}])
    ready = asyncio.Event()
    class Interrupted(Runtime):
        async def run_check(self,*,run_id,plan,deadline):
            ready.set()
            await asyncio.Event().wait()
        async def stop(self,run_id):
            return {'access_closed':True,'artifacts':[{'name':'traffic.pcap','sha256':'a'*64}],
                    'target_receiving_side_capture':False}
    async def resolve(host):
        return ['172.20.1.3']
    async def run():
        service = ComposeRunService(control,Interrupted(),resolver=resolve)
        job = await service.start_check(case,{'tool':'tls','host':'example.com'})
        await ready.wait()
        await service.stop_case(case)
        assert control.get_job_status(case,job['id'])['outcome'] == 'interrupted'
    asyncio.run(run())
    result = json.loads(control.export(case)['evidence'][-1]['body'])
    assert result['access_closed'] and result['artifacts'][0]['name'] == 'traffic.pcap'
    assert json.loads(result['receipt'])['closure']['target_receiving_side_capture'] is False
