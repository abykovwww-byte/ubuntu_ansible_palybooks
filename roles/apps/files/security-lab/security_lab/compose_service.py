"""Bind the existing PentestGPT Compose CLI to the chat's Controller.

This adapter launches the existing agent; it does not implement HTTP, a scanner,
or independent phase/network enforcement. The existing agent has native shell
access. A reduced HTTP scope is therefore refused before any provider starts.
"""
from __future__ import annotations

import asyncio
import json
import math
import socket

from .contracts import Denied, Scope
from .compose_runtime import _request_goal
from .controller import Controller
from .dispatcher import redact_worker_output
from .handoff import build_handoff
from .tool_catalog import NmapCheck, build_check


async def resolve_addresses(host: str) -> list[str]:
    """Host DNS selects literal CLI targets; model input never selects pins."""
    rows = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, None,
        type=socket.SOCK_STREAM), timeout=5)
    return sorted({row[4][0] for row in rows})


def _compact_handoff(snapshot: dict, manifest: dict, *, now: float) -> dict:
    """Complete graph projection, not a count, excerpt or execution grant.

    The existing builder validates snapshot references and the immutable scope.
    Histories/raw responses are omitted; every node, relation and evidence index
    entry is retained. Catalog-only gaps use compact status records; recorded
    receipt failures retain their reason. The goal cap rejects oversized maps.
    """
    packet = build_handoff(snapshot, manifest, [], now=now)
    data = packet['untrusted_osint_data']
    def project(row, fields):
        return {key: row[key] for key in fields if key in row}
    return {
        'format': 'compact_handoff_projection', 'case_id': packet['case_id'],
        'generation': packet['generation'], 'context_hash': packet['context_hash'],
        'binding': project(packet['binding'], ('id', 'version', 'hash')),
        'trust': data['trust'], 'ownership_proven': False,
        'scope_expansion_allowed': False, 'poc_allowed': False,
        'surface_nodes': [project(row, ('id', 'kind', 'value', 'attribution', 'evidence_ids'))
                          for row in data['surface_nodes']],
        'relations': [project(row, ('id', 'source_id', 'target_id', 'relation', 'evidence_ids'))
                      for row in data['relations']],
        'evidence_index': [project(row, ('id', 'source', 'status', 'hash', 'synthetic', 'purged'))
                           for row in data['evidence_index']],
        'coverage': data['coverage'],
        'gaps': [project(row, ('id', 'source', 'status', 'reason', 'truncated', 'synthetic'))
                 if 'id' in row else project(row, ('source', 'status', 'implemented'))
                 for row in data['gaps']],
    }


