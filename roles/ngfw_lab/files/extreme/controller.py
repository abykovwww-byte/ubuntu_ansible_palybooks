"""Execute one prepared extreme window under the existing laboratory lock/guards.

No profile, snapshot, route, policy or native collector mutation is performed.
Receipt producer contracts are documented in README.md. Missing proof blocks load.
"""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner as legacy
import measurement
try:
    from . import plan, held_evidence
except ImportError:
    import plan
    import held_evidence


class Blocked(legacy.Abort):
    pass


def utc():
    return datetime.now(timezone.utc).isoformat()


def epoch(value):
    if not isinstance(value, str):
        raise Blocked('receipt timestamp must be timezone-aware RFC3339')
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise Blocked('receipt timestamp lacks timezone')
    return stamp.timestamp()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def artifact(ref, base):
    """Read an explicit evidence file only after checking its immutable digest."""
    if not isinstance(ref, dict) or not re.fullmatch('[a-f0-9]{64}', ref.get('sha256', '')):
        raise Blocked('evidence reference requires path and SHA256')
    if not isinstance(ref.get('path'), str) or not ref['path']:
        raise Blocked('evidence reference path is absent')
    path = Path(ref['path'])
    if not path.is_absolute():
        path = Path(base) / path
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != ref['sha256']:
        raise Blocked('evidence artifact hash changed: ' + path.name)
    return payload


def reference_path(ref, base):
    path = Path(ref['path'])
    return path if path.is_absolute() else Path(base) / path


def validate_clock(ref, base, query_ref, raw, boot_id):
    """Recompute host-minus-guest bounds from hashed clock/query evidence."""
    alignment = json.loads(artifact(ref, base))
    if not isinstance(alignment, dict):
        raise Blocked('clock alignment must be an evidence object')
    clock_base = reference_path(ref, base).parent
    if (alignment.get('schema_version') != 1 or alignment.get('guest_boot_id') != boot_id
            or alignment.get('scope') != 'bounded_clock_alignment_not_ntp_synchronization'
            or alignment.get('query_evidence_sha256') != query_ref['sha256']
            or alignment.get('query_bounds') != raw.get('query_bounds')):
        raise Blocked('clock alignment/query identity mismatch')
    limits = alignment.get('limits', {})
    if not isinstance(limits, dict):
        raise Blocked('clock limits unavailable')
    for key, ceiling in (('max_uncertainty_seconds', 1), ('max_offset_spread_seconds', .5),
                         ('max_clock_step_seconds', .05)):
        if not measurement.number(limits.get(key)) or not 0 < limits[key] <= ceiling:
            raise Blocked('clock alignment limits absent or wider than qualification gate')
    step_ns = limits['max_clock_step_seconds'] * 1e9
    rows, bounds = [], []
    guest_keys = ('guest_epoch_ns', 'guest_monotonic_ns', 'guest_boot_id')
    for name in ('before', 'after'):
        row = alignment.get(name, {})
        if not isinstance(row, dict):
            raise Blocked('clock sample unavailable')
        for key in ('host_send_epoch_ns', 'host_receive_epoch_ns', 'host_send_monotonic_ns',
                    'host_receive_monotonic_ns', 'guest_epoch_ns', 'guest_monotonic_ns'):
            if type(row.get(key)) is not int or row[key] < 0:
                raise Blocked('clock sample requires integer nanosecond evidence')
        guest = json.loads(artifact(row.get('guest_evidence'), clock_base))
        if (not isinstance(guest, dict) or set(guest) != set(guest_keys) or any(guest[key] != row.get(key) for key in guest_keys)
                or row.get('guest_boot_id') != boot_id):
            raise Blocked('clock guest raw evidence/boot mismatch')
        elapsed = row['host_receive_monotonic_ns'] - row['host_send_monotonic_ns']
        wall_elapsed = row['host_receive_epoch_ns'] - row['host_send_epoch_ns']
        if elapsed < 0 or wall_elapsed < 0 or abs(wall_elapsed - elapsed) > step_ns:
            raise Blocked('host clock chronology unverified within alignment sample')
        if wall_elapsed / 2e9 > limits['max_uncertainty_seconds']:
            raise Blocked('clock alignment uncertainty exceeds limit')
        bounds.append((row['host_send_epoch_ns'] - row['guest_epoch_ns'],
                       row['host_receive_epoch_ns'] - row['guest_epoch_ns']))
        rows.append(row)
    before, after = rows
    query = alignment['query_bounds']
    if not isinstance(query, dict):
        raise Blocked('query clock bounds unavailable')
    for key in ('started_epoch_ns', 'completed_epoch_ns', 'started_monotonic_ns', 'completed_monotonic_ns'):
        if type(query.get(key)) is not int or query[key] < 0:
            raise Blocked('query clock bounds unavailable')
    for domain in ('epoch', 'monotonic'):
        if not (before['host_receive_' + domain + '_ns'] <= query['started_' + domain + '_ns']
                <= query['completed_' + domain + '_ns'] <= after['host_send_' + domain + '_ns']):
            raise Blocked('DB query is not bracketed by clock samples')
    if abs((query['completed_epoch_ns'] - query['started_epoch_ns'])
           - (query['completed_monotonic_ns'] - query['started_monotonic_ns'])) > step_ns:
        raise Blocked('host clock chronology unverified during DB query')
    for wall, mono in (('guest_epoch_ns', 'guest_monotonic_ns'),
                       ('host_send_epoch_ns', 'host_send_monotonic_ns'),
                       ('host_receive_epoch_ns', 'host_receive_monotonic_ns')):
        if after[mono] <= before[mono] or abs((after[wall] - before[wall])
                                           - (after[mono] - before[mono])) > step_ns:
            raise Blocked('clock chronology unverified across query alignment')
    low, high = min(row[0] for row in bounds), max(row[1] for row in bounds)
    if high - low > limits['max_offset_spread_seconds'] * 1e9:
        raise Blocked('clock offset spread exceeds query limit')
    if alignment.get('offset_min_ns') != low or alignment.get('offset_max_ns') != high:
        raise Blocked('declared clock offset differs from raw brackets')
    return dict(offset_min_ns=low, offset_max_ns=high, uncertainty_seconds=(high-low)/2e9,
                clock_alignment_sha256=ref['sha256'], alignment=alignment,
                scope='bounded_clock_alignment_not_ntp_synchronization')


