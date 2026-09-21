#!/usr/bin/env python3
"""Offline, conservative H2 numeric eligibility check. Never assigns SUPPORTED."""
import argparse
import hashlib
import json
import math
from pathlib import Path


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def verify(directory):
    manifest = read(directory / 'manifest.json')
    names = set()
    for item in manifest['files']:
        path = directory / item['file']
        if path.is_symlink() or path.resolve().parent != directory.resolve():
            raise ValueError('invalid artifact path')
        digest = hashlib.sha256()
        with path.open('rb') as src:
            for block in iter(lambda: src.read(1048576), b''):
                digest.update(block)
        if path.stat().st_size != item['bytes'] or digest.hexdigest() != item['sha256']:
            raise ValueError('artifact changed since final manifest: ' + path.name)
        names.add(path.name)
    if not {'summary.json', 'metrics.ndjson', 'inventory-before.json', 'inventory-after.json'} <= names:
        raise ValueError('required measurement artifacts absent from manifest')


def windows(directory):
    directory = Path(directory)
    verify(directory)
    summary = read(directory / 'summary.json')
    if summary.get('status') != 'completed' or not summary.get('measurement_config') or not summary.get('context'):
        raise ValueError('completed extended run with methodology context required')
    before, after = [read(directory / f'inventory-{when}.json') for when in ('before', 'after')]
    for key in ('rules_sha256', 'auditd_conf_sha256', 'forwarder_conf_sha256'):
        if not before['inventory'].get(key) or before['inventory'][key] != after['inventory'].get(key):
            raise ValueError('effective inventory not stable')
    # Bound memory for long campaigns; analyze one completed runner at a time.
    metrics_path = directory / 'metrics.ndjson'
    if metrics_path.stat().st_size > 134217728:
        raise ValueError('metrics exceeds 128 MiB analysis bound; split campaign')
    rows = [json.loads(line) for line in metrics_path.read_text(encoding='utf-8').splitlines() if line]
    output = {}
    for result in summary['results']:
        if result['phase'] != 'measure':
            continue
        key = result['scenario'] + '/' + str(result['repetition'])
        if key in output:
            raise ValueError('duplicate scenario/repetition')
        start, end = result['window_started_monotonic'], result['window_finished_monotonic']
        selected = []
        for old, row in zip(rows, rows[1:]):
            if (old['monotonic'] >= start and row['monotonic'] <= end and
                    row.get('phase') == 'measure' and old.get('phase') == 'measure' and
                    row.get('scenario') == result['scenario'] and row.get('repetition') == result['repetition']):
                selected.append((row['monotonic'] - old['monotonic'], row))
        issues, cpu_sum, seconds = [], 0, 0
        for duration, row in selected:
            guest = (row.get('measurement') or {}).get('guest') or {}
            derived = guest.get('derived') or {}
            cpu = derived.get('cpu', {}).get('cpu', {}).get('busy_pct')
            audit = guest.get('audit') or {}
            if not finite(cpu) or duration <= 0 or derived.get('gaps') or row.get('unavailable'):
                issues.append('incomplete measurement interval')
                continue
            if audit.get('lost') != 0 or guest.get('boot_id') != before.get('boot_id'):
                issues.append('loss or boot change')
                continue
            cpu_sum += cpu * duration
            seconds += duration
        coverage = seconds / (end - start) if end > start else 0
        if len(selected) < 3 or coverage < .8:
            issues.append('less than three intervals or 80% window coverage')
        metrics = result['metrics']
        work = metrics.get('received_bps', metrics.get('requests_per_second'))
        if not finite(work) or work <= 0 or metrics.get('errors', 0) != 0 or metrics.get('loss_pct', 0) > summary['config']['limits']['udp_loss_pct']:
            issues.append('workload missing, errored or loss threshold exceeded')
        output[key] = {'cpu_pct': cpu_sum / seconds if seconds else None, 'achieved_work': work,
                       'coverage': coverage, 'issues': sorted(set(issues)), 'metrics': metrics}
    return summary, before, output


