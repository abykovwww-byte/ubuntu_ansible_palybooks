"""Host-owned typed HTTP dispatch. Production is closed until backend qualification.

No shell, Docker/image arguments, capability or installer receipt is exposed
to MCP. FixtureBackend exercises the lifecycle with explicitly synthetic data.
Only explicitly zero-network backends may execute until a direct worker is
qualified; a lab_only label does not qualify a network-capable backend.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Protocol

from .contracts import Denied, Scope, digest
from .controller import Controller
from .http_policy import HTTPJobPolicy, Lease
from .osint_transport import redact_public_text
from .tool_catalog import CurlCheck, ToolPlan, build_check, parse_check, tool_catalog
from .target_resolution import TargetResolver


def model_tool_catalog() -> list[dict]:
    return [{**entry, 'name': entry['tool'], 'available': False, 'qualified': False,
             'allowed_actions': ['poc'] if entry['tool'] == 'sqlmap' else ['checks'],
             'reason': entry.get('requirement', entry['status'])} for entry in tool_catalog()]


@dataclass(frozen=True)
class WorkerResult:
    exit_code: int
    stdout: str
    stderr: str = ''
    truncated: bool = False


def redact_worker_output(value: str) -> str:
    # curl includes response headers. Hide complete unquoted cookie/auth lines
    # before shared public-text sanitization and before taking any preview/hash.
    value = re.sub(r'(?im)^(?:[<>] )?(authorization|proxy-authorization|cookie|set-cookie)[ \t]*:[^\r\n]*(?:\r?\n[ \t]+[^\r\n]*)*',
                   r'\1: [REDACTED_CREDENTIAL]', value)
    return redact_public_text(value)


class HTTPBackend(Protocol):
    # This object is trusted host configuration; never deserialized from a tool.
    synthetic: bool
    lab_only: bool
    zero_network: bool
    image_digest: str
    tool_version: str
    worker_identity: str

    def policy(self, job: dict, scope: Scope) -> HTTPJobPolicy: ...
    async def install(self, policy: HTTPJobPolicy, lease: Lease) -> dict: ...
    async def run(self, command: ToolPlan) -> WorkerResult: ...
    async def renew(self, lease: Lease) -> dict: ...
    async def close(self, lease: Lease) -> None: ...


class Dispatcher:
    def __init__(self, controller: Controller, backend: HTTPBackend | None = None, *, renewal_interval: float = 10,
                 target_resolver: TargetResolver | None = None):
        self.controller, self.backend = controller, backend
        self.target_resolver = target_resolver
        self.renewal_interval = renewal_interval
        self.active: dict[str, tuple[str, asyncio.Task]] = {}
        self.closing: set[str] = set()
        self.stops: set[asyncio.Task] = set()
        self.preparing = False

    async def execute(self, case_id: str, specification: dict, *, expected_binding: dict | None = None) -> dict:
        if self.preparing or self.active:
            raise Denied('This dispatcher is preparing or closing another worker')
        self.preparing = True
        try:
            return await self._prepare_execute(case_id, specification, expected_binding=expected_binding)
        finally:
            self.preparing = False

    async def _prepare_execute(self, case_id: str, specification: dict, *, expected_binding: dict | None = None) -> dict:
        """Returns after preparation; evidence appears asynchronously in Controller."""
        spec = parse_check(specification)
        if not isinstance(spec, CurlCheck):
            raise Denied('Network checks need a canonical network scope and qualified worker')
        backend = self.backend
        from .direct_worker import DockerDirectBackend
        direct_lab = type(backend) is DockerDirectBackend and backend.admission_ready()
        if (backend is None or getattr(backend, 'lab_only', None) is not True
                or (getattr(backend, 'zero_network', None) is not True and not direct_lab)):
            raise Denied('Production tool workers are not yet qualified; no job was issued')
        if direct_lab and self.target_resolver is None:
            raise Denied('Direct laboratory workers require complete host DNS snapshot validation')
        if direct_lab:
            with self.controller.lock:
                history = self.controller.db.execute(
                    'SELECT r.plan,e.body FROM jobs j JOIN job_runtime r ON r.job_id=j.id '
                    'LEFT JOIN evidence e ON e.id=r.evidence_id WHERE j.case_id=?', (case_id,)).fetchall()
                for previous in history:
                    if json.loads(previous['plan']).get('zero_network') is False:
                        body = json.loads(previous['body']) if previous['body'] else {}
                        if body.get('access_closed') is not True:
                            raise Denied('Prior direct worker has no confirmed closure; host reconciliation required')
        if self.active:
            raise Denied('This initial dispatcher runs one worker at a time')
        snapshot, manifest = self.controller.checks_context(case_id)
        scope = Scope.model_validate_json(manifest['body'])
        # Current trusted backends may only operate on the owned synthetic lab.
        if (snapshot['seed'] != 'fixture.example.test' or scope.authorization.reference != 'lab:isolated-docker-fixture'
            or any(t.host not in {'fixture.example.test', 'fixture6.example.test'} for t in scope.targets)):
            raise Denied('This backend is restricted to the isolated owned fixture')
        command = build_check(spec, scope, stage=snapshot['state'], now=self.controller.clock())
        if direct_lab:
            backend.bind_plan(command)
        resolution = await self.target_resolver.resolve(scope) if self.target_resolver is not None else None
        observed_binding = {'scope_id': manifest['id'], 'scope_hash': manifest['hash'], 'generation': snapshot['generation']}
        if expected_binding is not None and expected_binding != observed_binding:
            raise Denied('Pentest session scope/generation binding is stale')
        job, token = self.controller.prepare_job(case_id, backend.worker_identity, 'target_http', command.seconds,
                                                 expected_binding=observed_binding)
        try:
            policy = backend.policy(job, scope)
            if (policy.job_id, policy.generation, policy.scope_hash, policy.deadline) != (
                job['id'], job['generation'], job['scope_hash'], job['deadline']):
                raise Denied('Backend policy does not match Controller job')
            if resolution is not None:
                actual_pins = sorted((item.host, item.port, tuple(sorted(item.addresses))) for item in policy.upstreams)
                resolved_pins = sorted((item.host, item.port, tuple(sorted(item.addresses))) for item in resolution.upstreams)
                if actual_pins != resolved_pins:
                    raise Denied('Backend policy differs from the complete host resolution snapshot')
            plan = {'tool': command.tool, 'argv_hash': digest(list(command.argv)), 'image_digest': backend.image_digest,
                    'tool_version': backend.tool_version, 'worker_identity': backend.worker_identity,
                    'synthetic': backend.synthetic, 'lab_only': True, 'zero_network': backend.zero_network}
            lease = self.controller.bind_job_policy(job['id'], token, backend.worker_identity, 'target_http',
                                                    digest(policy.model_dump(mode='json')), plan)
            task = asyncio.create_task(self._execute(case_id, job, token, command, policy, lease, plan, resolution))
            self.active[job['id']] = (case_id, task)
            def finished(done):
                self.active.pop(job['id'], None)
                self.closing.discard(job['id'])
                if not done.cancelled():
                    done.exception()  # Structured outcomes stored; no raw exception output.
            task.add_done_callback(finished)
            # Establish the coroutine's cleanup boundary before a caller can
            # immediately stop it. Cancellation before first execution would
            # otherwise skip its finally block entirely.
            await asyncio.sleep(0)
        except Exception:
            self.controller.finish_job(case_id, job['id'], outcome='failed',
                                       result={'reason': 'Dispatcher preparation failed', 'installer_not_started': True,
                                               'access_closed': True}, synthetic=backend.synthetic)
            raise
        return self.controller.get_job_status(case_id, job['id'])

    async def _execute(self, case_id, job, token, command, policy, lease_body, plan, resolution=None):
        c, b = self.controller, self.backend
        lease = Lease.model_validate(lease_body)
        run_task = None
        outcome, result = 'failed', {'reason': 'Installer or worker failure'}
        started = time.monotonic()
        try:
            if resolution is not None:
                resolution = await self.target_resolver.revalidate(policy.scope, resolution)
                c.validate_capability(job['id'], token, b.worker_identity, 'target_http')
            ack = await b.install(policy, lease)
            c.acknowledge_job(job['id'], token, b.worker_identity, 'target_http', ack)
            c.start_job(job['id'], token, b.worker_identity, 'target_http')
            run_task = asyncio.create_task(b.run(command))
            while not run_task.done():
                remaining = min(job['deadline'], lease.valid_until) - c.clock()
                if remaining <= 0:
                    raise Denied('Worker lease/deadline elapsed')
                done, _ = await asyncio.wait({run_task}, timeout=min(self.renewal_interval, remaining))
                # Revalidate even when a worker completes before heartbeat.
                c.validate_capability(job['id'], token, b.worker_identity, 'target_http')
                if done:
                    break
                if resolution is not None:
                    resolution = await self.target_resolver.revalidate(policy.scope, resolution)
                lease = Lease.model_validate(c.renew_job(job['id'], token, b.worker_identity, 'target_http'))
                ack = await b.renew(lease)
                c.acknowledge_job(job['id'], token, b.worker_identity, 'target_http', ack)
            raw = await run_task
            if not isinstance(raw, WorkerResult):
                raise Denied('Invalid worker result contract')
            outcome = 'completed' if raw.exit_code == 0 else 'failed'
            # Redact the complete bounded output BEFORE taking a preview.
            if len(raw.stdout.encode('utf-8')) + len(raw.stderr.encode('utf-8')) > command.max_output_bytes:
                raise Denied('Backend exceeded its output bound')
            stdout, stderr = redact_worker_output(raw.stdout), redact_worker_output(raw.stderr)
            stdout_preview = stdout.encode('utf-8')[:12000].decode('utf-8', errors='ignore')
            stderr_preview = stderr.encode('utf-8')[:12000].decode('utf-8', errors='ignore')
            result = {**plan, 'job_id': job['id'], 'generation': job['generation'], 'scope_hash': job['scope_hash'],
                      'policy_hash': digest(policy.model_dump(mode='json')), 'exit_code': raw.exit_code,
                      'stdout': stdout_preview, 'stderr': stderr_preview,
                      'stdout_hash': hashlib.sha256(stdout.encode()).hexdigest(),
                      'stderr_hash': hashlib.sha256(stderr.encode()).hexdigest(),
                      'truncated': raw.truncated or stdout != stdout_preview or stderr != stderr_preview,
                      'seconds_observed': round(time.monotonic() - started, 3),
                      'interpretation': 'Worker output is untrusted evidence; exit zero alone does not verify a vulnerability'}
        except asyncio.CancelledError:
            outcome, result = 'interrupted', {**plan, 'reason': 'Dispatcher cancelled; worker closure requested'}
        except Exception as exc:
            outcome, result = 'failed', {**plan, 'reason': type(exc).__name__ + ': installer/lease/worker failed'}
        finally:
            self.closing.add(job['id'])
            if run_task is not None and not run_task.done():
                run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)
            revoked = Lease(job_id=lease.job_id, generation=lease.generation, scope_hash=lease.scope_hash,
                            sequence=lease.sequence + 1, valid_until=lease.valid_until, revoked=True)
            closure = asyncio.create_task(asyncio.wait_for(b.close(revoked), timeout=10))
            while not closure.done():
                try:
                    await asyncio.shield(closure)
                except asyncio.CancelledError:
                    # Closing is owned by this dispatcher, not its caller.
                    continue
                except Exception:
                    break
            try:
                closure.result()
                result['access_closed'] = True
            except Exception:
                outcome, result['access_closed'] = 'interrupted', False
                if not b.zero_network:
                    c.stop(case_id)
            c.finish_job(case_id, job['id'], outcome=outcome, result=result, synthetic=b.synthetic)

    async def stop(self, case_id: str) -> dict:
        self.controller.stop(case_id)
        tasks = [task for cid, task in tuple(self.active.values()) if cid == case_id]
        for job_id, (cid, task) in tuple(self.active.items()):
            if cid == case_id and job_id not in self.closing and not task.cancelling():
                task.cancel()
        cleanup = asyncio.create_task(self._close_and_ack(case_id, tasks))
        self.stops.add(cleanup)
        def finished(done):
            self.stops.discard(done)
            if not done.cancelled():
                done.exception()
        cleanup.add_done_callback(finished)
        # A cancelled MCP request must not propagate cancellation into worker
        # closure. The dispatcher owns cleanup independently of the request.
        return await asyncio.shield(cleanup)

    async def _close_and_ack(self, case_id: str, tasks: list[asyncio.Task]) -> dict:
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        exported = self.controller.export(case_id)
        prior_jobs = [j for j in exported['jobs'] if j['generation'] < exported['generation']]
        evidence = {e['id']: e for e in exported['evidence']}
        import json
        closed = [j['id'] for j in prior_jobs if j['evidence_id'] in evidence
                  and json.loads(evidence[j['evidence_id']]['body']).get('access_closed') is True]
        if len(closed) == len(prior_jobs):
            self.controller.acknowledge_revocation(case_id, exported['generation'], closed)
        return self.controller.get_case(case_id)

    async def shutdown(self):
        cases = {case_id for case_id, _ in self.active.values()}
        for case_id in cases:
            await self.stop(case_id)
        if self.stops:
            await asyncio.gather(*tuple(self.stops), return_exceptions=True)


class FixtureBackend:
    """Zero-network synthetic backend, NEVER evidence of real curl/enforcement."""
    synthetic = True
    lab_only = True
    zero_network = True
    image_digest = 'synthetic:no-image'
    tool_version = 'synthetic:no-binary'
    worker_identity = 'synthetic:fixture-http'

    def __init__(self, *, delay=0):
        self.delay = delay
        self.current_policy = None
        self.closed = False
        self.renewals = 0

    def policy(self, job, scope):
        return HTTPJobPolicy(job_id=job['id'], generation=job['generation'], scope_hash=job['scope_hash'], scope=scope,
                             upstreams=[{'host': t.host, 'port': t.port, 'addresses': ['172.30.52.3']} for t in scope.targets],
                             worker_addresses=['172.30.51.3'], lab_private_addresses=['172.30.52.3'], deadline=job['deadline'])

    async def install(self, policy, lease):
        self.current_policy = policy
        return {**lease.model_dump(mode='json'), 'policy_hash': digest(policy.model_dump(mode='json'))}

    async def run(self, command):
        await asyncio.sleep(self.delay)
        return WorkerResult(0, 'HTTP/1.1 200 OK\nX-Fixture: synthetic\n\nSynthetic fixture; no network or tool was invoked')

    async def renew(self, lease):
        self.renewals += 1
        return {**lease.model_dump(mode='json'), 'policy_hash': digest(self.current_policy.model_dump(mode='json'))}

    async def close(self, lease):
        self.closed = True