def validate_audit_baseline(path, before, now, expected_profile):
    """An explicit new qualification epoch retains, never clears, historical loss."""
    path = Path(path)
    baseline_bytes = path.read_bytes()
    baseline = json.loads(baseline_bytes)
    if not isinstance(baseline, dict) or baseline.get('schema_version') != 1:
        raise Blocked('audit loss baseline schema unavailable')
    if baseline.get('profile') not in ('A', 'B') or baseline['profile'] != expected_profile:
        raise Blocked('audit loss baseline profile differs from selected window')
    for field in ('rules_sha256', 'auditd_conf_sha256'):
        if not isinstance(baseline.get(field), str) or not re.fullmatch(r'[0-9a-f]{64}', baseline[field]):
            raise Blocked('audit loss baseline rules/config hash unavailable')
    if (type(baseline.get('lost')) is not int or baseline['lost'] < 0
            or not 0 <= now - epoch(baseline.get('readback_at')) <= 60):
        raise Blocked('audit loss baseline invalid/stale; fresh raw read-back required')
    incident = baseline.get('incident_ref')
    if not isinstance(incident, str) or (baseline['lost'] and not incident.strip()):
        raise Blocked('historical audit loss requires an explicit incident reference')
    source_bytes = artifact(baseline.get('source_evidence'), path.parent)
    source = json.loads(source_bytes)
    if not isinstance(source, dict) or source.get('collected_at') != baseline['readback_at']:
        raise Blocked('audit loss baseline read-back chronology mismatch')
    if source.get('profile') != baseline['profile']:
        raise Blocked('audit loss baseline source profile differs')
    snapshot = source.get('snapshot', {})
    fixed = baseline.get('fixed_controls')
    if not isinstance(fixed, dict) or set(fixed) != set(measurement.AUDIT_FIXED_CONTROLS):
        raise Blocked('audit loss baseline fixed controls unavailable')
    for current in (snapshot, before):
        if (current is before and isinstance(current, dict)
                and current.get('boot_id') == baseline.get('guest_boot_id')
                and isinstance(current.get('audit'), dict) and type(current['audit'].get('lost')) is int):
            if current['audit']['lost'] > baseline['lost']:
                raise legacy.WindowStop('GLOBAL_STOP', 'AuditD new loss since verified qualification epoch')
            if current['audit']['lost'] < baseline['lost']:
                raise Blocked('AuditD loss counter reset/regressed; window invalid')
        validate_audit_epoch_inventory(baseline, current)
        if (not isinstance(current, dict) or current.get('boot_id') != baseline.get('guest_boot_id')
                or not isinstance(current.get('audit'), dict)
                or type(current['audit'].get('lost')) is not int or current['audit']['lost'] != baseline['lost']
                or any(type(fixed[key]) is not int or fixed[key] < 0
                       or type(current['audit'].get(key)) is not int or current['audit'][key] != fixed[key]
                       for key in measurement.AUDIT_FIXED_CONTROLS)):
            raise Blocked('audit loss baseline differs from raw/current boot/counters/controls')
    measurement.measure_probe.validate_process_boot_id(baseline.get('guest_boot_id'))
    if fixed['enabled'] != 1 or fixed['failure'] not in (0, 1) or not fixed['pid']:
        raise Blocked('audit loss baseline has unsafe fixed controls')
    return dict(baseline=baseline, baseline_sha256=hashlib.sha256(baseline_bytes).hexdigest(),
                source_sha256=baseline['source_evidence']['sha256'],
                scope='new_qualification_epoch_preserving_historical_incident'), baseline_bytes, source_bytes


def validate_audit_epoch_inventory(baseline, snapshot):
    inventory = snapshot.get('inventory') if isinstance(snapshot, dict) else None
    if not isinstance(inventory, dict) or any(
            inventory.get(field) != baseline[field] for field in ('rules_sha256', 'auditd_conf_sha256')):
        raise Blocked('audit loss epoch rules/config identity changed or unavailable; window invalid')


def validate_audit_epoch_identity(baseline, identity):
    profile = identity.get('profile_evidence') if isinstance(identity, dict) else None
    if (not isinstance(profile, dict) or identity.get('profile') != baseline['profile']
            or any(profile.get(field) != baseline[field] for field in ('rules_sha256', 'auditd_conf_sha256'))):
        raise Blocked('audit loss epoch profile/rules identity changed or unavailable; window invalid')


class EpochAuditGuard(legacy.Guard):
    def __init__(self, limits, baseline):
        super().__init__(limits, True, 'warn')
        self.loss_baseline = baseline

    def audit_loss_reasons(self, audit):
        if audit.get('boot_id') != self.loss_baseline['guest_boot_id']:
            return ['AuditD epoch boot changed; window invalid']
        value = audit.get('audit_lost')
        if type(value) is not int or value < self.loss_baseline['lost']:
            return ['AuditD loss counter reset/regressed or unavailable; window invalid']
        if value > self.loss_baseline['lost']:
            return ['AuditD new loss since verified qualification epoch']
        return []

    def check(self, sample):
        reasons = super().check(sample)
        return ['appliance boot identity changed; window invalid' if reason == 'appliance rebooted' else reason
                for reason in reasons]


def window_clock_summary(before, after, measurement_bounds=None):
    first, last = before['clock_alignment'], after['clock_alignment']
    low = last['offset_min_ns'] - first['offset_max_ns']
    high = last['offset_max_ns'] - first['offset_min_ns']
    summary = dict(before_alignment_sha256=first['clock_alignment_sha256'],
                   after_alignment_sha256=last['clock_alignment_sha256'],
                   drift_min_seconds=low / 1e9, drift_max_seconds=high / 1e9,
                   limit_seconds=1, status='BOUNDED', raw_source_timestamps_unchanged=True)
    def reject(reason):
        error = Blocked(reason)
        error.clock_summary = dict(summary, status='INVALID', reason=reason)
        raise error
    if measurement_bounds is not None:
        for domain in ('epoch', 'monotonic'):
            if (first['alignment']['after']['host_receive_' + domain + '_ns'] > measurement_bounds['started_' + domain + '_ns']
                    or last['alignment']['before']['host_send_' + domain + '_ns'] < measurement_bounds['completed_' + domain + '_ns']):
                reject('before/after clock samples do not enclose measured window')
        if abs((measurement_bounds['completed_epoch_ns'] - measurement_bounds['started_epoch_ns'])
               - (measurement_bounds['completed_monotonic_ns'] - measurement_bounds['started_monotonic_ns'])) > 50_000_000:
            reject('host clock chronology unverified: realtime/monotonic divergence over measured window')
    # A separate window gate: per-query tight alignment alone is insufficient.
    if max(abs(low), abs(high)) > 1_000_000_000:
        reject('before/after window clock drift bound exceeds1s')
    return summary


