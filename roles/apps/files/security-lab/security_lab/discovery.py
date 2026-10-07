"""Bounded background discovery with durable per-source receipts and explicit gaps."""
from __future__ import annotations

import asyncio
import ipaddress
import time
from pydantic import ValidationError

from .contracts import Denied, hostname
from .discovery_contracts import SourceRequest, SourceResult
from .osint_sources import dns_requests, initial_requests, ip_requests, parse_source, select_registry
from .osint_transport import PublicSourceClient, body_hash

# First local credential-free acquisition profile. Longer/paged discovery and
# production job isolation are separate delivery; hitting a cap is visible.
MAX_REQUESTS = 64
MAX_HOSTS = 5
MAX_IPS = 3
MAX_SECONDS = 180


def omitted(request: SourceRequest, reason: str, clock=time.time, *, synthetic=False) -> SourceResult:
    return SourceResult(request=request, collected_at=float(clock()), status='not_attempted',
                        body_hash=body_hash(''), reason=reason, synthetic=synthetic)


async def collect_public_sources(controller, case_id: str, run_id: str, *, client=None,
                                 interval: float = 1, max_requests=MAX_REQUESTS,
                                 max_hosts=MAX_HOSTS, max_ips=MAX_IPS,
                                 max_seconds=MAX_SECONDS, monotonic=time.monotonic):
    client = client or PublicSourceClient()
    seed = controller.get_case(case_id)['seed']
    queue = initial_requests(seed)
    seen = {r.url for r in queue}
    hosts, ips = {seed}, set()
    gap_receipts = set()
    fetched, skipped = 0, 0
    incomplete = False
    deadline = monotonic() + max_seconds
    reason = 'bounded_public_source_plan_complete'
    synthetic = bool(getattr(client, 'synthetic', False))

    def enqueue(requests):
        for request in requests:
            if request.url not in seen:
                seen.add(request.url)
                queue.append(request)

    def active():
        return controller.run_active(case_id, run_id)

    def gap(request, why):
        nonlocal skipped
        skipped += 1
        key = (request.source, why)
        if key not in gap_receipts:
            gap_receipts.add(key)
            controller.record_source_result(case_id, run_id, omitted(request, why, synthetic=synthetic))

    try:
        while queue and controller.run_active(case_id, run_id):
            request = queue.pop(0)
            if fetched >= max_requests or monotonic() >= deadline:
                reason = 'request_or_time_budget_exhausted'
                gap(request, reason)
                continue
            if fetched and interval:
                await asyncio.sleep(interval)
            if not active():
                continue
            if monotonic() >= deadline:
                reason = 'request_or_time_budget_exhausted'
                gap(request, reason)
                continue
            result = await asyncio.to_thread(client.fetch, request, active=active,
                timeout_seconds=max(.01, min(8, deadline - monotonic())))
            fetched += 1
            if not controller.run_active(case_id, run_id):
                return controller.finish_discovery(case_id, run_id, status='cancelled', reason='case_stopped')
            if result.status == 'success':
                try:
                    parsed = parse_source(request, result.body)
                    result = result.model_copy(update={
                        'status': parsed.status, 'reason': parsed.reason,
                        'truncated': parsed.truncated or result.truncated,
                        'observations': parsed.observations, 'relations': parsed.relations,
                    })
                except (ValueError, KeyError, TypeError, ValidationError):
                    result = result.model_copy(update={'status': 'error', 'reason': 'source_parser_error'})
            controller.record_source_result(case_id, run_id, result)
            incomplete = incomplete or result.status not in {'success', 'empty'}
            if result.status not in {'success', 'empty', 'partial'} or not result.observations:
                # Bootstrap contains routing metadata rather than surface nodes.
                if request.source != 'rdap_bootstrap' or result.status not in {'success', 'empty'}:
                    continue
            if request.source == 'rdap_bootstrap' and result.status in {'success', 'empty'}:
                try:
                    serving = select_registry(seed, result.body)
                    if serving is not None:
                        client.authorize_registry(serving)
                        enqueue([serving])
                    else:
                        controller.record_source_result(case_id, run_id,
                            omitted(request.model_copy(update={'source': 'rdap_domain', 'parser': 'rdap_domain'}),
                                    'no_serving_registry_for_seed', synthetic=synthetic))
                except (ValueError, TypeError, KeyError):
                    controller.record_source_result(case_id, run_id,
                        omitted(request.model_copy(update={'source': 'rdap_domain', 'parser': 'rdap_domain'}),
                                'invalid_registry_bootstrap', synthetic=synthetic))
            for observation in result.observations:
                if observation.kind in {'hostname', 'domain'}:
                    try:
                        host = hostname(observation.value)
                    except ValueError:
                        continue
                    if host not in hosts and (host == seed or host.endswith('.' + seed)):
                        if len(hosts) < max_hosts:
                            hosts.add(host)
                            enqueue(dns_requests(host))
                        else:
                            for query in dns_requests(host)[:1]:
                                if query.url not in seen:
                                    seen.add(query.url)
                                    gap(query, 'hostname_pivot_budget_exhausted')
                elif observation.kind == 'ip':
                    try:
                        address = ipaddress.ip_address(observation.value)
                    except ValueError:
                        continue
                    if address.is_global and str(address) not in ips:
                        if len(ips) < max_ips:
                            ips.add(str(address))
                            enqueue(ip_requests(str(address)))
                        else:
                            for query in ip_requests(str(address))[:1]:
                                if query.url not in seen:
                                    seen.add(query.url)
                                    gap(query, 'ip_pivot_budget_exhausted')
        if not controller.run_active(case_id, run_id):
            return controller.finish_discovery(case_id, run_id, status='cancelled', reason='case_stopped')
        return controller.finish_discovery(case_id, run_id, status='partial' if skipped or incomplete else 'completed',
            reason=reason + (f'; {skipped} unperformed requests' if skipped else ''))
    except asyncio.CancelledError:
        controller.finish_discovery(case_id, run_id, status='interrupted', reason='MCP_session_closed')
        raise
    except Denied:
        if controller.run_active(case_id, run_id):
            controller.finish_discovery(case_id, run_id, status='failed', reason='discovery_policy_or_store_denied')
        return controller.get_case(case_id)
    except Exception:
        # Never expose arbitrary exception text/credentials as a source result.
        controller.finish_discovery(case_id, run_id, status='failed', reason='discovery_internal_error')
        raise
