"""Conservative paired-profile analysis of explicitly normalized run evidence."""
from collections import defaultdict
from datetime import datetime
import math
import re
import statistics

OUTCOMES = {'NGFW_LIMIT', 'GENERATOR_LIMITED', 'HOST_LIMITED', 'INVALID_MEASUREMENT', 'SAFETY_STOP',
            'COMPLETED', 'STABLE_LIMIT_NOT_ESTABLISHED'}
PARITY = ('offered_load', 'versions', 'config_hashes', 'workload', 'regime')
INVARIANTS = ('build', 'policy', 'security_features', 'vm_resources', 'topology', 'generator',
              'receiver', 'observation', 'log_forwarding')


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def quantiles(samples):
    """Nearest-rank empirical quantiles; do not average precomputed quantiles."""
    if not samples:
        return None
    if any(not number(x) or x < 0 for x in samples):
        raise ValueError('Latency samples must be finite nonnegative milliseconds')
    values = sorted(samples)
    return {f'p{int(p * 100)}': values[max(0, math.ceil(p * len(values)) - 1)] for p in (.5, .95, .99)}


def histogram_quantiles(histogram):
    bounds = histogram['upper_bounds_ms']
    counts = histogram['counts']
    numeric = [float('inf') if b == '+Inf' else b for b in bounds]
    if not counts or len(counts) != len(bounds) or any(type(c) is not int or c < 0 for c in counts):
        raise ValueError('Histogram needs equally sized bounds and nonnegative integer bucket counts')
    if any(not isinstance(b, (int, float)) or math.isnan(b) or b < 0 for b in numeric) or any(a >= b for a, b in zip(numeric, numeric[1:])):
        raise ValueError('Histogram bounds must increase strictly')
    total = sum(counts)
    if not total:
        return None
    result = {}
    for p in (.5, .95, .99):
        rank, cumulative, lower = math.ceil(p * total), 0, 0
        for upper, count in zip(numeric, counts):
            cumulative += count
            if cumulative >= rank:
                result[f'p{int(p*100)}'] = {'lower_ms': lower, 'upper_ms': None if math.isinf(upper) else upper,
                                         'upper_unbounded': math.isinf(upper)}
                break
            lower = upper
    return result


def timestamp(value):
    result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.tzinfo is None:
        raise ValueError('UTC offset required')
    return result


def validate_run(run):
    reasons = []
    required = ('run_id', 'block_id', 'repeat', 'position', 'profile', 'workload', 'regime', 'start', 'end',
                'offered_load', 'versions', 'config_hashes', 'outcome', 'valid', 'evidence', 'achieved',
                'completeness', 'profile_evidence')
    for key in required:
        if key not in run:
            reasons.append('missing:' + key)
    if reasons:
        return reasons
    if run['profile'] not in ('A', 'B'):
        reasons.append('unknown_profile')
    if run['regime'] not in ('stable', 'overload', 'endurance'):
        reasons.append('unknown_regime')
    if run['outcome'] not in OUTCOMES:
        reasons.append('unknown_outcome')
    if run['valid'] is not True or run['outcome'] in ('INVALID_MEASUREMENT', 'SAFETY_STOP'):
        reasons.append('invalid_or_safety_stopped')
    try:
        seconds = (timestamp(run['end']) - timestamp(run['start'])).total_seconds()
        if seconds < (3600 if run['regime'] == 'endurance' else 300):
            reasons.append('measure_window_too_short')
    except (ValueError, TypeError):
        reasons.append('invalid_measure_window')
    for key in ('offered_load', 'versions', 'config_hashes'):
        if not isinstance(run[key], dict) or not run[key]:
            reasons.append('empty:' + key)
    if isinstance(run['config_hashes'], dict):
        for key in INVARIANTS:
            if not re.fullmatch(r'[a-fA-F0-9]{64}', str(run['config_hashes'].get(key, ''))):
                reasons.append('missing_or_invalid_invariant_hash:' + key)
    if not isinstance(run['evidence'], list) or not run['evidence']:
        reasons.append('missing_evidence_references')
    achieved = run['achieved']
    if not isinstance(achieved, dict) or not number(achieved.get('value')) or achieved['value'] < 0 or not achieved.get('unit'):
        reasons.append('invalid_achieved')
    complete = run['completeness']
    if (not isinstance(complete, dict) or any(type(complete.get(k)) is not int for k in ('expected', 'observed', 'missing'))
            or complete.get('expected', 0) <= 0 or complete.get('observed', -1) != complete.get('expected') or complete.get('missing') != 0):
        reasons.append('incomplete_or_unknown_observation')
    profile = run['profile_evidence']
    expected = {'edr_installed': False, 'audit_profile': 'stock'} if run['profile'] == 'A' else {'edr_installed': True, 'audit_profile': 'full', 'audit_rule_count': 116}
    if not isinstance(profile, dict) or any(profile.get(k) != v for k, v in expected.items()) or profile.get('edr_installed') is not expected['edr_installed']:
        reasons.append('profile_not_established')
    return reasons