def validate_native(path, boot_id, now):
    """Validate fresh DB read-back, not a boolean declaring collection healthy."""
    path = Path(path)
    receipt = read_json(path)
    if receipt.get('schema_version') != 2 or receipt.get('status') != 'HEALTHY':
        raise Blocked('native health producer has not confirmed aligned collection')
    interval = receipt.get('source_interval_seconds')
    if type(interval) not in (int, float) or not 1 <= interval <= 60:
        raise Blocked('native receipt lacks verified PT source interval')
    freshness = 2 * interval + 10
    if not 0 <= now - epoch(receipt.get('collected_at')) <= freshness:
        raise Blocked('native-health query receipt is stale or in the future')
    raw = json.loads(artifact(receipt.get('query_evidence'), path.parent))
    if (raw.get('guest_boot_id') != boot_id or receipt.get('guest_boot_id') != boot_id
            or not raw.get('database') or raw.get('database') != receipt.get('database')
            or raw.get('partial') is not False):
        raise Blocked('native query identity/database/completeness unavailable or changed')
    clock = validate_clock(receipt.get('clock_alignment'), path.parent, receipt['query_evidence'], raw, boot_id)
    if any(clock['alignment'][side]['guest_monotonic_ns'] < freshness * 1e9 for side in ('before', 'after')):
        raise Blocked('guest boot too recent for native freshness; points have no boot tag')
    expected = receipt.get('required_measurements')
    if not isinstance(expected, list) or not expected or not all(isinstance(v, str) and v for v in expected):
        raise Blocked('native required measurement catalog is absent')
    points = raw.get('points')
    if not isinstance(points, list) or not points:
        raise Blocked('native DB query returned no points')
    newest, newest_ns = {}, {}
    for point in points:
        stamp = epoch(point['time'])
        ns = point.get('source_timestamp_ns')
        if type(ns) is not int or abs(ns / 1e9 - stamp) > .000001:
            raise Blocked('native raw timestamp nanosecond identity unavailable')
        name = point['measurement']
        newest[name] = max(newest.get(name, 0), stamp)
        newest_ns[name] = max(newest_ns.get(name, ns), ns)
    age_ranges = {name: {'min_seconds': (int(now * 1e9) - stamp - clock['offset_max_ns']) / 1e9,
                         'max_seconds': (int(now * 1e9) - stamp - clock['offset_min_ns']) / 1e9}
                  for name, stamp in newest_ns.items()}
    if any(name not in age_ranges or not 0 <= age_ranges[name]['min_seconds']
           <= age_ranges[name]['max_seconds'] <= min(30, freshness) for name in expected):
        raise Blocked('native required measurements missing/stale')
    return dict(receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                query_sha256=receipt['query_evidence']['sha256'], collected_at=receipt['collected_at'],
                source_interval_seconds=interval, database=receipt['database'], newest=newest,
                query_response=raw, clock_alignment=clock, age_ranges=age_ranges,
                scope='freshness_only_not_full_window_completeness')


def validate_identity(receipt, campaign, window, before, topology, base, now):
    if not 0 <= now - epoch(receipt.get('collected_at')) <= 60:
        raise Blocked('identity read-back older than60s or future')
    if receipt.get('profile') != window['profile'] or receipt.get('guest_boot_id') != before.get('boot_id'):
        raise Blocked('actual profile/boot identity differs from selected window')
    profile = receipt.get('profile_evidence', {})
    if window['profile'] == 'A':
        if profile.get('edr_installed') is not False or profile.get('audit_profile') != 'stock':
            raise Blocked('A requires package absence and stock administrative audit read-back')
    elif (profile.get('edr_installed') is not True or profile.get('audit_profile') != 'full'
          or profile.get('audit_rule_count') != 116):
        raise Blocked('B requires installed EDR and116 effective AuditD rules')
    inv = before.get('inventory', {})
    if (profile.get('rules_sha256') != inv.get('rules_sha256') or not inv.get('rules_sha256')
            or profile.get('auditd_conf_sha256') != inv.get('auditd_conf_sha256')):
        raise Blocked('profile evidence differs from live effective AuditD inventory')
    refs = receipt.get('artifacts', {})
    if json.loads(artifact(refs.get('profile'), base)) != profile:
        raise Blocked('profile evidence content differs from raw read-back')
    if receipt.get('config_hashes') != campaign['invariants']:
        raise Blocked('actual invariant hashes differ from campaign')
    for name in plan.HASH_FIELDS:
        payload = artifact(refs.get(name), base)
        if name == 'topology':
            if json.loads(payload) != topology or plan.fingerprint(topology) != campaign['invariants'][name]:
                raise Blocked('current topology differs from receipt/campaign')
        elif hashlib.sha256(payload).hexdigest() != campaign['invariants'][name]:
            raise Blocked('actual invariant artifact differs: ' + name)
    if not isinstance(receipt.get('versions'), dict) or not receipt['versions']:
        raise Blocked('current versions absent')
    return receipt