def evaluate(paths):
    if len({Path(p).resolve() for p in paths}) != 4:
        raise ValueError('four distinct A-before/B/C-NET/A-after runs required')
    runs = [windows(path) for path in paths]
    summaries = [r[0] for r in runs]
    expected = ('A', 'B', 'C-NET', 'A')
    for summary, profile in zip(summaries, expected):
        if summary['audit_profile'] != profile or summary['context']['profile_id'] != profile:
            raise ValueError('H2 requires profiles A, B, C-NET, A in that order')
        for key in ('signature', 'measurement_config', 'tool_sha256'):
            if summary[key] != summaries[0][key]:
                raise ValueError('matrix/topology/observer settings mismatch')
        for key in ('campaign_id', 'comparison_id', 'workload_id'):
            if summary['context'][key] != summaries[0]['context'][key]:
                raise ValueError('campaign/comparison/workload context mismatch')
    if any(summaries[i]['finished_at'] > summaries[i+1]['started_at'] for i in range(3)):
        raise ValueError('runs overlap or are not chronological A/B/C/A')
    if runs[0][1]['inventory']['rules_sha256'] != runs[3][1]['inventory']['rules_sha256']:
        raise ValueError('final A rules differ from original A')
    # Auditd daemon settings must not change in the network-rule ablation campaign.
    if len({r[1]['inventory']['auditd_conf_sha256'] for r in runs}) != 1:
        raise ValueError('auditd configuration is a confounder')
    if len({r[1]['inventory']['forwarder_conf_sha256'] for r in runs}) != 1:
        raise ValueError('forwarder/observer configuration is a confounder')
    keys = set(runs[0][2])
    if not keys or any(set(run[2]) != keys for run in runs):
        raise ValueError('measurement windows differ')
    result = []
    for key in sorted(keys):
        scenario = key.rsplit('/', 1)[0]
        group_keys = [k for k in keys if k.rsplit('/', 1)[0] == scenario]
        rows = [run[2][key] for run in runs]
        issues = [issue for row in rows for issue in row['issues']]
        controls = [run[2][k]['cpu_pct'] for run in (runs[0], runs[3]) for k in group_keys]
        if len(group_keys) < 3 or not all(finite(v) for v in controls):
            issues.append('need at least three valid paired control repeats')
        baseline = (rows[0]['cpu_pct'] + rows[3]['cpu_pct']) / 2 if all(finite(rows[i]['cpu_pct']) for i in (0, 3)) else None
        spread = max(controls) - min(controls) if controls and all(finite(v) for v in controls) else None
        added = rows[1]['cpu_pct'] - baseline if finite(rows[1]['cpu_pct']) and baseline is not None else None
        reduction = None
        if added is None or spread is None or added <= max(5, 2 * spread):
            issues.append('added B CPU does not exceed max(5 pp, 2 * control spread)')
        elif finite(rows[2]['cpu_pct']):
            reduction = 100 * (rows[1]['cpu_pct'] - rows[2]['cpu_pct']) / added
        works = [row['achieved_work'] for row in rows]
        if not all(finite(v) and v > 0 for v in works) or max(works) / min(works) > 1.05:
            issues.append('achieved work differs by more than 5%')
        result.append({'window': key, 'cpu_A_reference_pct': baseline, 'control_spread_pp': spread,
                       'added_B_cpu_pp': added, 'removed_added_cpu_pct': reduction,
                       'numeric_70pct_met': not issues and reduction is not None and reduction >= 70,
                       'issues': sorted(set(issues))})
    return {'schema_version': 1, 'hypothesis': 'H2', 'test_id': 'T10',
            'run_ids': [s.get('run_id') for s in summaries], 'windows': result,
            'all_numeric_conditions_met': all(r['numeric_70pct_met'] for r in result),
            'verdict': 'REQUIRES_SEMANTIC_REVIEW',
            'required_review': ['T11 administrative audit preservation with actual events',
                                'approved exact network-rule difference and active source',
                                'NGFW policy/offload/debug, thermal and other observer confounders',
                                'delivery completeness, clock alignment and recovery controls'],
            'boundary': 'Necessary numeric checks, not a causal or automatic SUPPORTED verdict.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('a-before', 'b', 'c', 'a-after'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(evaluate([args.a_before, args.b, args.c, args.a_after]), indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
