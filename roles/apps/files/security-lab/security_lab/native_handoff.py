"""Native checks admission, separate from the non-executable typed facade.

Only the Controller creates a reserved admission. The preview estimates the
same file before charging a job, but is never accepted by the native runner.
This record contains no capability/credentials and claims no network firewall.
"""
from __future__ import annotations

import json
import math
from typing import Literal

from pydantic import Field, model_validator

from .contracts import Contract, Denied, Scope, canonical, digest
from .native_targets import select_native_targets


class CaseAllowance(Contract):
    seconds_limit: int = Field(ge=1)
    spent_or_reserved_seconds: float = Field(ge=0, allow_inf_nan=False)
    unallocated_seconds: int = Field(ge=0)
    jobs_limit: int = Field(ge=1)
    used_or_reserved_jobs: int = Field(ge=1)
    unallocated_jobs: int = Field(ge=0)
    rps: int = Field(ge=1, le=10)
    concurrent_jobs: Literal[1] = 1

    @model_validator(mode='after')
    def accounting(self):
        if (self.unallocated_seconds != math.floor(self.seconds_limit - self.spent_or_reserved_seconds)
                or self.unallocated_jobs != self.jobs_limit - self.used_or_reserved_jobs):
            raise ValueError('Inconsistent native case allowance')
        return self


class NativeExecution(Contract):
    schema_version: Literal[1] = 1
    admission: Literal['preflight_only', 'controller_reserved']
    execution_mode: Literal['existing_compose_runtime'] = 'existing_compose_runtime'
    task_stage: Literal['checks'] = 'checks'
    case_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    job_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    run_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    scope_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')
    scope_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    scope_version: int = Field(ge=1)
    generation: int = Field(ge=1)
    profile: Literal['target_http', 'target_network']
    execution_targets: list[str] = Field(min_length=1, max_length=16)
    snapshot_at: float = Field(allow_inf_nan=False)
    reserved_at: float = Field(allow_inf_nan=False)
    deadline: float = Field(allow_inf_nan=False)
    reserved_seconds: int = Field(ge=1, le=1800)
    remaining_seconds_at_snapshot: int = Field(ge=1)
    case_allowance: CaseAllowance
    toolbox: Literal['existing_pentestgpt_native_shell'] = 'existing_pentestgpt_native_shell'
    transport: Literal['direct_native', 'configured_http_proxy']
    native_full_access: Literal[True] = True
    phase_enforcement: Literal['prompt_only'] = 'prompt_only'
    independent_network_enforcement: Literal[False] = False
    independent_tool_qualification: Literal[False] = False

    @model_validator(mode='after')
    def window(self):
        if (not self.reserved_at <= self.snapshot_at < self.deadline
                or not math.isclose(self.deadline - self.reserved_at, self.reserved_seconds, abs_tol=0.0001)
                or self.remaining_seconds_at_snapshot != math.floor(self.deadline - self.snapshot_at)
                or self.case_allowance.spent_or_reserved_seconds < self.reserved_seconds):
            raise ValueError('Invalid native reservation window')
        return self


def execution_record(*, snapshot: dict, manifest: dict, targets: list[str], job_id: str, run_id: str,
                     seconds: int, deadline: float, now: float, spent: float, jobs: int,
                     preview: bool = False) -> dict:
    """Host-only constructor; caller supplies actual Controller accounting."""
    scope = Scope.model_validate_json(manifest['body'])
    scope.check_window(now)
    selected = select_native_targets(scope, targets)
    network = any(target.startswith('tcp://') for target in selected)
    if deadline > min(scope.expires_at, scope.authorization.valid_until):
        raise Denied('Native reservation exceeds scope window')
    return NativeExecution(
        admission='preflight_only' if preview else 'controller_reserved',
        case_id=snapshot['id'], job_id=job_id, run_id=run_id,
        scope_id=manifest['id'], scope_hash=manifest['hash'], scope_version=manifest['version'],
        generation=snapshot['generation'], profile='target_network' if network else 'target_http',
        execution_targets=selected, snapshot_at=now, reserved_at=deadline-seconds,
        deadline=deadline, reserved_seconds=seconds, remaining_seconds_at_snapshot=math.floor(deadline-now),
        case_allowance=CaseAllowance(seconds_limit=scope.budget.seconds, spent_or_reserved_seconds=float(spent),
            unallocated_seconds=math.floor(scope.budget.seconds-spent), jobs_limit=scope.budget.jobs,
            used_or_reserved_jobs=jobs, unallocated_jobs=scope.budget.jobs-jobs,
            rps=scope.budget.rps),
        transport='direct_native' if network else 'configured_http_proxy',
    ).model_dump(mode='json')


