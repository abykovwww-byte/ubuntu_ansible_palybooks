"""Trusted Docker direct-curl backend for owned internal fixtures only.

No production admission, shell, model-controlled image, TLS interception or
credentials. The root supervisor and nft bootstrap belong to the host launcher;
curl runs as UID 1001 with an empty environment and zero capabilities.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

from .contracts import Denied, Scope, digest
from .http_policy import HTTPJobPolicy, Lease
from .tool_catalog import ToolPlan
from .direct_policy import compile_direct_execution, nft_initial_rules, nft_lease_rules, nft_revoke_rules
from .direct_readback import validate_readback

LABEL = 'security-lab-direct-owner'
WORKER_LABEL = 'security-lab-direct-worker'
TABLE = 'security_lab_direct'


def docker(*args, input=None, check=True, timeout=5):
    result = subprocess.run(['/usr/bin/docker', *args], input=input, capture_output=True,
                            text=True, timeout=timeout)
    if check and result.returncode:
        raise Denied('Owned direct-worker Docker operation failed')
    return result


def inspect_owned(kind, name, owner, *, timeout=2):
    result = docker(kind, 'inspect', name, check=False, timeout=timeout)
    if result.returncode:
        # A daemon/permission failure is uncertainty, not an absence receipt.
        args = ['container', 'ls', '-a'] if kind == 'container' else ['network', 'ls']
        names = docker(*args, '--format', '{{.Names}}' if kind == 'container' else '{{.Name}}', timeout=timeout).stdout.splitlines()
        if name not in names:
            return None
        raise Denied('Owned object inspect failed while the object still exists')
    body = json.loads(result.stdout)[0]
    labels = body.get('Labels', {}) if kind == 'network' else body['Config'].get('Labels', {})
    if labels.get(LABEL) != owner:
        raise Denied('Object is not owned by this exact direct laboratory run')
    return body


def remove_owned(kind, name, owner, *, timeout=1):
    if inspect_owned(kind, name, owner, timeout=timeout) is None:
        return
    docker(kind, 'rm', *(['-f'] if kind == 'container' else []), name, timeout=timeout)
    if inspect_owned(kind, name, owner, timeout=timeout) is not None:
        raise Denied('Owned direct-worker removal was not confirmed')


class DockerDirectBackend:
    synthetic = lab_only = True
    zero_network = False

    def __init__(self, *, image_digest: str, root: Path, owner: str, network: str,
                 trusted_fixture_certificate: Path):
        if sys.platform != 'linux' or os.geteuid() == 0:
            raise Denied('Direct lab launcher requires Linux and a nonroot host service UID')
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', image_digest):
            raise Denied('An inspected immutable image ID is required')
        if not re.fullmatch(r'sl-direct-[a-f0-9]{12}', owner) or network != owner + '-net':
            raise Denied('Expected exact private-laboratory ownership names')
        root = Path(root)
        if not root.is_absolute() or root.is_symlink() or root.resolve() != root or root.stat().st_uid != os.geteuid():
            raise Denied('Private direct-lab directory must belong to the host service')
        certificate = Path(trusted_fixture_certificate)
        if certificate.resolve() != root / 'fixture' / 'cert.pem' or certificate.is_symlink() or not certificate.is_file():
            raise Denied('Only the public certificate of this owned synthetic endpoint may be mounted')
        network_body = inspect_owned('network', network, owner)
        if not network_body or network_body.get('Internal') is not True or network_body.get('EnableIPv6') is not True:
            raise Denied('Expected the owned internal dual-stack fixture network')
        subnets = {c['Subnet'] for c in network_body['IPAM']['Config']}
        if subnets != {'172.31.64.0/24', 'fd00:64::/64'}:
            raise Denied('Unexpected fixture subnets')
        if docker('ps', '-aq', '--filter', 'label=' + WORKER_LABEL).stdout.strip():
            raise Denied('An unreconciled direct worker exists; independent host cleanup is required')
        actual = docker('image', 'inspect', '--format', '{{.Id}}', image_digest).stdout.strip()
        if actual != image_digest:
            raise Denied('Immutable image identity mismatch')
        version = docker('run', '--rm', '--network', 'none', '--cap-drop', 'ALL',
                         '--entrypoint', '/usr/bin/curl', image_digest, '--version').stdout.splitlines()[0]
        self.image_digest, self.tool_version = actual, version
        self.root, self.owner, self.network, self.certificate = root, owner, network, certificate
        self.worker_identity = 'docker-direct-lab:' + owner
        self._validated = True
        self.poisoned = False
        self.stop_requested = False
        self.lock = threading.RLock()
        self.worker = self.control = self.policy_value = self.lease = self.command = self.execution = None
        self.counter = 0
        self.process = None
        self.acks, self.execution_receipts, self.closed_workers = [], [], []
        self.installation_step = 'not_started'
        self.bootstrap_receipts = []

    def admission_ready(self):
        return self._validated is True and not self.poisoned and self.worker is None

    def bind_plan(self, plan):
        if not self.admission_ready() or not isinstance(plan, ToolPlan):
            raise Denied('Direct backend cannot accept a new plan')
        self.command = plan
        self.stop_requested = False

    def policy(self, job, scope: Scope):
        if not self.admission_ready() or self.command is None:
            raise Denied('Direct backend is not clean and bound')
        pins = {'fixture.example.test': '172.31.64.3', 'fixture6.example.test': 'fd00:64::3'}
        if (scope.authorization.reference != 'lab:isolated-docker-fixture'
                or any(t.host not in pins or t.port != 8443 for t in scope.targets)):
            raise Denied('Direct backend only supports the exact owned HTTPS fixture')
        return HTTPJobPolicy(job_id=job['id'], generation=job['generation'], scope_hash=job['scope_hash'], scope=scope,
                             upstreams=[{'host': t.host, 'port': t.port, 'addresses': [pins[t.host]]}
                                        for t in {(t.host, t.port): t for t in scope.targets}.values()],
                             worker_addresses=['172.31.64.2', 'fd00:64::2'],
                             lab_private_addresses=list(pins.values()), deadline=job['deadline'])

    def _publish(self, lease):
        temporary = self.control / 'lease.new'
        with temporary.open('x', encoding='utf-8') as output:
            output.write(lease.model_dump_json())
            output.flush()
            os.fsync(output.fileno())
        temporary.chmod(0o644)  # public binding/expiry only; no capability token
        os.replace(temporary, self.control / 'lease.json')

    def _apply(self, lease):
        # Account for the maximum native installer delay conservatively. nft's
        # relative timeout cannot outlive the absolute lease even after latency.
        margin = 5.0
        if lease.valid_until - time.time() <= margin + 1:
            raise Denied('Insufficient lease time for bounded direct installation')
        rules = nft_lease_rules(self.execution, self.policy_value, lease, now=time.time() + margin)
        docker('exec', '-i', self.worker, '/usr/sbin/nft', '-f', '-', input=rules)
        if time.time() >= lease.valid_until - 1:
            raise Denied('Direct installer acknowledgement arrived too late')
        table = json.loads(docker('exec', self.worker, '/usr/sbin/nft', '-n', '-j', 'list', 'table', 'inet', TABLE).stdout)
        (self.root / f'readback-{self.counter}-{lease.sequence}.json').write_text(json.dumps(table, indent=2), 'utf-8')
        validate_readback(table, self.execution, maximum_ms=min(30000, int((lease.valid_until - time.time()) * 1000)))
        if self.stop_requested:
            raise Denied('Direct stop was requested before installer acknowledgement')
        self._publish(lease)
        self.lease = lease
        ack = {**lease.model_dump(mode='json'), 'policy_hash': self.execution.policy_hash}
        self.acks.append(ack)
        return ack

    def _install(self, policy, lease):
        with self.lock:
            if not self.admission_ready():
                raise Denied('Uncertain direct worker cannot be reused')
            self.policy_value = policy
            self.execution = compile_direct_execution(self.command, policy, lease, now=time.time())
            self.counter += 1
            self.control = self.root / ('control-' + str(self.counter))
            self.control.mkdir(mode=0o755)
            if self.stop_requested:
                (self.control / 'stop').touch()
                raise Denied('Direct stop preceded namespace creation')
            self._publish(lease)
            self.worker = self.owner + '-worker-' + str(self.counter)
            try:
                self.installation_step = 'create_namespace'
                docker('run', '-d', '--name', self.worker, '--label', LABEL + '=' + self.owner,
                       '--label', WORKER_LABEL + '=true', '--network', 'none', '--cap-drop', 'ALL',
                       '--cap-add', 'NET_ADMIN', '--cap-add', 'KILL', '--cap-add', 'SETUID',
                       '--cap-add', 'SETGID', '--cap-add', 'SETPCAP', '--security-opt', 'no-new-privileges:true',
                       '--read-only', '--pids-limit', '32', '--memory', '128m', '--cpus', '0.5',
                       '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m',
                       '--mount', f'type=bind,src={self.control},dst=/control,readonly',
                       '--mount', f'type=bind,src={self.certificate},dst=/etc/ssl/certs/ca-certificates.crt,readonly',
                       '--entrypoint', '/usr/local/bin/python', self.image_digest, '/app/lab/direct_watchdog.py',
                       '--job-id', policy.job_id, '--generation', str(policy.generation),
                       '--scope-hash', policy.scope_hash, '--deadline', str(policy.deadline))
                self.installation_step = 'empty_nft_policy'
                docker('exec', '-i', self.worker, '/usr/sbin/nft', '-f', '-', input=nft_initial_rules(self.execution))
                before_ns = docker('exec', self.worker, '/usr/bin/readlink', '/proc/self/ns/net').stdout.strip()
                if not re.fullmatch(r'net:\[[0-9]+\]', before_ns):
                    raise Denied('Invalid direct network namespace receipt')
                before_table = json.loads(docker('exec', self.worker, '/usr/sbin/nft', '-n', '-j', 'list', 'table', 'inet', TABLE).stdout)
                self.installation_step = 'attach_network'
                # Docker refuses a bridge attachment while its special none
                # endpoint exists. Drop-default nft hooks are already active;
                # detach none before connecting only the owned internal bridge.
                docker('network', 'disconnect', 'none', self.worker)
                docker('network', 'connect', '--ip', '172.31.64.2', '--ip6', 'fd00:64::2', self.network, self.worker)
                self.installation_step = 'isolation_readback'
                body = inspect_owned('container', self.worker, self.owner)
                if not body or set(body['NetworkSettings']['Networks']) != {self.network} or not body['HostConfig']['ReadonlyRootfs']:
                    raise Denied('Direct worker isolation read-back mismatch')
                if any(m['RW'] or m['Destination'] not in {'/control', '/etc/ssl/certs/ca-certificates.crt'} for m in body['Mounts']):
                    raise Denied('Unexpected direct worker mount')
                after_ns = docker('exec', self.worker, '/usr/bin/readlink', '/proc/self/ns/net').stdout.strip()
                after_table = json.loads(docker('exec', self.worker, '/usr/sbin/nft', '-n', '-j', 'list', 'table', 'inet', TABLE).stdout)
                if after_ns != before_ns or after_table != before_table:
                    raise Denied('Direct network attachment changed namespace or initial policy')
                self.bootstrap_receipts.append({'job_id': policy.job_id, 'namespace': after_ns,
                                                'initial_policy_preserved': True,
                                                'initial_policy_hash': digest(after_table)})
                self.installation_step = 'tool_identity_readback'
                status = docker('exec', self.worker, '/usr/bin/setpriv', '--reuid=1001', '--regid=1001',
                                '--clear-groups', '--bounding-set=-all', '/usr/bin/cat', '/proc/self/status').stdout
                if 'CapEff:\t0000000000000000' not in status or 'Uid:\t1001\t1001\t1001\t1001' not in status:
                    raise Denied('Direct tool is not an unprivileged service process')
                self.installation_step = 'timed_nft_readback'
                result = self._apply(lease)
                self.installation_step = 'acknowledged'
                return result
            except BaseException:
                self.poisoned = True
                self._close_native()
                raise

    async def install(self, policy, lease):
        installation = asyncio.create_task(asyncio.to_thread(self._install, policy, lease))
        try:
            return await asyncio.shield(installation)
        except BaseException:
            await self._drain(installation)
            await self._join_close()
            raise

    @staticmethod
    async def _drain(task):
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()

    async def run(self, command):
        from .dispatcher import WorkerResult
        if command != self.command or self.worker is None or self.poisoned:
            raise Denied('Direct execution is not installed for this exact plan')
        execution = compile_direct_execution(command, self.policy_value, self.lease, now=time.time())
        if execution.destination != self.execution.destination or execution.port != self.execution.port:
            raise Denied('Direct pin cannot change inside a live namespace')
        self.execution_receipts.append({'job_id': self.policy_value.job_id, 'argv_hash': digest(list(execution.argv)),
                                        'destination': execution.destination, 'port': execution.port,
                                        'hostname': execution.hostname, 'tls_interception': False})
        argv = ['/usr/bin/docker', 'exec', self.worker, '/usr/bin/setpriv', '--reuid=1001', '--regid=1001',
                '--clear-groups', '--bounding-set=-all', '/usr/bin/env', '-i',
                'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'LC_ALL=C', *execution.argv]
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        self.process = process
        buffers, size = [bytearray(), bytearray()], 0
        async def read(stream, index):
            nonlocal size
            while chunk := await stream.read(16384):
                if size + len(chunk) > command.max_output_bytes:
                    await self._join_close()
                    raise Denied('Direct worker output bound exceeded')
                buffers[index].extend(chunk)
                size += len(chunk)
        readers = [asyncio.create_task(read(process.stdout, 0)), asyncio.create_task(read(process.stderr, 1))]
        try:
            await asyncio.wait_for(asyncio.gather(*readers, process.wait()), timeout=command.seconds + 1)
            return WorkerResult(process.returncode, buffers[0].decode('utf-8', 'replace'), buffers[1].decode('utf-8', 'replace'))
        finally:
            for task in readers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if process.returncode is None:
                await self._join_close()
            await process.wait()

    def _renew(self, lease):
        with self.lock:
            if self.poisoned or self.worker is None or self.lease is None or time.time() >= self.lease.valid_until - 1:
                raise Denied('Expired or uncertain direct namespace cannot be renewed')
            if lease.sequence != self.lease.sequence + 1:
                raise Denied('Direct lease must advance exactly one sequence')
            try:
                return self._apply(lease)
            except BaseException:
                self.poisoned = True
                self._close_native()
                raise

    async def renew(self, lease):
        renewal = asyncio.create_task(asyncio.to_thread(self._renew, lease))
        try:
            return await asyncio.shield(renewal)
        except BaseException:
            await self._drain(renewal)
            await self._join_close()
            raise

    def _close_native(self):
        with self.lock:
            worker = self.worker
            if worker is None:
                return
            try:
                # Terminate curl while FIN is still permitted, before revoke.
                body = inspect_owned('container', worker, self.owner, timeout=1)
                exit_error = None
                if body and body['State']['Running']:
                    try:
                        docker('kill', '--signal', 'TERM', worker, check=False, timeout=1)
                        # PID1 owns a bounded post-kill socket drain. Removing
                        # its namespace immediately would cut that drain short.
                        waited = docker('wait', worker, timeout=1)
                        if waited.stdout.strip() != '0':
                            raise Denied('Direct supervisor did not exit normally')
                    except Exception as error:
                        exit_error = error
                        self.poisoned = True
                try:
                    logs = docker('logs', worker, check=False, timeout=1)
                    (self.root / (worker + '.log')).write_text(logs.stdout + logs.stderr, 'utf-8')
                except Exception as error:
                    exit_error = exit_error or error
                    self.poisoned = True
                # Re-check exact ownership and prove removal even if wait or
                # log capture failed. Forced cleanup is not graceful closure.
                remove_owned('container', worker, self.owner)
                self.closed_workers.append(worker)
                self.worker = None
                if exit_error is not None:
                    raise Denied('Direct supervisor closure was not confirmed before forced cleanup') from exit_error
            except BaseException:
                self.poisoned = True
                raise

    async def close(self, lease):
        self.stop_requested = True
        # Revoked lease identity is validated even if setup failed before start.
        if self.policy_value is not None and (lease.job_id, lease.generation, lease.scope_hash) != (
                self.policy_value.job_id, self.policy_value.generation, self.policy_value.scope_hash):
            self.poisoned = True
            raise Denied('Direct close binding mismatch')
        await self._join_close()

    async def _join_close(self):
        self.stop_requested = True
        # Publish an irreversible stop before waiting for an installer/renewer
        # lock. PID1 observes this independently of Docker API availability.
        if self.control is not None:
            stop = self.control / 'stop'
            stop.touch(exist_ok=True)
            stop.chmod(0o644)
        closure = asyncio.create_task(asyncio.to_thread(self._close_native))
        await self._drain(closure)
        closure.result()