def analyze(document):
    if document.get('schema_version') != 1:
        raise ValueError('schema_version must be 1')
    runs = document.get('runs', [])
    ids = [r.get('run_id') for r in runs]
    if len(set(ids)) != len(ids):
        raise ValueError('Duplicate run_id would double count evidence')
    blocks, summaries = defaultdict(list), []
    for run in runs:
        reasons = validate_run(run)
        summary = {'run_id': run.get('run_id'), 'eligible': not reasons, 'reasons': reasons,
                   'profile': run.get('profile'), 'regime': run.get('regime'), 'outcome': run.get('outcome'),
                   'achieved': run.get('achieved'), 'completeness': run.get('completeness'),
                   'evidence': run.get('evidence'), 'errors': run.get('errors'), 'loss': run.get('loss'),
                   'normalization_errors': run.get('normalization_errors', []),
                   'stop_reason': (run.get('workload_observation', {}).get('final', {}).get('stop_reason')
                                   or run.get('controller_observation', {}).get('reason')),
                   'controller_gaps': run.get('controller_observation', {}).get('gaps')}
        try:
            if 'latency_ms' in run:
                summary['latency'] = {'method': 'raw_nearest_rank', 'quantiles_ms': quantiles(run['latency_ms'])}
            elif 'latency_histogram' in run:
                summary['latency'] = {'method': 'histogram_rank_bounds', 'quantiles': histogram_quantiles(run['latency_histogram'])}
            else:
                summary['latency'] = {'method': 'UNAVAILABLE'}
        except (ValueError, KeyError, TypeError):
            summary['eligible'] = False
            summary['reasons'].append('invalid_latency_evidence')
        summaries.append(summary)
        blocks[run.get('block_id')].append((run, summary))
    comparisons, rejected = [], []
    for block_id, entries in blocks.items():
        reasons = []
        if len(entries) != 3 or sorted(r.get('position', -1) for r, _ in entries) != [0, 1, 2]:
            rejected.append({'block_id': block_id, 'reasons': ['requires_three_unique_positions']})
            continue
        entries.sort(key=lambda x: x[0]['position'])
        rows = [r for r, _ in entries]
        if any(not s['eligible'] for _, s in entries):
            reasons.append('ineligible_run')
        if [r.get('profile') for r in rows] not in (['A', 'B', 'A'], ['B', 'A', 'B']):
            reasons.append('requires_return_control')
        for key in PARITY + ('repeat',):
            if any(r.get(key) != rows[0].get(key) for r in rows[1:]):
                reasons.append('parity_mismatch:' + key)
        if not reasons:
            if any(timestamp(left['end']) > timestamp(right['start']) for left, right in zip(rows, rows[1:])):
                reasons.append('overlapping_or_reversed_windows')
            if len({r['achieved']['unit'] for r in rows}) != 1:
                reasons.append('incompatible_achieved_units')
        if reasons:
            rejected.append({'block_id': block_id, 'reasons': reasons})
            continue
        rates = {p: statistics.mean(r['achieved']['value'] for r in rows if r['profile'] == p) for p in ('A', 'B')}
        reference = statistics.mean([rows[0]['achieved']['value'], rows[2]['achieved']['value']])
        scatter = abs(rows[0]['achieved']['value'] - rows[2]['achieved']['value']) / reference * 100 if reference else None
        drop = (rates['A'] - rates['B']) / rates['A'] * 100 if rates['A'] else None
        capacity_limited = any(r['outcome'] in ('HOST_LIMITED', 'GENERATOR_LIMITED') for r in rows)
        comparisons.append({'block_id': block_id, 'workload': rows[0]['workload'], 'regime': rows[0]['regime'],
                            'offered_load': rows[0]['offered_load'], 'repeat': rows[0]['repeat'],
                            'versions': rows[0]['versions'], 'config_hashes': rows[0]['config_hashes'],
                            'order': ''.join(r['profile'] for r in rows), 'a': rates['A'], 'b': rates['B'],
                            'unit': rows[0]['achieved']['unit'], 'drop_pct': drop, 'return_control_scatter_pct': scatter,
                            'capacity_limited': capacity_limited,
                            'exceeds_research_threshold': not capacity_limited and drop is not None and scatter is not None and drop > max(5, scatter)})
    groups = defaultdict(list)
    import json
    for comparison in comparisons:
        key = json.dumps([comparison[k] for k in ('workload', 'regime', 'offered_load', 'unit', 'versions', 'config_hashes')], sort_keys=True)
        groups[key].append(comparison)
    findings = []
    for group in groups.values():
        drops = [x['drop_pct'] for x in group if x['drop_pct'] is not None]
        reproduced = (len({x['repeat'] for x in group}) >= 3 and len({x['order'] for x in group}) == 2
                      and all(x['exceeds_research_threshold'] for x in group))
        findings.append({k: group[0][k] for k in ('workload', 'regime', 'offered_load', 'unit', 'versions', 'config_hashes')} | {
            'blocks': len(group), 'repeats': len({x['repeat'] for x in group}),
            'orders': sorted({x['order'] for x in group}), 'drop_pct_range': [min(drops), max(drops)] if drops else None,
            'research_performance_effect_reproduced': reproduced,
            'interpretation': 'FULL_EDR_AND_AUDIT_PROFILE_EFFECT' if reproduced else 'INSUFFICIENT_OR_NO_REPRODUCED_EFFECT'})
    return {'schema_version': 1, 'synthetic': document.get('synthetic', False), 'runs': summaries,
            'comparisons': comparisons, 'rejected_blocks': rejected, 'findings': findings,
            'limitations': ['Manifest declarations and evidence references are not independent live verification.',
                            'Research threshold >5% and above control scatter is not a PT specification.',
                            'No conclusion about NGFW limit follows from generator or host limited outcomes.',
                            'OS CPU and native dataplane measurements must remain separate.',
                            'Latency, errors and session capacity require separate interpretation.']}