def build_native_handoff(context: dict, execution: dict) -> dict:
    """Replace facade-only gates, retaining the complete verified source context."""
    packet = json.loads(canonical(context))
    if (packet.get('schema_version') != 1 or packet.get('execution_authorized') is not False
            or digest({k:v for k,v in packet.items() if k != 'context_hash'}) != packet.get('context_hash')):
        raise Denied('Native handoff requires a valid original context snapshot')
    # The typed facade still uses these exact fields. They do not describe the
    # admitted native shell and must not become contradictory native run gates.
    del packet['execution_authorized']
    del packet['available_tools']
    packet['schema_version'] = 2
    packet['execution_mode'] = 'existing_compose_runtime'
    packet['native_execution'] = NativeExecution.model_validate(execution).model_dump(mode='json')
    packet['execution_targets'] = execution['execution_targets']
    packet['policy'] = {k:v for k,v in packet['policy'].items() if k not in {
        'execution_rule', 'tool_rule', 'legacy_runtime_rule'}}
    packet['policy'].update(
        execution_rule='native_execution records Controller admission and allocation for this exact native checks job; '
                       'Controller owns revocation/leases; this file contains no secret capability or firewall receipt',
        tool_rule='The admitted toolbox is existing_pentestgpt_native_shell; installed CLI commands may perform only '
                  'the selected scoped checks within the reserved job; typed-worker qualification is a separate contract',
        runtime_rule='Native FULL_ACCESS phase and network restrictions are prompt-only; '
                     'this admission does not qualify typed workers or independently enforce arbitrary shell effects',
    )
    packet['context_hash'] = digest({k:v for k,v in packet.items() if k != 'context_hash'})
    validate_native_handoff(packet, run_id=execution['run_id'], allow_preview=True)
    return packet


def validate_native_handoff(packet: dict, *, run_id: str, allow_preview: bool = False,
                            now: float | None = None) -> NativeExecution:
    try:
        if (packet['schema_version'] != 2 or packet['stage'] != 'checks'
                or packet['execution_mode'] != 'existing_compose_runtime'
                or 'execution_authorized' in packet or 'available_tools' in packet
                or packet['policy'].get('poc_allowed') is not False
                or packet['policy'].get('scope_expansion_allowed') is not False
                or digest({k:v for k,v in packet.items() if k != 'context_hash'}) != packet['context_hash']):
            raise ValueError('Invalid native envelope')
        record = NativeExecution.model_validate(packet['native_execution'])
        if record.admission != 'controller_reserved' and not allow_preview:
            raise ValueError('Preview is not native execution admission')
        scope = Scope.model_validate(packet['scope_manifest'])
        if (digest(packet['scope_manifest']) != packet['binding']['hash']
                or scope.model_dump(mode='json') != packet['effective_scope']
                or scope.authorization.model_dump(mode='json') != packet['rules_of_engagement']
                or record.case_id != packet['case_id'] or record.generation != packet['generation']
                or record.run_id != run_id
                or (record.scope_id, record.scope_hash, record.scope_version) != (
                    packet['binding']['id'], packet['binding']['hash'], packet['binding']['version'])
                or record.execution_targets != packet['execution_targets']
                or select_native_targets(scope, record.execution_targets) != record.execution_targets):
            raise ValueError('Native admission differs from scope, run or selection')
        network = any(t.startswith('tcp://') for t in record.execution_targets)
        if (record.profile != ('target_network' if network else 'target_http')
                or record.transport != ('direct_native' if network else 'configured_http_proxy')
                or record.deadline > min(scope.expires_at, scope.authorization.valid_until)
                or record.reserved_seconds > scope.budget.seconds
                or record.case_allowance.seconds_limit != scope.budget.seconds
                or record.case_allowance.jobs_limit != scope.budget.jobs
                or record.case_allowance.rps != scope.budget.rps):
            raise ValueError('Native budget/transport differs from immutable scope')
        if now is not None:
            scope.check_window(now)
            if not record.snapshot_at <= now < record.deadline:
                raise ValueError('Native admission is expired or from the future')
        return record
    except (KeyError, TypeError, ValueError) as exc:
        raise Denied('Invalid native execution handoff: ' + str(exc)[:180]) from exc