def validate_stable_window(campaign, window, base, now):
    """Endurance selection must reference actual validated prior window evidence.

The selected artifact is the derived receipt, retaining its hashed original
controller receipt and native export's hashed workload bytes. A stable label
does not establish a maximum capacity, and never substitutes for 3600s control.
"""
    from native_analysis.normalize import iperf_observation, extreme_observation
    from extreme.control_capacity import capacity_limits, performance_errors, mixed_timing
    ref = window.get('stable_receipt')
    stable = json.loads(artifact(ref, base))
    stable_base = reference_path(ref, base).parent
    if (stable.get('valid') is not True or stable.get('window_validated') is not True
            or stable.get('regime') != 'stable' or stable.get('outcome') != 'STABLE_LIMIT_NOT_ESTABLISHED'
            or stable.get('profile') != window['profile'] or stable.get('offered_load') != window['offered']
            or stable.get('config_hashes') != campaign['invariants']
            or not stable.get('run_id') or stable['run_id'] == window['run_id']):
        raise Blocked('endurance stable receipt profile/load/outcome/invariants differ')
    original_ref = stable.get('original_controller')
    original = json.loads(artifact(original_ref, stable_base))
    for key in ('run_id', 'profile', 'offered_load', 'config_hashes', 'profile_evidence', 'components'):
        if original.get(key) != stable.get(key): raise Blocked('stable source identity differs: ' + key)
    if original.get('outcome') != 'COMPLETED' or original.get('recovery', {}).get('status') != 'RECOVERED':
        raise Blocked('stable source did not complete and recover')
    profile = campaign['profiles'][window['profile']]
    if any(stable.get('profile_evidence', {}).get(key) != value for key, value in profile.items()):
        raise Blocked('stable profile evidence differs from campaign')
    native = stable.get('native_completeness', {})
    if (native.get('status') != 'COMPLETE_WITHIN_DECLARED_CATALOG_AND_CADENCE'
            or native.get('scope') != 'native_telemetry' or type(native.get('expected')) is not int
            or native['expected'] <= 0 or native.get('observed') != native['expected']
            or native.get('missing') != 0 or native.get('gaps') or not native.get('evidence')):
        raise Blocked('stable native completeness absent')
    hashes = set()
    for evidence in native['evidence']:
        artifact(evidence, stable_base); hashes.add(evidence['sha256'])
    offered = window['offered']
    parts = offered.get('components', [offered])
    components = [c for c in original.get('components', []) if c.get('phase') == 'measure']
    if len(parts) != len(components): raise Blocked('stable workload component count differs')
    retained = stable.get('stable_workload_evidence')
    if not isinstance(retained, list) or len(retained) != len(components):
        raise Blocked('stable explicit workload evidence mapping absent')
    criterion_ref = stable.get('stable_criterion')
    criterion = json.loads(held_evidence.referenced(criterion_ref, stable_base))
    seconds = criterion.get('duration_seconds')
    if criterion.get('offered') != offered or type(seconds) is not int or seconds < 300:
        raise Blocked('stable criterion offered load or duration differs')
    limits = capacity_limits(criterion.get('thresholds', {}))
    actual_seconds = plan.component_seconds(offered, seconds)
    if offered['kind'] == 'mixed':
        if (criterion.get('component_measure_seconds') != actual_seconds
                or stable.get('component_measure_seconds') != actual_seconds
                or original.get('component_measure_seconds') != actual_seconds
                or stable.get('measure_seconds') != seconds or original.get('measure_seconds') != seconds):
            raise Blocked('stable mixed nominal/component duration contract differs')
    declared = epoch(criterion.get('declared_at') or criterion.get('started_at'))
    if declared > epoch(original.get('process_start')):
        raise Blocked('stable criterion was not declared before source window')
    expected_commands = plan.commands(offered, campaign['target'], seconds, stable['run_id'], campaign.get('source_ips', []))
    durations, mixed_observations = [], []
    for index, (part, component, evidence) in enumerate(zip(parts, components, retained)):
        if component.get('kind') != part['kind'] or component.get('exit_code') != 0:
            raise Blocked('stable workload component failed')
        if offered['kind'] == 'mixed' and component.get('requested_duration_seconds') != actual_seconds:
            raise Blocked('stable mixed component requested duration differs')
        argv = component.get('argv', [])
        duration_arg = '-t' if part['kind'] in ('tcp', 'udp') else '--duration'
        try: requested_seconds = int(argv[argv.index(duration_arg) + 1])
        except (ValueError, IndexError, TypeError): raise Blocked('stable workload invocation absent') from None
        if requested_seconds != actual_seconds or argv != expected_commands[index]:
            raise Blocked('stable workload invocation differs from offered load')
        if (not isinstance(evidence, dict) or evidence.get('component_path') != component.get('path')
                or evidence.get('kind') != part['kind']):
            raise Blocked('stable workload evidence association differs')
        raw = held_evidence.referenced(evidence.get('reference'), stable_base)
        if hashlib.sha256(raw).hexdigest() not in hashes:
            raise Blocked('stable workload lacks retained hash evidence')
        if part['kind'] in ('tcp', 'udp'):
            source_load = json.loads(raw).get('start', {}).get('test_start', {})
            parallel = part.get('parallel', 1)
            if (source_load.get('num_streams') != parallel
                    or source_load.get('target_bitrate') != int(part['bps'] / parallel)
                    or source_load.get('duration') != actual_seconds
                    or part['kind'] == 'udp' and source_load.get('blksize') != part['payload_bytes']):
                raise Blocked('stable actual iperf offered load differs')
            measured, observation, errors = iperf_observation(raw, part['kind'], {})
        else:
            measured, observation, errors = extreme_observation(raw, stable['run_id'], part['kind'])
            expected_rate = part['open_rate'] if part['kind'] == 'held' else part['rate']
            if observation.get('final', {}).get('requested_rate') != expected_rate:
                raise Blocked('stable actual offered request rate differs')
        errors.extend(performance_errors(part, measured, observation, actual_seconds, limits))
        if errors: raise Blocked('stable actual workload invalid: ' + ','.join(errors))
        duration = epoch(measured['end']) - epoch(measured['start'])
        if duration < requested_seconds or epoch(measured['end']) > now:
            raise Blocked('stable actual workload duration or chronology invalid')
        if part['kind'] == 'held':
            held_evidence.verify(stable.get('held_receiver_evidence'), stable_base, raw, part, stable['run_id'])
        durations.append(duration)
        mixed_observations.append(dict(actual=measured, observation=observation))
    if len(parts) > 1: mixed_timing(mixed_observations, seconds)
    return {'source': ref, 'original_controller': original_ref, 'run_id': stable['run_id'],
            'stable_criterion': criterion_ref,
            'profile': stable['profile'], 'load_hash': plan.fingerprint(offered),
            'actual_component_seconds': durations, 'scope': 'validated_stable_load_not_maximum_capacity'}


