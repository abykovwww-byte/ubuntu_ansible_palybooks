"""Trusted receiving-side qualification on owned internal Docker endpoints only."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from security_lab.contracts import Denied, Scope
from security_lab.controller import Controller
from security_lab.dispatcher import Dispatcher
from security_lab.direct_worker import DockerDirectBackend, LABEL, docker, remove_owned
from security_lab.http_policy import Lease
from security_lab.target_resolution import TargetResolver
from security_lab.tool_catalog import build_check

def rows(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text('utf-8').splitlines() if line]

async def wait(predicate, timeout=15):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return
        await asyncio.sleep(.1)
    raise AssertionError('Owned direct fixture receipt timeout')

def scope_body():
    now = int(time.time())
    return {'targets': [{'host': h, 'port': 8443, 'paths': ['/public'], 'excluded_paths': ['/public/admin'],
                         'methods': ['GET', 'HEAD']} for h in ('fixture.example.test', 'fixture6.example.test')],
            'authorization': {'reference': 'lab:isolated-docker-fixture', 'owner': 'fixture-operator',
                              'basis': 'Owned dual-stack direct HTTPS endpoints in private Docker network',
                              'hosts': ['fixture.example.test', 'fixture6.example.test'],
                              'valid_from': now - 1, 'valid_until': now + 600},
            'expires_at': now + 600, 'budget': {'seconds': 300, 'rps': 10, 'jobs': 20, 'concurrency': 1}}

async def qualify(image, root, output):
    owner = 'sl-direct-' + uuid.uuid4().hex[:12]
    network = owner + '-net'
    fixture = root / 'fixture'
    fixture.mkdir(mode=0o755)
    receipts, containers, dns_calls = [], [], []
    controller = dispatcher = backend = None
    completed = False
    def receipt(test, **values):
        receipts.append({'test': test, 'passed': True, **values})
    async def execute(url, *, seconds=20):
        job = await dispatcher.execute(case, {'tool': 'curl', 'url': url, 'method': 'GET', 'seconds': seconds})
        active = dispatcher.active.get(job['id'])
        if active:
            await active[1]
        result = controller.get_job_status(case, job['id'])
        exported = controller.export(case)
        body = json.loads(next(x['body'] for x in exported['evidence'] if x['id'] == result['evidence_id']))
        return result, body
    try:
        # Caller checks overlap before any object creation. No host firewall edit.
        docker('network', 'create', '--internal', '--ipv6', '--subnet', '172.31.64.0/24',
               '--subnet', 'fd00:64::/64', '--label', LABEL + '=' + owner, network)
        for suffix, ip, ip6, port in (('upstream', '172.31.64.3', 'fd00:64::3', 8443),
                                      ('foreign', '172.31.64.4', 'fd00:64::4', 8443)):
            name = owner + '-' + suffix
            containers.append(name)
            serverroot = fixture if suffix == 'upstream' else root / 'foreign'
            serverroot.mkdir(exist_ok=True, mode=0o755)
            docker('run', '-d', '--name', name, '--label', LABEL + '=' + owner, '--network', network,
                   '--ip', ip, '--ip6', ip6, '--cap-drop', 'ALL', '--read-only',
                   '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--user', '1000:1000',
                   '--mount', f'type=bind,src={serverroot},dst=/fixture',
                   '--entrypoint', '/usr/local/bin/python', image, '/app/lab/direct_fixture.py', '--port', str(port),
                   *(['--extra-port', '8444'] if suffix == 'upstream' else []))
        await wait(lambda: (fixture / 'ready.json').exists() and (root / 'foreign' / 'ready.json').exists())
        probe_endpoints = ((4, '172.31.64.4', 8443), (6, 'fd00:64::4', 8443),
                           (4, '172.31.64.3', 8444), (6, 'fd00:64::3', 8444))
        for family, address, port in probe_endpoints:
            code = ('import socket\ns=socket.socket(socket.AF_INET' + ('6' if family == 6 else '') +
                    ',socket.SOCK_STREAM)\ns.settimeout(3)\ns.connect((' + repr(address) + ',' + str(port) + '))\ns.close()\n')
            docker('exec', containers[-1], '/usr/local/bin/python', '-c', code)
        receipt('negative_probe_endpoints_reachable_without_worker_filter', endpoints=4,
                wrong_port_positive_listener=8444)
        controller = Controller(root / 'controller', 'fixture-operator')
        case = controller.create_case('fixture.example.test')['id']
        scope = controller.propose(case, 'scope', scope_body())
        controller.activate_from_task(case, scope['id'], 'scope')
        backend = DockerDirectBackend(image_digest=image, root=root, owner=owner, network=network,
                                      trusted_fixture_certificate=fixture / 'cert.pem')
        async def resolve(host, port):
            pins = {'fixture.example.test': '172.31.64.3', 'fixture6.example.test': 'fd00:64::3'}
            assert host in pins and port == 8443
            dns_calls.append({'host': host, 'port': port, 'at': time.time()})
            return [pins[host]]
        resolver = TargetResolver(resolve, lab_private_addresses=('172.31.64.3', 'fd00:64::3'))
        dispatcher = Dispatcher(controller, backend, renewal_interval=5, target_resolver=resolver)
        for host, family in (('fixture.example.test', 4), ('fixture6.example.test', 6)):
            before = len(rows(fixture / 'received.jsonl'))
            status, result = await execute('https://' + host + ':8443/public/info')
            assert status['status'] == 'completed' and result['exit_code'] == 0 and result['access_closed'] is True, result
            assert len(rows(fixture / 'received.jsonl')) == before + 1
            assert 'OWNED DIRECT HTTPS FIXTURE' in result['stdout'] and result['zero_network'] is False
            receipt('direct_https_ipv' + str(family) + '_received', job_id=status['id'], evidence_id=status['evidence_id'],
                    execution=backend.execution_receipts[-1], tls_interception=False)
        before = len(rows(fixture / 'received.jsonl'))
        job_count = len(controller.get_case(case)['jobs'])
        for path, method in (('/public/admin', 'GET'), ('/publicity', 'GET'), ('/public/info', 'POST')):
            try:
                await dispatcher.execute(case, {'tool': 'curl', 'url': 'https://fixture.example.test:8443' + path,
                                               'method': method, 'seconds': 20})
            except (Denied, ValueError):
                pass
            else:
                raise AssertionError('Forbidden typed request dispatched')
        assert len(controller.get_case(case)['jobs']) == job_count
        assert len(rows(fixture / 'received.jsonl')) == before
        receipt('forbidden_typed_path_method_before_job', upstream_new_requests=0, independent_http_filter=False)
        status, result = await execute('https://fixture.example.test:8443/public/redirect')
        assert status['status'] == 'completed' and '302' in result['stdout']
        assert len(rows(fixture / 'received.jsonl')) == before + 1
        assert rows(root / 'foreign' / 'received.jsonl') == []
        receipt('direct_redirect_not_followed', received=1)

        original_certificate = (fixture / 'cert.pem').read_bytes()
        before_tls = len(rows(fixture / 'received.jsonl'))
        try:
            # Trust another owned endpoint's certificate; the target continues
            # serving its original leaf loaded into its TLS context.
            (fixture / 'cert.pem').write_bytes((root / 'foreign' / 'cert.pem').read_bytes())
            status, result = await execute('https://fixture.example.test:8443/public/info')
            assert status['status'] == 'failed' and result['exit_code'] == 60, result
            assert len(rows(fixture / 'received.jsonl')) == before_tls
            receipt('direct_upstream_tls_bad_trust_refused', curl_exit=60, upstream_http_requests=0)
        finally:
            (fixture / 'cert.pem').write_bytes(original_certificate)

        # No model tool exposes these raw socket probes. They are independent
        # adversarial checks executed only by this trusted fixture harness.
        before = len(rows(fixture / 'received.jsonl'))
        job = await dispatcher.execute(case, {'tool': 'curl', 'url': 'https://fixture.example.test:8443/public/stream',
                                             'method': 'GET', 'seconds': 60})
        await wait(lambda: len(rows(fixture / 'received.jsonl')) == before + 1)
        started = rows(fixture / 'received.jsonl')[-1]['at']
        stream_peer = rows(fixture / 'received.jsonl')[-1]['peer_port']
        worker = backend.worker
        for family, address, port in (*probe_endpoints, (4, '127.0.0.11', 53)):
            code = ('import socket\ns=socket.socket(socket.AF_INET' + ('6' if family == 6 else '') +
                    ',socket.SOCK_STREAM)\ns.settimeout(1)\ntry:\n s.connect((' + repr(address) + ',' + str(port) +
                    '))\nexcept TimeoutError:\n pass\nelse:\n raise SystemExit(2)\n')
            probe = docker('exec', worker, '/usr/bin/setpriv', '--reuid=1001', '--regid=1001', '--clear-groups',
                           '--bounding-set=-all', '/usr/local/bin/python', '-c', code, check=False)
            assert probe.returncode == 0, 'Expected an actual socket timeout, not connection refused or process failure'
        receipt('actual_worker_tcp_ipv4_ipv6_port_dns_egress_denied', probes=5,
                positive_endpoint_baseline=True, expected_result='socket_timeout')
        remaining = 35 - (time.time() - started)
        if remaining > 0:
            await asyncio.sleep(remaining)
        assert controller.get_job_status(case, job['id'])['status'] == 'running'
        renewals = [a for a in backend.acks if a['job_id'] == job['id'] and a['sequence'] > 1]
        assert len(renewals) >= 3
        receipt('direct_stream_survives_initial_lease_with_real_renewal', seconds=round(time.time() - started, 3),
                renewals=len(renewals), dns_snapshot_calls=len(dns_calls))
        stopped = time.time()
        result = await dispatcher.stop(case)
        await wait(lambda: any(r['at'] >= stopped and r['peer_port'] == stream_peer
                              for r in rows(fixture / 'stream_closed.jsonl')), timeout=8)
        closed = next(r for r in rows(fixture / 'stream_closed.jsonl')
                      if r['at'] >= stopped and r['peer_port'] == stream_peer)
        assert closed['reason'] != 'natural_end'
        assert result['state'] == 'paused' and backend.worker is None
        assert controller.get_job_status(case, job['id'])['status'] == 'interrupted'
        receipt('direct_operator_stop_received_connection_closed', seconds=round(closed['at'] - stopped, 3),
                receiving_reason=closed['reason'], peer_port=stream_peer)

        # Controller-less heartbeat loss: real PID1 must close the long stream
        # before the short absolute lease, with no host renewer alive.
        controller.resume(case)
        approved = Scope.model_validate(scope_body())
        plan = build_check({'tool': 'curl', 'url': 'https://fixture.example.test:8443/public/stream',
                            'method': 'GET', 'seconds': 20}, approved, stage='scope_approved', now=time.time())
        backend.bind_plan(plan)
        fake = {'id': uuid.uuid4().hex, 'generation': controller.get_case(case)['generation'],
                'scope_hash': __import__('security_lab.contracts', fromlist=['digest']).digest(approved.model_dump(mode='json')),
                'deadline': time.time() + 20}
        policy = backend.policy(fake, approved)
        lease = Lease(job_id=fake['id'], generation=fake['generation'], scope_hash=fake['scope_hash'],
                      sequence=1, valid_until=time.time() + 10)
        await backend.install(policy, lease)
        lost = time.time()
        before_lost = len(rows(fixture / 'received.jsonl'))
        process = asyncio.create_task(backend.run(plan))
        await wait(lambda: len(rows(fixture / 'received.jsonl')) == before_lost + 1, timeout=3)
        lost_peer = rows(fixture / 'received.jsonl')[-1]['peer_port']
        await asyncio.wait_for(process, timeout=12)
        await wait(lambda: any(r['at'] >= lost and r['peer_port'] == lost_peer
                              for r in rows(fixture / 'stream_closed.jsonl')), timeout=3)
        close = next(r for r in rows(fixture / 'stream_closed.jsonl')
                     if r['at'] >= lost and r['peer_port'] == lost_peer)
        assert close['reason'] != 'natural_end'
        await backend.close(lease.model_copy(update={'revoked': True, 'sequence': 2}))
        receipt('direct_heartbeat_loss_root_supervisor_closes_connection', seconds=round(close['at'] - lost, 3),
                receiving_reason=close['reason'], peer_port=lost_peer)
        completed = True
    finally:
        if dispatcher:
            await dispatcher.shutdown()
        if controller:
            output.with_name('controller-export.json').write_text(json.dumps(controller.export(case), indent=2), 'utf-8')
            controller.close()
        if backend:
            output.with_name('installation-status.json').write_text(json.dumps({
                'last_step': backend.installation_step, 'acknowledgements': len(backend.acks),
                'closed_workers': backend.closed_workers, 'poisoned': backend.poisoned,
                'bootstrap_receipts': backend.bootstrap_receipts,
            }, indent=2), 'utf-8')
        if backend and backend.worker:
            await asyncio.to_thread(backend._close_native)
        for name in reversed(containers):
            log = docker('logs', name, check=False)
            output.with_name(name + '.log').write_text(log.stdout + log.stderr, 'utf-8')
            remove_owned('container', name, owner)
        remove_owned('network', network, owner)
        for name in ('received.jsonl', 'stream_closed.jsonl'):
            if (fixture / name).exists():
                shutil.copyfile(fixture / name, output.with_name(name))
        for log in root.glob('*-worker-*.log'):
            shutil.copyfile(log, output.with_name(log.name))
        for readback in root.glob('readback-*.json'):
            shutil.copyfile(readback, output.with_name(readback.name))
        observed_https = any(r['test'].startswith('direct_https_ipv') for r in receipts)
        output.write_text(json.dumps({'completed': completed, 'real_tool_and_packets': observed_https,
                                     'synthetic_target': True, 'tls_interception': False,
                                     'production_qualified': False, 'static_lab_dns': True,
                                     'receipts': receipts, 'dns_calls': dns_calls}, indent=2), 'utf-8')

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--image', required=True)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    args.root.mkdir(mode=0o700)
    args.output.parent.mkdir(exist_ok=True)
    asyncio.run(qualify(args.image, args.root, args.output))
