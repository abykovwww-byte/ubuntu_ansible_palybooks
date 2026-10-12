"""Join archived workload output with observed controller evidence, never plan claims.

CLI: python -m native_analysis.normalize --plan plan.json --receipts index.json
     --output normalized.json
The index is {"receipts": ["relative/path/to/receipt.json", ...]}.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path

from .analyze import histogram_quantiles, number, timestamp, validate_run


def utc(value):
    if not number(value):
        raise ValueError('finite epoch seconds required')
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def artifact(path):
    """Hash the actual preserved source, not a value copied from the plan."""
    path = Path(path).resolve()
    raw = path.read_bytes()
    return raw, {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def extreme_observation(raw, expected_run_id, kind):
    errors, records = [], []
    for index, line in enumerate(raw.decode('utf-8').splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError()
            records.append(record)
        except (ValueError, TypeError):
            errors.append('malformed_jsonl_line:' + str(index))
    final = [row for row in records if row.get('record_type') == 'final']
    observation = {'record_count': len(records), 'progress_record_count': sum(r.get('record_type') == 'progress' for r in records)}
    if len(final) != 1:
        errors.append('requires_one_final_record')
    if not final:
        return {}, observation, errors
    row = final[-1]
    if any(r.get('run_id') != expected_run_id or r.get('kind') != kind for r in records):
        errors.append('workload_identity_mismatch')
    observation['final'] = row
    result = {}
    try:
        start, end, seconds = row['measure_start'], row['time_end'], row['seconds']
        if not number(start) or not number(end) or not number(seconds) or seconds <= 0 or not math.isclose(end-start, seconds, rel_tol=1e-6, abs_tol=.001):
            raise ValueError()
        result.update(start=utc(start), end=utc(end))
        counters = ('successful_requests', 'attempted_requests', 'errors', 'censored_at_boundary', 'pending_requests')
        if any(type(row.get(key)) is not int or row[key] < 0 for key in counters):
            raise ValueError()
        if row['attempted_requests'] != sum(row[key] for key in ('successful_requests', 'errors', 'censored_at_boundary', 'pending_requests')):
            errors.append('request_counter_conservation_failed')
        success_rate = row['successful_requests'] / seconds
        if not number(row.get('success_rate')) or not math.isclose(success_rate, row['success_rate'], rel_tol=1e-6, abs_tol=1e-9):
            errors.append('reported_success_rate_mismatch')
        result['achieved'] = {'value': success_rate, 'unit': {'short-tcp': 'cps', 'http': 'rps', 'dns': 'qps', 'held': 'successful_echoes_per_second'}[kind]}
        result['errors'] = {'count': row['errors'], 'classes': row.get('error_classes'),
                            'censored_at_boundary': row['censored_at_boundary'], 'pending_requests': row['pending_requests']}
        if row['pending_requests']:
            errors.append('unresolved_requests_at_end')
        hist = row['latency_histogram']
        if hist.get('cumulative') is not False or hist.get('population') != 'successful_requests' or hist.get('overflow') != 'unbounded':
            raise ValueError()
        normalized_hist = {'upper_bounds_ms': hist['bounds_ms'] + ['+Inf'], 'counts': hist['counts']}
        histogram_quantiles(normalized_hist)
        if sum(hist['counts']) != row['successful_requests']:
            errors.append('histogram_population_mismatch')
        result['latency_histogram'] = normalized_hist
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        errors.append('invalid_actual_workload_measurement')
    if row.get('stop_reason'):
        errors.append('workload_stopped_early')
    if kind == 'held':
        required = row.get('requested_sessions')
        counts = ('active_sessions_at_measure_end', 'minimum_active_measurement_sessions', 'confirmed_measurement_sessions')
        if type(required) is not int or required <= 0 or any(row.get(k) != required for k in counts) or row.get('stale_echo_sessions') != 0:
            errors.append('held_sustained_population_not_established')
        # A single union of echo IDs is not independent sustained-session proof.
        observation['requires_held_receiver_readback'] = True
    return result, observation, errors


def iperf_observation(raw, kind, receipt):
    result, errors, observation = {}, [], {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('iperf object required')
    if value.get('error'):
        errors.append('iperf_error')
    end = value.get('end', {})
    receiver = end.get('sum_received')
    scope = 'client.end.sum_received'
    if not receiver:
        server = value.get('server_output_json', {})
        if isinstance(server, str):
            try:
                server = json.loads(server)
            except ValueError:
                server = {}
        server_end = server.get('end', {})
        receiver = server_end.get('sum_received')
        scope = 'server.end.sum_received'
        if not receiver and server_end.get('sum', {}).get('sender') is False:
            receiver, scope = server_end['sum'], 'server.end.sum'
        if not receiver and end.get('sum', {}).get('sender') is False:
            receiver, scope = end['sum'], 'client.end.sum_receiver'
    # iperf 3.12 TCP sum_received uses received bytes/time, while its sender
    # flag describes the local stream role (iperf_api.c:3688-3698). Only this
    # explicitly named TCP summary has that meaning; generic sum and UDP keep
    # their receiver-role guards.
    named_tcp_receiver = kind == 'tcp' and scope in ('client.end.sum_received', 'server.end.sum_received')
    if not isinstance(receiver, dict) or not receiver or (receiver.get('sender') is True and not named_tcp_receiver):
        return result, observation, errors + ['receiver_summary_unavailable']
    observation.update(receiver_summary=receiver, receiver_scope=scope,
                       sender_summary=end.get('sum_sent'), iperf_protocol=value.get('start', {}).get('test_start', {}).get('protocol'),
                       iperf_version=value.get('start', {}).get('version'),
                       iperf_reverse=value.get('start', {}).get('test_start', {}).get('reverse'))
    if str(observation['iperf_protocol']).lower() != kind:
        errors.append('iperf_protocol_mismatch')
    try:
        seconds, byte_count = receiver['seconds'], receiver['bytes']
        if not number(seconds) or seconds <= 0 or type(byte_count) is not int or byte_count < 0:
            raise ValueError()
        result['achieved'] = {'value': byte_count * 8 / seconds, 'unit': 'bits_per_second'}
        if number(receiver.get('bits_per_second')) and not math.isclose(result['achieved']['value'], receiver['bits_per_second'], rel_tol=1e-4, abs_tol=1):
            errors.append('receiver_rate_mismatch')
        window = receipt.get('measurement_window')
        if window:
            start, finish = timestamp(window['start']), timestamp(window['end'])
            if not window.get('time_source') or not math.isclose((finish-start).total_seconds(), seconds, rel_tol=1e-5, abs_tol=.01):
                raise ValueError()
            result.update(start=start.isoformat(), end=finish.isoformat())
            observation['time_source'] = window['time_source']
        else:
            epoch = value['start']['timestamp']['timesecs']
            offset = receiver['start']
            final_offset = receiver['end']
            if not number(epoch) or not number(offset) or not number(final_offset) or not math.isclose(final_offset-offset, seconds, rel_tol=1e-5, abs_tol=.01):
                raise ValueError()
            result.update(start=utc(epoch+offset), end=utc(epoch+final_offset))
            observation.update(time_source='iperf_start_timestamp_plus_receiver_offsets', wall_timestamp_resolution_seconds=1)
        if kind == 'udp':
            result['loss'] = {key: receiver.get(key) for key in ('lost_packets', 'packets', 'lost_percent')}
            observation['jitter_ms'] = receiver.get('jitter_ms')
            observation['pps'] = None
            observation['pps_limitation'] = 'No packet-rate semantics inferred from version-dependent iperf counters'
    except (KeyError, ValueError, TypeError, OverflowError, OSError):
        errors.append('invalid_actual_iperf_measurement')
    return result, observation, errors


def normalize_window(window, workload_path, receipt, format=None, kind=None):
    """Return a run even on a missing/failed raw window; never substitute plan facts.

