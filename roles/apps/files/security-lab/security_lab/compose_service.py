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
from urllib.parse import urlsplit

from .contracts import Denied, Scope
from .compose_runtime import _request_goal, prepare_handoff_context
from .controller import Controller
from .dispatcher import redact_worker_output
from .handoff import build_handoff
from .tool_catalog import NmapCheck, build_check
from .native_checks import parse_native_check, check_subject, compile_native_check
from .native_targets import select_native_targets
from .native_handoff import execution_record, build_native_handoff


async def resolve_addresses(host: str) -> list[str]:
    """Host DNS selects literal CLI targets; model input never selects pins."""
    rows = await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, None,
        type=socket.SOCK_STREAM), timeout=5)
    return sorted({row[4][0] for row in rows})


class ComposeRunService:
    def __init__(self, controller: Controller, runtime, *, poll_seconds: float = 2, resolver=resolve_addresses):
        self.controller, self.runtime = controller, runtime
        self.poll_seconds = poll_seconds
        self.resolver = resolver
        self.tasks: dict[str, tuple[str, asyncio.Task]] = {}
        self.preparing = False

    async def start_nmap(self, case_id: str, host: str, ports: list[int], *, seconds: int = 30,
                         ipv6: bool = False) -> dict:
        return await self._start_cli(case_id, host, ports, seconds=seconds, ipv6=ipv6)

    async def start_check(self, case_id: str, specification: dict, *, ipv6: bool = False) -> dict:
        spec = parse_native_check(specification)
        return await self._start_cli(case_id, None, [], seconds=spec.seconds, ipv6=ipv6,
                                     specification=spec.model_dump(mode='json'))

    async def start_action(self, case_id: str, manifest_id: str, finding_id: str, *, ipv6: bool = False) -> dict:
        return await self._start_cli(case_id, None, [], seconds=0, ipv6=ipv6,
                                     action_identity=(manifest_id, finding_id))

    async def _start_cli(self, case_id, host, ports, *, seconds, ipv6, specification=None, action_identity=None):
        if self.preparing or self.tasks:
            raise Denied('A native run is already preparing or active')
        self.preparing = True
        c, job, task = self.controller, None, None
        try:
            action_binding, action = None, None
            if action_identity is not None:
                snapshot, manifest, action_manifest, action = c.action_context(case_id, *action_identity)
                specification = action.request.model_dump(mode='json')
                seconds = action.request.seconds
                action_binding = {'task_stage': 'poc', 'action_manifest_id': action_manifest['id'],
                                  'action_hash': action_manifest['hash'], 'finding_id': action.finding_id}
            else:
                snapshot, manifest = c.checks_context(case_id)
            scope = Scope.model_validate_json(manifest['body'])
            if specification is not None:
                host = check_subject(parse_native_check(specification), scope)
            elif not any(target.host == host and set(ports) <= set(target.tcp_ports)
                       for target in scope.network_targets):
                raise Denied('Nmap host/ports are outside the explicit approved network scope')
            addresses = await self.resolver(host)
            import ipaddress
            selected = [str(ipaddress.ip_address(a)) for a in addresses
                        if ipaddress.ip_address(a).version == (6 if ipv6 else 4)]
            if not selected:
                raise Denied('Host DNS returned no address for the selected family')
            if specification is not None:
                selected = sorted(set(selected))[:1]
                plan = compile_native_check(specification, scope, selected, stage=snapshot['state'], now=c.clock(),
                                            task_stage='poc' if action_binding else 'checks')
            else:
                plan = build_check(NmapCheck(host=host, addresses=selected, ports=ports, seconds=seconds),
                    scope, stage=snapshot['state'], now=c.clock(), pinned_addresses={host: addresses})
            binding = {'scope_id': manifest['id'], 'scope_hash': manifest['hash'],
                       'generation': snapshot['generation']}
            job, token = c.prepare_job(case_id, 'existing-pentestgpt-compose', plan.profile, seconds,
                                       expected_binding=binding, action_binding=action_binding)
            run_id = plan.tool + '-' + job['id']
            receipt_plan = {**binding, 'run_id': run_id, 'tool': plan.tool,
                'execution_mode': 'existing_compose_runtime', 'native_full_access': False,
                'phase_enforcement': 'fixed_' + plan.tool + '_entrypoint', 'provider_called': False,
                'reduced_scope_enforced': False, 'network_ack': False,
                'scope_enforcement': 'fixed_cli_arguments_and_host_dns_pins',
                'target_host': host, 'target_addresses': selected,
                'target_ports': ([specification['port']] if specification and 'port' in specification else ports),
                'specification': specification,
                'coverage_limits': 'Single selected DNS address; no independent version-probe RPS enforcement' if specification else '',
                'synthetic': False}
            if plan.tool in {'tls', 'testssl'}:
                receipt_plan.update(scope_enforcement='per_run_nft_pinned_tcp_endpoint',
                    network_enforcement_qualification='candidate_requires_receiving_side_tests',
                    capture_vantage='probe_network_namespace_on_worker_host',
                    target_receiving_side_capture=False)
            if action_binding:
                receipt_plan.update(action_binding)
                receipt_plan['verification'] = action.verification.model_dump(mode='json') if action.verification else None
            c.start_native_job(job['id'], token, 'existing-pentestgpt-compose', plan.profile, receipt_plan)
            task = asyncio.create_task(self._monitor_tool(case_id, job, token, plan, run_id, receipt_plan))
            self.tasks[job['id']] = (case_id, task)
            task.add_done_callback(lambda t: self._finished(job['id'], t))
            await asyncio.sleep(0)
            return {**c.get_job_status(case_id, job['id']), 'run_id': run_id,
                    'tool': plan.tool, 'provider_called': False}
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
                            result={'tool':plan.tool, 'reason':'CLI admission interrupted after task ownership',
                                    'access_closed':closed})
                else:
                    c.finish_job(case_id, job['id'], outcome='failed', synthetic=False,
                        result={'tool':plan.tool, 'reason':'CLI admission failed before task ownership',
                                'access_closed':True})
            raise
        finally:
            self.preparing = False

    async def _monitor_tool(self, case_id, job, token, plan, run_id, receipt_plan):
        c = self.controller
        outcome, latest, run = 'failed', {}, None
        try:
            if receipt_plan.get('specification') is not None:
                options = {'deadline':job['deadline']} if plan.tool in {'tls', 'testssl'} else {}
                run = asyncio.create_task(self.runtime.run_check(run_id=run_id, plan=plan, **options))
            else:
                run = asyncio.create_task(self.runtime.run_tool(run_id=run_id, argv=plan.argv, seconds=plan.seconds))
            while not run.done():
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', plan.profile)
                remaining = job['deadline'] - c.clock()
                if remaining <= 0:
                    raise Denied('Approved tool deadline elapsed')
                await asyncio.wait({run}, timeout=min(5, remaining))
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', plan.profile)
                if not run.done():
                    c.renew_native_job(job['id'], token, 'existing-pentestgpt-compose', plan.profile)
            latest = await run
            outcome = 'completed' if latest.get('status') == 'completed' else 'failed'
        except asyncio.CancelledError:
            outcome = 'interrupted'
        except Exception as exc:
            outcome, latest = 'interrupted', {'error_type': type(exc).__name__,
                                              'error': redact_worker_output(str(exc))[:1000]}
        finally:
            if run is not None:
                if not run.done():
                    run.cancel()
                await asyncio.shield(asyncio.gather(run, return_exceptions=True))
            try:
                closure = await asyncio.shield(self.runtime.stop(run_id))
                closed = closure.get('access_closed') is True
            except Exception:
                closure = {}
                closed = False
            if not closed:
                outcome = 'interrupted'
                c.stop(case_id)
            evaluation = {}
            if receipt_plan.get('task_stage') == 'poc':
                from .native_checks import evaluate_action_response
                evaluation = evaluate_action_response(latest, receipt_plan.get('verification'))
            if plan.tool in {'tls', 'testssl'}:
                # Cancelled SSH may not return stdout. Independent stop recovers
                # host artifacts instead of discarding interrupted evidence.
                latest = {**latest, 'closure':closure}
                evaluation['artifacts'] = closure.get('artifacts',latest.get('artifacts',[]))
                if len(json.dumps(latest,ensure_ascii=False)) > 18000:
                    latest = {'status':latest.get('status'), 'receipts_truncated':True,
                              'artifacts':evaluation['artifacts'], 'closure':closure,
                              'note':'Complete bounded command outputs are in host artifacts'}
            c.finish_job(case_id, job['id'], outcome=outcome, synthetic=False,
                result={**receipt_plan, **evaluation, 'access_closed': closed,
                        'receipt': redact_worker_output(json.dumps(latest, ensure_ascii=False))[:18000],
                        'verified_vulnerabilities': [], 'finding_status': 'untrusted_tool_observation'})

    async def start(self, case_id: str, *, targets: list[str] | None = None) -> dict:
        if self.preparing or self.tasks:
            raise Denied('A native PentestGPT run is already preparing or active')
        self.preparing = True
        c = self.controller
        job = None
        try:
            snapshot, manifest = c.checks_context(case_id)
            scope = Scope.model_validate_json(manifest['body'])
            execution_targets = select_native_targets(scope, targets)
            for value in execution_targets:
                parsed = urlsplit(value)
                if parsed.scheme in {'http', 'https'}:
                    service = next(t for t in scope.targets if t.host == parsed.hostname and t.scheme == parsed.scheme
                                   and t.port == (parsed.port or (443 if parsed.scheme == 'https' else 80)))
                    if service.paths != ['/'] or service.excluded_paths:
                        raise Denied('Native PentestGPT cannot enforce a reduced HTTP path scope; use typed HTTP checks instead')
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
            context = build_handoff(snapshot, manifest, [], now=c.clock())
            # Refuse a map that cannot reach the model intact before reserving
            # a job, starting a provider or touching a container.
            # Reserve the longest allowed host-generated run path in the goal
            # preflight; the eventual chat-UUID path must fit before any job.
            preview_run = 'preflight-' + '0' * 54
            preview_at = c.clock()
            preview = execution_record(snapshot=snapshot, manifest=manifest, targets=execution_targets,
                job_id='0' * 32, run_id=preview_run, seconds=seconds, deadline=preview_at+seconds,
                now=preview_at, spent=snapshot['spent']+seconds, jobs=len(snapshot['jobs'])+1, preview=True)
            descriptor = prepare_handoff_context(scope, build_native_handoff(context, preview), run_id=preview_run)
            target_url, _ = _request_goal(Scope.model_validate(body), descriptor, targets=execution_targets)
            profile = 'target_network' if any(t.startswith('tcp://') for t in execution_targets) else 'target_http'
            job, token = c.prepare_job(case_id, 'existing-pentestgpt-compose', profile, seconds,
                                       expected_binding=binding)
            run_id = 'chat-' + job['id']
            execution = c.native_execution_record(case_id, job['id'], token, 'existing-pentestgpt-compose',
                                                   profile, run_id, execution_targets)
            context = build_native_handoff(context, execution)
            descriptor = prepare_handoff_context(scope, context, run_id=run_id)
            plan = {**binding, 'tool': 'pentestgpt-agent', 'run_id': run_id,
                    'execution_mode': 'existing_compose_runtime', 'synthetic': False,
                    'phase_enforcement': 'prompt_only', 'native_full_access': True,
                    'scope_enforcement': 'launcher_allowlist_and_prompt_only_network_scope' if profile == 'target_network'
                                         else 'launcher_allowlist_and_existing_proxy',
                    'reduced_scope_enforced': False, 'network_ack': False,
                    'handoff_context_hash': context['context_hash'], 'handoff': descriptor,
                    'target': target_url, 'execution_targets': execution_targets, 'profile': profile,
                    'network_scope_enforcement': 'prompt_only', 'native_execution': execution}
            c.start_native_job(job['id'], token, 'existing-pentestgpt-compose', profile, plan)
            started = await self.runtime.start(run_id=run_id, scope=body, osint_context=context)
            c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', profile)
            task = asyncio.create_task(self._monitor(case_id, job, token, run_id, plan, started))
            self.tasks[job['id']] = (case_id, task)
            task.add_done_callback(lambda t: self._finished(job['id'], t))
            await asyncio.sleep(0)
            return {**c.get_job_status(case_id, job['id']), 'run_id': run_id,
                    'execution': 'existing_pentestgpt', 'phase_enforcement': 'prompt_only',
                    'native_full_access': True, 'reduced_scope_enforced': False,
                    'native_preflight':started.get('handoff',{}).get('native_preflight')}
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
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', plan.get('profile', 'target_http'))
                latest = await self.runtime.poll(run_id)
                c.validate_capability(job['id'], token, 'existing-pentestgpt-compose', plan.get('profile', 'target_http'))
                if latest.get('status') in {'completed', 'failed', 'stopped', 'missing_or_interrupted'}:
                    outcome = 'completed' if latest['status'] == 'completed' else 'failed'
                    break
                c.renew_native_job(job['id'], token, 'existing-pentestgpt-compose', plan.get('profile', 'target_http'))
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
                closure = closure_task.result()
                closed = closure.get('access_closed') is True
            except Exception:
                closure = {}
                closed = False
            if not closed:
                outcome = 'interrupted'
                c.stop(case_id)
            # Bound before recording; no raw auth/env or arbitrary filesystem
            # reads enter the operator's evidence store.
            receipt = redact_worker_output(json.dumps(latest, ensure_ascii=False))[:18000]
            c.finish_job(case_id, job['id'], outcome=outcome, synthetic=False,
                result={**plan, 'receipt': receipt, 'access_closed': closed,
                        'native_preflight':started.get('handoff',{}).get('native_preflight'),
                        'handoff_removed': closure.get('handoff_removed'),
                        'handoff_cleanup_error': closure.get('handoff_cleanup_error'),
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