def validate_campaign_artifacts(campaign, window, base, backend, config, now):
    def scoped(raw, name):
        if (raw.get('kind') != name or raw.get('scope') != 'isolated-ngfw-lab'
                or raw.get('campaign_id') != campaign['campaign_id'] or raw.get('target') != campaign['target']
                or raw.get('config_hashes') != campaign['invariants']
                or not 0 <= now - epoch(raw.get('observed_at')) <= 86400):
            raise Blocked('prerequisite receipt scope/identity/age differs: ' + name)
        evidence = raw.get('evidence')
        if not isinstance(evidence, list) or not evidence:
            raise Blocked('prerequisite receipt has no raw evidence: ' + name)
        for reference in evidence:
            artifact(reference, base)
    snapshots = {}
    for name in ('isolation', 'snapshot_A', 'snapshot_B', 'rollback', 'profile_A', 'profile_B',
                 'native_collection', 'observer_impact', 'resource_reservations'):
        raw = json.loads(artifact(campaign['receipts'].get(name), base))
        scoped(raw, name)
        if name.startswith('snapshot_'):
            snapshot = raw.get('snapshot_name')
            if (not isinstance(snapshot, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}', snapshot)
                    or raw.get('vm') != config['ngfw_vm'] or raw.get('creation_state') != 'shut off'):
                raise Blocked('powered-off rollback snapshot identity missing')
            text = backend.call(['virsh', '-c', 'qemu:///system', 'snapshot-info', config['ngfw_vm'], snapshot])
            fields = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
            if (fields.get('Name', '').strip() != snapshot or fields.get('Domain', '').strip() != config['ngfw_vm']
                    or fields.get('State', '').strip() not in ('shutoff', 'shut off')):
                raise Blocked('current virsh snapshot-info does not confirm powered-off snapshot')
            snapshots[name] = dict(name=snapshot, current_readback=text)
        elif name == 'rollback':
            if not isinstance(raw.get('steps'), list) or not raw['steps'] or not raw.get('restore_verification_evidence'):
                raise Blocked('rollback procedure/restore verification missing')
            artifact(raw['restore_verification_evidence'], base)
        elif name == 'observer_impact':
            if raw.get('observer_configuration_sha256') != campaign['invariants']['observation']:
                raise Blocked('observer-impact receipt describes another observer configuration')
        elif name == 'resource_reservations':
            headroom = raw.get('headroom', {})
            for key in ('fd_slots', 'source_port_tuples', 'receiver_sessions', 'host_memory_bytes'):
                if type(headroom.get(key)) not in (int, float) or headroom[key] <= 0:
                    raise Blocked('resource headroom not proven: ' + key)
    selected = next((w for w in campaign['workloads'] if w['offered'] == window['offered']), None)
    if selected is None:
        raise Blocked('selected workload has no control-path evidence')
    raw = json.loads(artifact(selected['control_capacity'].get('receipt'), base))
    scoped(raw, 'control_capacity')
    if (raw.get('status') != 'VALID' or raw.get('load_hash') != plan.fingerprint(window['offered'])
            or raw.get('topology_hash') != campaign['invariants']['topology']
            or raw.get('offered_achieved') is not True or raw.get('resources_unlimited') is not True
            or type(raw.get('duration_seconds')) not in (int, float)
            or raw['duration_seconds'] < window['measure_seconds']
            or raw.get('path_kind') != 'direct_control' or raw.get('traverses_ngfw') is not False
            or raw.get('control_topology_hash') in (None, campaign['invariants']['topology'])
            or raw.get('endpoint_config_hashes') != {k: campaign['invariants'][k] for k in ('generator', 'receiver')}):
        raise Blocked('actual direct-path receipt does not support selected full-duration load')
    if window['offered']['kind'] == 'mixed':
        timing = raw.get('mixed_measurement_window', {})
        if (raw.get('component_measure_seconds') != plan.component_seconds(window['offered'], raw['duration_seconds'])
                or timing.get('measure_seconds') != raw['duration_seconds']
                or timing.get('component_measure_seconds') != raw['component_measure_seconds']
                or type(timing.get('conservative_common_seconds')) not in (int, float)
                or not math.isfinite(timing['conservative_common_seconds'])
                or timing['conservative_common_seconds'] < raw['duration_seconds']):
            raise Blocked('direct mixed full concurrent duration evidence absent')
    control_topology = json.loads(artifact(raw.get('control_topology_evidence'), base))
    if plan.fingerprint(control_topology) != raw['control_topology_hash']:
        raise Blocked('direct control topology evidence differs')
    if window.get('phase') == 'endurance':
        snapshots['endurance_stable_window'] = validate_stable_window(campaign, window, base, now)
    return snapshots


def selected_window(campaign, run_id):
    full = plan.materialize(campaign)
    matches = [w for w in full['windows'] + full['endurance'] if w['run_id'] == run_id]
    if len(matches) != 1:
        raise Blocked('choose exactly one existing manifest window')
    window = matches[0]
    # Readiness for this load; unselected other workloads do not grant or block it.
    selected = [w for w in campaign['workloads'] if w['offered'] == window['offered']]
    if not selected:
        raise Blocked('endurance load needs a matching calibrated workload entry')
    blockers = plan.readiness(dict(campaign, workloads=selected))
    if blockers:
        raise Blocked('; '.join(blockers))
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', run_id):
        raise Blocked('unsafe run identifier')
    return window


class NativeRunner(legacy.Runner):
    def capture_held_receiver(self):
        row = {'phase': 'measure', 'host_requested_at': time.time()}
        try:
            row.update(json.loads(self.backend.endpoint('traffic-server', ['python3', '-c', held_evidence.PROBE], timeout=5)))
        except Exception as error:
            row['error'] = type(error).__name__
            raise Blocked('independent held receiver readback failed') from error
        finally:
            row['host_received_at'] = time.time()
            with (self.directory / 'held-receiver.ndjson').open('a', encoding='utf-8') as output:
                output.write(json.dumps(row) + '\n'); output.flush(); os.fsync(output.fileno())

    def monitor(self):
        try:
            health = validate_native(self.native_health, self.boot_id, time.time())
        except (Blocked, OSError, ValueError, KeyError, TypeError) as error:
            failure = dict(time=utc(), phase=self.context.get('phase'), outcome='INVALID_MEASUREMENT', reason=str(error))
            try:
                failure['receipt'] = read_json(self.native_health)
            except (OSError, ValueError):
                failure['receipt'] = None
            with (self.directory / 'native-health-failures.ndjson').open('a', encoding='utf-8') as output:
                output.write(json.dumps(failure) + '\n')
                output.flush()
                os.fsync(output.fileno())
            raise Blocked('native collection health invalid: ' + str(error)) from error
        with (self.directory / 'native-health.ndjson').open('a', encoding='utf-8') as output:
            output.write(json.dumps(dict(time=utc(), **health)) + '\n')
        try:
            super().monitor()
        finally:
            if getattr(self, 'held_capture', False): final_held_readback(self)


def final_held_readback(monitor, terminal=False):
    """Archive readback failure without replacing an in-flight safety exception."""
    primary = sys.exc_info()[1]
    try:
        if terminal: time.sleep(1.1)  # receiver publishes once per second
        monitor.capture_held_receiver()
    except Exception:
        if primary is None: raise
    finally:
        if terminal: monitor.held_capture = False