The controller's native_completeness is an actual full-window native point
receipt. Generic monitor cadence completeness is carried separately and cannot
make a window eligible. Validity requires explicit observed window validation.
"""
    ident = window.get('run_id')
    result = {key: window[key] for key in ('run_id', 'block_id', 'repeat', 'position') if key in window}
    errors = []
    for key in ('profile', 'workload', 'regime', 'offered_load', 'versions', 'config_hashes', 'profile_evidence'):
        if key in receipt and receipt[key] is not None:
            result[key] = receipt[key]
    result['outcome'] = receipt.get('outcome', 'INVALID_MEASUREMENT')
    result['controller_observation'] = {key: receipt.get(key) for key in
                                       ('outcome', 'reason', 'gaps', 'process_start', 'process_end', 'recovery')}
    result['valid'] = receipt.get('valid') is True and receipt.get('window_validated') is True
    result['evidence'] = list(receipt.get('evidence') or [])
    result['controller_completeness'] = receipt.get('completeness')
    native = receipt.get('native_completeness')
    if native is None and isinstance(receipt.get('completeness'), dict) and receipt['completeness'].get('scope') == 'native_telemetry':
        native = receipt['completeness']
    if isinstance(native, dict) and native.get('evidence'):
        result['completeness'] = native
    else:
        errors.append('native_full_window_completeness_unavailable')
    if receipt.get('run_id') != ident:
        errors.append('controller_run_identity_mismatch')
    for key in ('profile', 'workload'):
        if key in window and receipt.get(key) != window[key]:
            errors.append('observed_identity_differs_from_plan:' + key)
    if receipt.get('offered_load') != window.get('offered'):
        errors.append('observed_offered_differs_from_plan')
    if not result['valid']:
        errors.append('controller_window_not_validated')
    kind = kind or window.get('offered', {}).get('kind')
    format = format or ('iperf-json' if kind in ('tcp', 'udp') else 'extreme-jsonl')
    try:
        raw, source = artifact(workload_path)
        result['evidence'].append(source['path'])
        result['raw_source'] = source
        if format == 'extreme-jsonl':
            measured, observation, issues = extreme_observation(raw, ident, kind)
        elif format == 'iperf-json':
            measured, observation, issues = iperf_observation(raw, kind, receipt)
        else:
            raise ValueError('unsupported workload format')
        result.update(measured)
        result['workload_observation'] = observation
        errors.extend(issues)
        if kind == 'held':
            try:
                from extreme import held_evidence
                if receipt.get('held_receiver_window_validated') is not True:
                    raise ValueError('held receiver proof absent')
                observation['held_receiver'] = held_evidence.verify(receipt.get('held_receiver_evidence'),
                    Path(workload_path).parent, raw, window['offered'], ident)
            except (OSError, ValueError, KeyError, TypeError):
                errors.append('independent_held_receiver_window_unverified')
    except (OSError, ValueError, TypeError, UnicodeError, AttributeError):
        errors.append('workload_source_missing_or_malformed')
        result['raw_source'] = {'path': str(workload_path), 'status': 'UNAVAILABLE_OR_MALFORMED'}
    errors.extend(validate_run(result))
    result['normalization_errors'] = sorted(set(errors))
    if errors:
        result['valid'] = False
        if result['outcome'] != 'SAFETY_STOP':
            result['outcome'] = 'INVALID_MEASUREMENT'
    return result


def normalize_campaign(plan, receipts, base_dir):
    """receipts entries are receipt file paths relative to the index directory."""
    base = Path(base_dir)
    loaded, unreadable = {}, []
    for item in receipts:
        path = base / item
        try:
            receipt = read_json(path)
            if not isinstance(receipt, dict):
                raise ValueError('controller receipt object required')
            ident = receipt.get('run_id')
            if ident in loaded:
                loaded[ident][0]['duplicate_controller_receipts'] = True
            else:
                loaded[ident] = (receipt, path.resolve())
        except (OSError, ValueError, TypeError):
            unreadable.append(str(path))
    runs = []
    windows = plan.get('windows', []) + plan.get('endurance', [])
    planned_ids = [w.get('run_id') for w in windows]
    if len(set(planned_ids)) != len(planned_ids):
        raise ValueError('Duplicate planned run identity')
    for window in windows:
        receipt, receipt_path = loaded.pop(window.get('run_id'), ({}, base / 'missing-receipt.json'))
        all_components = receipt.get('components') or []
        components = [c for c in all_components if isinstance(c, dict) and c.get('phase') == 'measure'] or [{}]
        offered = window.get('offered', {})
        expected = offered.get('components', []) if offered.get('kind') == 'mixed' else [offered]
        first_run = len(runs)
        # Preserve a missing component as an invalid run too.
        for index in range(max(len(components), len(expected))):
            component = components[index] if index < len(components) else {}
            component_kind = component.get('kind') or (expected[index].get('kind') if index < len(expected) else None)
            raw_path = receipt_path.parent / component.get('path', 'missing-workload-output')
            joined = dict(receipt)
            joined['evidence'] = list(receipt.get('evidence') or []) + [str(receipt_path)]
            if component.get('measurement_window'):
                joined['measurement_window'] = component['measurement_window']
            run = normalize_window(window, raw_path, joined, component.get('format'), component_kind)
            if offered.get('kind') == 'mixed':
                suffix = '::component' + str(index) + ':' + str(component_kind)
                run['run_id'] = str(run.get('run_id')) + suffix
                run['block_id'] = str(run.get('block_id', window.get('run_id'))) + suffix
                if 'workload' in run:
                    run['workload'] += suffix
                run['mixed_parent_run_id'] = window.get('run_id')
            extra_errors = []
            if receipt.get('duplicate_controller_receipts'):
                extra_errors.append('duplicate_controller_receipts')
            if index >= len(expected) or component_kind != expected[index].get('kind'):
                extra_errors.append('component_identity_mismatch')
            if component.get('exit_code') != 0:
                extra_errors.append('component_exit_not_successful')
            if extra_errors:
                run['valid'] = False
                run['normalization_errors'].extend(extra_errors)
                if run['outcome'] != 'SAFETY_STOP':
                    run['outcome'] = 'INVALID_MEASUREMENT'
            run['controller_component'] = component
            run['nonmeasurement_components'] = [c for c in all_components if isinstance(c, dict) and c.get('phase') != 'measure']
            runs.append(run)
        if offered.get('kind') == 'mixed':
            mixed_runs = runs[first_run:]
            try:
                from extreme import plan as extreme_plan
                from extreme.control_capacity import mixed_timing
                nominal = window['measure_seconds']
                seconds = extreme_plan.component_seconds(offered, nominal)
                if (window.get('component_measure_seconds') != seconds
                        or receipt.get('measure_seconds') != nominal
                        or receipt.get('component_measure_seconds') != seconds
                        or any(c.get('requested_duration_seconds') != seconds for c in components)):
                    raise ValueError('mixed declared timing contract missing or differs')
                timing = mixed_timing([{'actual': r, 'observation': r['workload_observation']} for r in mixed_runs], nominal)
                for run in mixed_runs: run['mixed_measurement_window'] = timing
            except (RuntimeError, ValueError, KeyError, TypeError):
                for run in mixed_runs:
                    run['valid'] = False
                    run['normalization_errors'].append('mixed_full_concurrent_window_unverified')
                    if run['outcome'] != 'SAFETY_STOP': run['outcome'] = 'INVALID_MEASUREMENT'
    for ident, (receipt, path) in loaded.items():
        runs.append({'run_id': ident, 'valid': False, 'outcome': 'INVALID_MEASUREMENT',
                     'evidence': [str(path)], 'normalization_errors': ['unplanned_controller_receipt']})
    return {'schema_version': 1, 'synthetic': plan.get('synthetic', False), 'runs': runs,
            'controller_receipt_files': [str((base / p).resolve()) for p in receipts],
            'unreadable_controller_receipts': unreadable,
            'limitations': ['Plans supply scheduling identity only; actual facts require observed receipts.',
                            'Controller cadence completeness is not native point completeness.',
                            'Native metric formulas and EDR central receipt require their own evidence.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--receipts', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    plan = read_json(args.plan)
    index = read_json(args.receipts)
    result = normalize_campaign(plan, index['receipts'], args.receipts.resolve().parent)
    result['input_artifacts'] = {'plan': artifact(args.plan)[1], 'receipts_index': artifact(args.receipts)[1]}
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')


if __name__ == '__main__':
    main()