class ComposeRunService:
    def __init__(self, controller: Controller, runtime, *, poll_seconds: float = 2, resolver=resolve_addresses):
        self.controller, self.runtime = controller, runtime
        self.poll_seconds = poll_seconds
        self.resolver = resolver
        self.tasks: dict[str, tuple[str, asyncio.Task]] = {}
        self.preparing = False

    async def start_nmap(self, case_id: str, host: str, ports: list[int], *, seconds: int = 30,
                         ipv6: bool = False) -> dict:
        if self.preparing or self.tasks:
            raise Denied('A native run is already preparing or active')
        self.preparing = True
        c, job, task = self.controller, None, None
        try:
            snapshot, manifest = c.checks_context(case_id)
            scope = Scope.model_validate_json(manifest['body'])
            if not any(target.host == host and set(ports) <= set(target.tcp_ports)
                       for target in scope.network_targets):
                raise Denied('Nmap host/ports are outside the explicit approved network scope')
            addresses = await self.resolver(host)
            import ipaddress
            selected = [str(ipaddress.ip_address(a)) for a in addresses
                        if ipaddress.ip_address(a).version == (6 if ipv6 else 4)]
            if not selected:
                raise Denied('Host DNS returned no address for the selected family')
            plan = build_check(NmapCheck(host=host, addresses=selected, ports=ports, seconds=seconds),
                scope, stage=snapshot['state'], now=c.clock(), pinned_addresses={host: addresses})
            binding = {'scope_id': manifest['id'], 'scope_hash': manifest['hash'],
                       'generation': snapshot['generation']}
            job, token = c.prepare_job(case_id, 'existing-pentestgpt-compose', 'target_network', seconds,
                                       expected_binding=binding)
            run_id = 'nmap-' + job['id']
            receipt_plan = {**binding, 'run_id': run_id, 'tool': 'nmap',
                'execution_mode': 'existing_compose_runtime', 'native_full_access': False,
                'phase_enforcement': 'fixed_nmap_entrypoint', 'provider_called': False,
                'reduced_scope_enforced': False, 'network_ack': False,
                'scope_enforcement': 'fixed_cli_arguments_and_host_dns_pins',
                'target_host': host, 'target_addresses': selected, 'target_ports': ports,
                'synthetic': False}
            c.start_native_job(job['id'], token, 'existing-pentestgpt-compose', 'target_network', receipt_plan)
            task = asyncio.create_task(self._monitor_tool(case_id, job, token, plan, run_id, receipt_plan))
            self.tasks[job['id']] = (case_id, task)
            task.add_done_callback(lambda t: self._finished(job['id'], t))
            await asyncio.sleep(0)
            return {**c.get_job_status(case_id, job['id']), 'run_id': run_id,
                    'tool': 'nmap', 'provider_called': False}
        except BaseException:
            if job is not None:
                if task is not None:
                    # The first yield can already start the native runtime.
                    # Its monitor owns closure/evidence; join it before the
                    # cancelled admission returns or releases the service gate.
                    if not task.done() and not task.cancelling():
                        task.cancel()
                    joined = asyncio.gather(task, return_exceptions=True)
                    while not joined.done():
                        try:
                            await asyncio.shield(joined)
                        except asyncio.CancelledError:
                            continue
                    if c.get_job_status(case_id, job['id'])['evidence_id'] is None:
                        # Cancellation before the monitor's first turn never
                        # invoked run_tool. Otherwise, a broken monitor needs
                        # an explicit runtime closure receipt before finishing.
                        closed = task.cancelled()
                        if not closed:
                            closure = asyncio.create_task(self.runtime.stop(run_id))
                            while not closure.done():
                                try:
                                    await asyncio.shield(closure)
                                except asyncio.CancelledError:
                                    continue
                                except Exception:
                                    break
                            try:
                                closed = closure.result().get('access_closed') is True
                            except Exception:
                                closed = False
                        if not closed:
                            c.stop(case_id)
                        c.finish_job(case_id, job['id'], outcome='interrupted', synthetic=False,
                            result={'tool':'nmap', 'reason':'CLI admission interrupted after task ownership',
                                    'access_closed':closed})
                else:
                    c.finish_job(case_id, job['id'], outcome='failed', synthetic=False,
                        result={'tool':'nmap', 'reason':'CLI admission failed before task ownership',
                                'access_closed':True})
            raise
        finally:
            self.preparing = False

    async def _monitor_tool(self, case_id, job, token, plan, run_id, receipt_plan):
        c = self.controller
        run = asyncio.create_task(self.runtime.run_tool(run_id=run_id, argv=plan.argv, seconds=plan.seconds))
        outcome, latest = 'failed', {}
        try:
            while not run.done():
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', 'target_network')
                remaining = job['deadline'] - c.clock()
                if remaining <= 0:
                    raise Denied('Approved Nmap deadline elapsed')
                await asyncio.wait({run}, timeout=min(5, remaining))
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', 'target_network')
                if not run.done():
                    c.renew_native_job(job['id'], token, 'existing-pentestgpt-compose', 'target_network')
            latest = await run
            outcome = 'completed' if latest.get('status') == 'completed' else 'failed'
        except asyncio.CancelledError:
            outcome = 'interrupted'
        except Exception as exc:
            outcome, latest = 'interrupted', {'error_type': type(exc).__name__,
                                              'error': redact_worker_output(str(exc))[:1000]}
        finally:
            if not run.done():
                run.cancel()
            await asyncio.shield(asyncio.gather(run, return_exceptions=True))
            try:
                closure = await asyncio.shield(self.runtime.stop(run_id))
                closed = closure.get('access_closed') is True
            except Exception:
                closed = False
            if not closed:
                outcome = 'interrupted'
                c.stop(case_id)
            c.finish_job(case_id, job['id'], outcome=outcome, synthetic=False,
                result={**receipt_plan, 'access_closed': closed,
                        'receipt': redact_worker_output(json.dumps(latest, ensure_ascii=False))[:18000],
                        'verified_vulnerabilities': [], 'finding_status': 'untrusted_tool_observation'})

    async def start(self, case_id: str) -> dict:
        if self.preparing or self.tasks:
            raise Denied('A native PentestGPT run is already preparing or active')
        self.preparing = True
        c = self.controller
        job = None
        try:
            snapshot, manifest = c.checks_context(case_id)
            scope = Scope.model_validate_json(manifest['body'])
            # The legacy runner accepts one target. Do not silently truncate
            # a surface, bypass excluded paths, or promise prompt-only limits.
            if len(scope.targets) != 1:
                raise Denied('Existing PentestGPT runner accepts one target per run')
            target = scope.targets[0]
            if target.paths != ['/'] or target.excluded_paths:
                raise Denied('Native PentestGPT cannot enforce a reduced HTTP path scope; no provider started')
            if getattr(scope, 'network_targets', []):
                raise Denied('Native runtime network scope is not enforced by the existing proxy')
            # SQLite spent is REAL even when zero. Preserve the strict integer
            # Budget contract and never round fractional remaining time upward.
            seconds = math.floor(min(self.runtime.config.max_runtime_seconds,
                          min(scope.expires_at, scope.authorization.valid_until) - c.clock(),
                          scope.budget.seconds - snapshot['spent']))
            if seconds < 60:
                raise Denied('Existing runtime needs at least 60 seconds within the approved budget')
            body = json.loads(manifest['body'])
            body['budget'] = {**body['budget'], 'seconds': seconds}
            binding = {'scope_id': manifest['id'], 'scope_hash': manifest['hash'],
                       'generation': snapshot['generation']}
            # Use the original immutable manifest for handoff validation. Only
            # the launcher receives the reduced remaining runtime budget.
            context = _compact_handoff(snapshot, manifest, now=c.clock())
            # Refuse a map that cannot reach the model intact before reserving
            # a job, starting a provider or touching a container.
            _request_goal(Scope.model_validate(body), context)
            job, token = c.prepare_job(case_id, 'existing-pentestgpt-compose', 'target_http', seconds,
                                       expected_binding=binding)
            run_id = 'chat-' + job['id']
            plan = {**binding, 'tool': 'pentestgpt-agent', 'run_id': run_id,
                    'execution_mode': 'existing_compose_runtime', 'synthetic': False,
                    'phase_enforcement': 'prompt_only', 'native_full_access': True,
                    'scope_enforcement': 'launcher_allowlist_and_existing_proxy',
                    'reduced_scope_enforced': False, 'network_ack': False,
                    'handoff_context_hash': context['context_hash']}
            c.start_native_job(job['id'], token, 'existing-pentestgpt-compose', 'target_http', plan)
            started = await self.runtime.start(run_id=run_id, scope=body, osint_context=context)
            c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', 'target_http')
            task = asyncio.create_task(self._monitor(case_id, job, token, run_id, plan, started))
            self.tasks[job['id']] = (case_id, task)
            task.add_done_callback(lambda t: self._finished(job['id'], t))
            await asyncio.sleep(0)
            return {**c.get_job_status(case_id, job['id']), 'run_id': run_id,
                    'execution': 'existing_pentestgpt', 'phase_enforcement': 'prompt_only',
                    'native_full_access': True, 'reduced_scope_enforced': False}
        except BaseException:
            if job is not None:
                try:
                    closure = await asyncio.shield(self.runtime.stop('chat-' + job['id']))
                    closed = closure.get('access_closed') is True
                except Exception:
                    closed = False
                if not closed:
                    c.stop(case_id)
                c.finish_job(case_id, job['id'], outcome='failed', synthetic=False,
                    result={'execution_mode': 'existing_compose_runtime', 'access_closed': closed,
                            'reason': 'Existing runtime preparation did not complete'})
            raise
        finally:
            self.preparing = False

    def _finished(self, job_id, task):
        self.tasks.pop(job_id, None)
        if not task.cancelled():
            task.exception()

    async def _monitor(self, case_id, job, token, run_id, plan, started):
        c = self.controller
        outcome, latest = 'failed', {'launch': started}
        try:
            while True:
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', 'target_http')
                latest = await self.runtime.poll(run_id)
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', 'target_http')
                if latest.get('status') in {'completed', 'failed', 'stopped', 'missing_or_interrupted'}:
                    outcome = 'completed' if latest['status'] == 'completed' else 'failed'
                    break
                c.renew_native_job(job['id'], token, 'existing-pentestgpt-compose', 'target_http')
                await asyncio.sleep(self.poll_seconds)
        except asyncio.CancelledError:
            outcome = 'interrupted'
        except Exception:
            outcome = 'interrupted'
        finally:
            closure_task = asyncio.create_task(self.runtime.stop(run_id))
            while not closure_task.done():
                try:
                    await asyncio.shield(closure_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                closed = closure_task.result().get('access_closed') is True
            except Exception:
                closed = False
            if not closed:
                outcome = 'interrupted'
                c.stop(case_id)
            # Bound before recording; no raw auth/env or arbitrary filesystem
            # reads enter the operator's evidence store.
            receipt = redact_worker_output(json.dumps(latest, ensure_ascii=False))[:18000]
            c.finish_job(case_id, job['id'], outcome=outcome, synthetic=False,
                result={**plan, 'receipt': receipt, 'access_closed': closed,
                        'verified_vulnerabilities': [], 'finding_status': 'untrusted_model_assessment'})

    async def stop_case(self, case_id: str):
        pending = [task for owner, task in self.tasks.values() if owner == case_id]
        for task in pending:
            if not task.cancelling():
                task.cancel()
        if pending:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))

    async def shutdown(self):
        for owner in {owner for owner, _ in self.tasks.values()}:
            await self.stop_case(owner)