def execute_measurement(monitor, window, campaign, report):
    held = window['offered']['kind'] == 'held'
    try:
        if held:
            monitor.held_capture = True
            monitor.capture_held_receiver()
        execute_components(monitor, window, campaign, 'measure', window['measure_seconds'], report)
    finally:
        if held: final_held_readback(monitor, terminal=True)
    if window['offered']['kind'] == 'mixed':
        from native_analysis.normalize import iperf_observation, extreme_observation
        from extreme.control_capacity import mixed_timing
        observations = []
        for component in report['components']:
            if component['phase'] != 'measure': continue
            raw = held_evidence.read_bounded(monitor.directory / component['path'])
            kind = component['kind']
            actual, observation, errors = (iperf_observation(raw, kind, {}) if kind in ('tcp', 'udp')
                else extreme_observation(raw, window['run_id'], kind))
            if errors: raise Blocked('mixed component timing source invalid: ' + ','.join(errors))
            observations.append(dict(actual=actual, observation=observation))
        report['mixed_measurement_window'] = mixed_timing(observations, window['measure_seconds'])


def duration_budget(offered, seconds):
    buildup = math.ceil(offered['sessions'] / offered['open_rate']) + 10 if offered['kind'] == 'held' else 0
    if buildup > 610 or seconds + buildup + 20 > 4500:
        raise Blocked('held buildup/watchdog exceeds prepared bounds')
    return seconds + buildup + 20


def stop_jobs(backend, jobs, gaps):
    for job in jobs:
        process = job['process']
        if process is not None:
            try:
                backend.endpoint('traffic-client', ['python3', '/opt/traffic/job.py', 'stop', job['job_id']])
            except (legacy.Abort, OSError, subprocess.SubprocessError) as error:
                gaps.append('job stop unconfirmed: ' + job['job_id'] + ': ' + type(error).__name__)
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            except (OSError, subprocess.SubprocessError) as error:
                gaps.append('local exec process stop unconfirmed: ' + job['job_id'] + ': ' + type(error).__name__)


def execute_components(monitor, window, campaign, phase, seconds, report, popen=subprocess.Popen,
                       monotonic=time.monotonic, sleep=time.sleep):
    """Capture every component from launch; one failure stops all our sibling jobs."""
    ident = window['run_id'] + ('-stabilization' if phase == 'stabilization' else '')
    argv_set = plan.commands(window['offered'], campaign['target'], seconds, ident, campaign.get('source_ips', []), phase=phase)
    parts = window['offered'].get('components', [window['offered']])
    actual_seconds = plan.component_seconds(window['offered'], seconds, phase)
    budget = duration_budget(window['offered'], actual_seconds)
    if window['offered']['kind'] == 'mixed' and phase == 'measure':
        if window.get('component_measure_seconds', actual_seconds) != actual_seconds:
            raise Blocked('mixed declared component duration differs')
        report.update(measure_seconds=seconds, component_measure_seconds=actual_seconds)
    jobs = []
    handles = []
    monitor.context = dict(run_id=window['run_id'], phase=phase, offered=window['offered'], profile=window['profile'])
    monitor.monitor()
    if phase == 'measure':
        report['controller_measurement_bounds'] = dict(started_epoch_ns=time.time_ns(),
            started_monotonic_ns=time.monotonic_ns(), scope='controller_envelope_not_workload_measurement')
    deadline = monotonic() + budget + 5
    starts = []
    with ExitStack() as stack:
        try:
            for index, argv in enumerate(argv_set):
                prefix = f'{phase}-{index:02d}-{parts[index]["kind"]}'
                outpath, errpath = monitor.directory / (prefix + '.jsonl'), monitor.directory / (prefix + '.stderr')
                output = stack.enter_context(outpath.open('w', encoding='utf-8'))
                error = stack.enter_context(errpath.open('w', encoding='utf-8'))
                handles.extend((output, error))
                jid = uuid.uuid4().hex
                command = monitor.backend.compose + ['exec', '-T', 'traffic-client', 'python3',
                    '/opt/traffic/job.py', 'run', '--timeout', str(budget), jid, '--'] + argv
                row = dict(kind=parts[index]['kind'], phase=phase, path=outpath.name, stderr=errpath.name,
                           job_id=jid, argv=argv, process_start=utc(), process_end=None, exit_code=None)
                if window['offered']['kind'] == 'mixed': row['requested_duration_seconds'] = actual_seconds
                report['components'].append(row)
                job = dict(job_id=jid, process=None, row=row)
                jobs.append(job)
                job['process'] = popen(command, stdout=output, stderr=error)
                starts.append(monotonic())
                legacy.save(monitor.directory / 'receipt.json', report)
            if starts and max(starts) - min(starts) > 1:
                raise Blocked('mixed launch skew exceeds1s; window invalid')
            next_sample = monotonic()
            while True:
                active = False
                for job in jobs:
                    code = job['process'].poll()
                    if code is None:
                        active = True
                    elif job['row']['exit_code'] is None:
                        job['row'].update(exit_code=code, process_end=utc())
                        if code:
                            raise Blocked('workload component failed with exit ' + str(code))
                if not active:
                    break
                if monitor.cancelled:
                    raise legacy.Abort('operator interrupted run')
                if monotonic() >= deadline:
                    raise legacy.Abort('bounded workload deadline exceeded')
                if monotonic() >= next_sample:
                    monitor.monitor()
                    next_sample = monotonic() + monitor.config['sample_interval']
                sleep(.2)
        finally:
            stop_jobs(monitor.backend, jobs, report['gaps'])
            if phase == 'measure':
                report['controller_measurement_bounds'].update(completed_epoch_ns=time.time_ns(),
                    completed_monotonic_ns=time.monotonic_ns())
            for job in jobs:
                if job['process'] is not None:
                    job['row'].update(exit_code=job['process'].poll(), process_end=job['row']['process_end'] or utc())
            for handle in handles:
                handle.flush()
                os.fsync(handle.fileno())
            legacy.save(monitor.directory / 'receipt.json', report)


def normalized(sample, baseline, audit_loss_baseline=None):
    if sample.get('unavailable') or not all(sample.get(k) for k in ('dataplane_ok', 'management_ok', 'mngt_ok', 'vms_ok')):
        return False
    audit, old_audit = sample.get('audit', {}), baseline.get('audit', {})
    for key in ('memory_used_pct', 'audit_backlog'):
        if type(audit.get(key)) not in (float, int) or type(old_audit.get(key)) not in (float, int):
            return False
    if audit['memory_used_pct'] > old_audit['memory_used_pct'] + 5 or audit['audit_backlog'] > old_audit['audit_backlog'] + 10:
        return False
    current_host, old_host = sample.get('host', {}), baseline.get('host', {})
    if current_host.get('memory_used_pct', 101) > old_host.get('memory_used_pct', -10) + 5:
        return False
    latency, old_latency = sample.get('service_probe_ms'), baseline.get('service_probe_ms')
    return (type(latency) in (int, float) and type(old_latency) in (int, float)
            and latency <= max(20, old_latency * 2)
            and audit.get('audit_lost') == (audit_loss_baseline['lost'] if audit_loss_baseline else 0)
            and (audit_loss_baseline is None or audit.get('boot_id') == audit_loss_baseline['guest_boot_id']))


def recover(monitor, baseline, seconds, timeout, monotonic=time.monotonic):
    if seconds < 120 or timeout < seconds:
        raise Blocked('recovery requires >=120s and a bounded normalization deadline')
    monitor.context = dict(monitor.context, phase='recovery', offered={})
    monitor.cancelled = False  # Load is already stopped; cancellation never restarts it.
    start = monotonic()
    monitor.pause(seconds)
    consecutive = 0
    consumed = len(monitor.window_samples)
    while monotonic() - start <= timeout:
        monitor.monitor()
        for sample in monitor.window_samples[consumed:]:
            consecutive = consecutive + 1 if normalized(sample, baseline,
                getattr(monitor, 'audit_loss_baseline', None)) else 0
        consumed = len(monitor.window_samples)
        if consecutive >= 2:
            return {'status': 'RECOVERED', 'seconds': monotonic() - start,
                    'normalization': 'two consecutive healthy baseline-bound samples',
                    'normalization_sample_indices': [consumed - 2, consumed - 1]}
        monitor.pause(monitor.config['sample_interval'])
    raise legacy.Abort('recovery did not normalize before deadline')


def execute_window(campaign, window, config, backend, measure_config, identity_argv,
                   identity_base, native_health, isolation_path, directory, recovery_timeout=600, campaign_base=None,
                   audit_loss_baseline_file=None):
    report = dict(schema_version=1, run_id=window['run_id'], profile=window['profile'],
                  workload=window.get('workload', 'endurance'), regime='unclassified', offered_load=window['offered'],
                  process_start=utc(), process_end=None, outcome='INVALID_MEASUREMENT', valid=False,
                  window_validated=False, components=[], gaps=[], results=[], evidence=[],
                  normalization_pending='full-window native completeness and workload analysis are required')
    legacy.save(directory / 'window.json', window)
    legacy.save(directory / 'receipt.json', report)
    monitor = None
    baseline = None
    before = None
    audit_epoch = None
    observation_ready = False
    try:
        measure_config = measurement.validate(dict(measure_config, qualification_mode=True))
        observation_ready = True
        if audit_loss_baseline_file is None:
            raise Blocked('explicit immutable audit loss epoch receipt required')
        snapshots = validate_campaign_artifacts(campaign, window, campaign_base or identity_base,
                                               backend, config, time.time())
        legacy.save(directory / 'snapshots-readback.json', snapshots)
        topology = backend.topology()
        legacy.save(directory / 'topology-before.json', topology)
        isolation = read_json(isolation_path)
        legacy.validate_isolation(isolation, topology, time.time(), 86400, campaign['campaign_id'])
        if (isolation.get('ngfw_state') != 'shut off' or isolation.get('client_to_receiver') != 'unreachable'
                or isolation.get('receiver_local_health') is not True):
            raise Blocked('negative powered-off isolation proof is incomplete')
        legacy.save(directory / 'isolation.json', isolation)
        before = backend.extended_guest(inventory=True)
        legacy.save(directory / 'inventory-before.json', before)
        epoch_proof, epoch_bytes, source_bytes = validate_audit_baseline(audit_loss_baseline_file, before, time.time(), window['profile'])
        audit_epoch = epoch_proof['baseline']
        report['audit_loss_epoch'] = epoch_proof
        (directory / 'audit-loss-baseline.json').write_bytes(epoch_bytes)
        (directory / 'audit-loss-source.json').write_bytes(source_bytes)
        risks = measurement.inventory_risks(before, measure_config, audit_epoch)
        if risks:
            raise Blocked('; '.join(risks))
        identity = json.loads(backend.call(identity_argv, timeout=30))
        validate_identity(identity, campaign, window, before, topology, identity_base, time.time())
        validate_audit_epoch_identity(audit_epoch, identity)
        legacy.save(directory / 'identity-before.json', identity)
        for name, reference in identity['artifacts'].items():
            if name in (*plan.HASH_FIELDS, 'profile'):
                (directory / ('identity-artifact-' + name + '.bin')).write_bytes(artifact(reference, identity_base))
        report.update(versions=identity['versions'], config_hashes=identity['config_hashes'],
                      profile_evidence=identity['profile_evidence'])
        report['native_preflight'] = validate_native(native_health, before['boot_id'], time.time())
        monitor = NativeRunner(config, backend, directory, report,
            EpochAuditGuard(config['limits'], audit_epoch), {}, measurement.Guard(measure_config, initial_guest=before))
        monitor.native_health, monitor.boot_id = native_health, before['boot_id']
        monitor.audit_loss_baseline = audit_epoch
        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: setattr(monitor, 'cancelled', True))
        try:
            monitor.monitor()
            baseline = monitor.window_samples[-1]
            warmup = window['stabilization_seconds']
            if warmup:
                execute_components(monitor, window, campaign, 'stabilization', warmup, report)
            held = window['offered']['kind'] == 'held'
            execute_measurement(monitor, window, campaign, report)
            if held:
                component = next(c for c in report['components'] if c['phase'] == 'measure')
                raw_path = directory / component['path']
                rows = [json.loads(line) for line in held_evidence.read_bounded(directory / 'held-receiver.ndjson').splitlines()]
                finals = [r for r in map(json.loads, held_evidence.read_bounded(raw_path).splitlines()) if r.get('record_type') == 'final']
                if len(finals) != 1: raise Blocked('held final measurement missing')
                proof = held_evidence.validate(rows, finals[0], window['offered'], window['run_id'],
                    config['sample_interval'] + measure_config['max_sample_seconds'] + 5.2)
                proof.update(receiver_evidence=held_evidence.reference(directory / 'held-receiver.ndjson'),
                             workload_evidence=held_evidence.reference(raw_path))
                legacy.save(directory / 'held-receiver-window.json', proof)
                report.update(held_receiver_window_validated=True,
                              held_receiver_evidence=held_evidence.reference(directory / 'held-receiver-window.json'))
            report['outcome'] = 'COMPLETED'
        finally:
            if baseline is not None:
                try:
                    if any('stop unconfirmed' in gap for gap in report['gaps']):
                        raise legacy.Abort('load stop unconfirmed; recovery cannot be declared safe')
                    report['recovery'] = recover(monitor, baseline, window['recovery_seconds'], recovery_timeout)
                except (legacy.Abort, OSError, ValueError, subprocess.SubprocessError) as error:
                    report['outcome'] = 'SAFETY_STOP'
                    report['recovery'] = {'status': 'SAFETY_STOP', 'reason': str(error)}
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
    except (legacy.Abort, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        report['outcome'] = ('SAFETY_STOP' if isinstance(error, legacy.Abort) and not isinstance(error, Blocked)
                             and getattr(error, 'outcome', None) != 'INVALID' else 'INVALID_MEASUREMENT')
        if report.get('recovery', {}).get('status') == 'SAFETY_STOP':
            report['outcome'] = 'SAFETY_STOP'
        report['reason'] = str(error)
    finally:
        try:
            if not observation_ready or before is None:
                raise Blocked('selected observation configuration unavailable; guest discovery not attempted')
            after = backend.extended_guest(inventory=True)
            legacy.save(directory / 'inventory-after.json', after)
            if before:
                changed = measurement.control_changes(before, after)
                if audit_epoch is not None:
                    if hashlib.sha256(Path(audit_loss_baseline_file).read_bytes()).hexdigest() != epoch_proof['baseline_sha256']:
                        raise Blocked('immutable audit loss baseline changed during window')
                    artifact(audit_epoch['source_evidence'], Path(audit_loss_baseline_file).parent)
                    loss_reasons = EpochAuditGuard({}, audit_epoch).audit_loss_reasons(measurement.legacy_guest(after))
                    if 'AuditD new loss since verified qualification epoch' in loss_reasons:
                        report['outcome'] = 'SAFETY_STOP'
                    changed.extend(loss_reasons)
                    changed.extend(measurement.inventory_risks(after, measure_config, audit_epoch))
                    validate_audit_epoch_inventory(audit_epoch, after)
                if after.get('inventory') != before.get('inventory') or after.get('boot_id') != before.get('boot_id'):
                    changed.append('guest inventory/boot changed')
                topology_after = backend.topology()
                legacy.save(directory / 'topology-after.json', topology_after)
                if topology_after != topology:
                    changed.append('topology changed during window')
                identity_after = json.loads(backend.call(identity_argv, timeout=30))
                validate_identity(identity_after, campaign, window, after, topology_after, identity_base, time.time())
                if audit_epoch is not None:
                    validate_audit_epoch_identity(audit_epoch, identity_after)
                legacy.save(directory / 'identity-after.json', identity_after)
                if report.get('native_preflight'):
                    report['native_postflight'] = validate_native(native_health, before['boot_id'], time.time())
                    report['window_clock_alignment'] = window_clock_summary(report['native_preflight'], report['native_postflight'],
                                                                           report.get('controller_measurement_bounds'))
                if changed:
                    report['gaps'] += changed
                    if report['outcome'] != 'SAFETY_STOP':
                        report['outcome'] = 'INVALID_MEASUREMENT'
        except (legacy.Abort, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            report['gaps'].append('after inventory unavailable: ' + (str(error) if isinstance(error, Blocked) else type(error).__name__))
            if hasattr(error, 'clock_summary'):
                report['window_clock_alignment'] = error.clock_summary
            if report['outcome'] != 'SAFETY_STOP':
                report['outcome'] = 'INVALID_MEASUREMENT'
        report['process_end'] = utc()
        samples = monitor.window_samples if monitor is not None else []
        measured = [s for s in samples if s.get('phase') == 'measure']
        expected = math.ceil(window['measure_seconds'] / config['sample_interval'])
        observed = sum(not s.get('unavailable') for s in measured)
        report['completeness'] = dict(scope='controller_monitor_not_native', expected=expected,
                                     observed=observed, missing=max(0, expected - observed))
        report['evidence'] = [p.name for p in directory.iterdir() if p.is_file() and p.name != 'receipt.json']
        legacy.save(directory / 'receipt.json', report)
        legacy.save(directory / 'receipts-index.json', {'receipts': ['receipt.json']})
        legacy.save(directory / 'manifest.json', measurement.manifest(directory))
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('campaign', 'config', 'measurement-config', 'measurement-argv-file',
                 'identity-argv-file', 'native-health', 'isolation', 'audit-loss-baseline-file'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--run-id', required=True)
    parser.add_argument('--recovery-timeout', type=int, default=600)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('live execution requires Linux KVM host; portable unit tests do not execute this main')
    campaign = read_json(args.campaign)
    window = selected_window(campaign, args.run_id)
    config = legacy.validate(read_json(args.config))
    if config['target'] != campaign['target']:
        raise Blocked('runner/campaign target differs')
    measure = measurement.validate(dict(read_json(args.measurement_config), qualification_mode=True))
    if measure.get('cpu_action') != 'warn':
        raise Blocked('cpu_action=warn is mandatory; CPU100 alone must not stop extreme trials')
    probe, identity = read_json(args.measurement_argv_file), read_json(args.identity_argv_file)
    for argv in (probe, identity):
        if not isinstance(argv, list) or not argv or not all(isinstance(v, str) and v for v in argv):
            raise Blocked('measurement/identity command must be a nonempty argv array')
    if probe[-2:] != ['python3', '-']:
        raise Blocked('measurement argv must end with python3 -')
    if not window['recovery_seconds'] <= args.recovery_timeout <= 3600:
        raise Blocked('recovery timeout outside recovery-minimum..3600s')
    import fcntl
    os.umask(0o077)
    root = Path(config['reports_dir'])
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Blocked('another existing or extreme runner holds the host lock') from error
        directory = root / window['run_id']
        directory.mkdir(mode=0o700)  # Never overwrite a failed window or reuse its ID.
        backend = legacy.Backend(config, measurement_argv=probe, measurement_config=measure)
        try:
            result = execute_window(campaign, window, config, backend, measure, identity,
                args.identity_argv_file.parent, args.native_health, args.isolation, directory, args.recovery_timeout,
                args.campaign.parent, args.audit_loss_baseline_file)
        finally:
            backend.pool.shutdown(wait=True)
        print(json.dumps({'directory': str(directory), 'outcome': result['outcome'],
                          'valid': result['valid'], 'reason': result.get('reason')}))
    return 0 if result['outcome'] == 'COMPLETED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
