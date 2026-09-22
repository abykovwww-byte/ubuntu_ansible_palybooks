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


def windows(directory, allow_partial=False):
    directory = Path(directory)
    verify(directory)
    summary = read(directory / 'summary.json')
    if (summary.get('status') != 'completed' and not allow_partial) or not summary.get('measurement_config') or not summary.get('context'):
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
        if allow_partial:
            key += '/attempt' + str(result.get('attempt', 1))
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
        if result.get('status', 'PASS') != 'PASS':
            issues.append('window outcome: ' + result['status'])
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
        if metrics.get('generator_limited') or metrics.get('invalid_reasons'):
            issues.append('generator limited or invalid window')
        work = metrics.get('received_bps', metrics.get('requests_per_second'))
        if not finite(work) or work <= 0 or metrics.get('errors', 0) != 0 or metrics.get('loss_pct', 0) > summary['config']['limits']['udp_loss_pct']:
            issues.append('workload missing, errored or loss threshold exceeded')
        output[key] = {'cpu_pct': cpu_sum / seconds if seconds else None, 'achieved_work': work,
                       'coverage': coverage, 'issues': sorted(set(issues)), 'metrics': metrics,
                       'scenario': result['scenario'], 'repetition': result['repetition'],
                       'phase_seconds': end - start, 'telemetry': telemetry(rows, selected, result)}
    return summary, before, output


def telemetry(rows, selected, result):
    """Observed measurements only; absent audit exports/central receipts stay null."""
    out = {'audit_eps': None, 'events_by_key': None, 'central_required_events': None,
           'edr_connected_central': None, 'gaps': ['source audit window export required',
           'central EDR receipt evidence required']}
    processes, disks, backlogs, lost, log_rates, health, forwarder = {}, {}, [], [], [], [], []
    for seconds, row in selected:
        guest = (row.get('measurement') or {}).get('guest') or {}
        derived = guest.get('derived') or {}
        for pid, value in (derived.get('processes') or {}).items():
            processes.setdefault(value.get('comm', pid), []).append((seconds, value.get('cpu_one_core_pct')))
        for device, value in (derived.get('disks') or {}).items():
            disks.setdefault(device, []).append(value)
        audit = guest.get('audit') or {}
        if finite(audit.get('backlog')):
            backlogs.append(audit['backlog'])
        if finite(audit.get('lost')):
            lost.append(audit['lost'])
        if finite(derived.get('log_bytes_per_second')):
            log_rates.append((seconds, derived['log_bytes_per_second']))
        health.append({k: row.get(k) for k in ('management_ok', 'mngt_ok', 'dataplane_ok')})
        if guest.get('forwarder') is not None:
            forwarder.append(guest['forwarder'])
    def mean(values):
        usable = [(dt, v) for dt, v in values if finite(v) and dt > 0]
        return sum(dt*v for dt, v in usable) / sum(dt for dt, _ in usable) if usable else None
    end = result['window_finished_monotonic']
    drain = next((row['monotonic'] - end for row in rows if row['monotonic'] >= end
                  and row.get('phase') == 'idle' and row.get('scenario') == result['scenario']
                  and row.get('repetition') == result['repetition']
                  and row.get('attempt', 1) == result.get('attempt', 1)
                  and ((row.get('measurement') or {}).get('guest') or {}).get('audit', {}).get('backlog') == 0), None)
    out.update(process_cpu_one_core_pct={k: mean(v) for k, v in processes.items()},
               audit_bytes_s=mean(log_rates), backlog_peak=max(backlogs, default=None),
               backlog_drain_seconds=drain, lost_peak=max(lost, default=None), disk_io=disks,
               health=health, forwarder=forwarder or None)
    if not processes:
        out['gaps'].append('EDR/auditd/forwarder process cost unavailable')
    if drain is None:
        out['gaps'].append('backlog drain not observed')
    return out


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


def qualification_evaluate(mode, paths, evidence=None):
    expected = {'net': ['B-EDR-CURRENT', 'C-EDR-NET', 'B-EDR-CURRENT'],
                'proc': ['B-EDR-CURRENT', 'C-EDR-PROC', 'B-EDR-CURRENT'],
                'confirmation': ['A-NO-EDR', 'B-EDR-CURRENT', 'D-EDR-CANDIDATE', 'A-NO-EDR']}
    if not paths or len({Path(p).resolve() for p in paths}) != len(paths):
        raise ValueError('distinct chronological run directories required')
    runs = [windows(path, allow_partial=mode == 'calibration') for path in paths]
    summaries = [r[0] for r in runs]
    if evidence is not None:
        for summary, _, rows in runs:
            for key, row in rows.items():
                supplement = evidence.get('windows', {}).get(summary.get('run_id'), {}).get(key)
                if supplement:
                    row['declared_evidence'] = supplement
    profiles = [s['audit_profile'] for s in summaries]
    if mode in expected and profiles != expected[mode]:
        raise ValueError('wrong ordered qualification profiles')
    if mode == 'calibration' and set(profiles) != {'B-EDR-CURRENT'}:
        raise ValueError('calibrate on unchanged B-EDR-CURRENT only')
    if mode == 'endurance' and (profiles != ['D-EDR-CANDIDATE'] * 2 or
                               [s['config']['duration'] for s in summaries] != [1800, 3600]):
        raise ValueError('endurance requires D 30-minute then 60-minute runs')
    for index, summary in enumerate(summaries):
        if summary['context']['profile_id'] != summary['audit_profile']:
            raise ValueError('profile context mismatch')
        for key in ('measurement_config', 'tool_sha256'):
            left, right = summary[key], summaries[0][key]
            if key == 'measurement_config':
                if not isinstance(left, dict) or not isinstance(right, dict):
                    raise ValueError('invalid observer settings')
                # A snapshot changes PID/boot identities, not observer selection/cadence.
                identity = {'guest_process_targets', 'guest_process_boot_id'}
                left, right = [{k: v for k, v in cfg.items() if k not in identity} for cfg in (left, right)]
            if left != right:
                raise ValueError('different observer settings/tools')
        for key in ('campaign_id', 'comparison_id', 'workload_id'):
            if summary['context'][key] != summaries[0]['context'][key]:
                raise ValueError('different campaign/comparison/workload')
        if mode in expected and summary['signature'] != summaries[0]['signature']:
            raise ValueError('different workloads/resources/topology')
        if mode == 'endurance':
            for field in ('scenarios', 'warmup', 'idle', 'repetitions', 'sample_interval', 'limits'):
                if summary['config'][field] != summaries[0]['config'][field]:
                    raise ValueError('endurance workloads/settings mismatch')
            if summary.get('topology') != summaries[0].get('topology'):
                raise ValueError('endurance topology mismatch')
        if index and summaries[index-1]['finished_at'] > summary['started_at']:
            raise ValueError('runs overlap or chronology changed')
    if mode in expected:
        if runs[0][1]['inventory']['rules_sha256'] != runs[-1][1]['inventory']['rules_sha256']:
            raise ValueError('incomplete baseline restoration: ordered rules hash differs')
    for field in ('auditd_conf_sha256', 'forwarder_conf_sha256'):
        if len({r[1]['inventory'][field] for r in runs}) != 1:
            raise ValueError('auditd/forwarder configuration changed')
    issues, comparisons = [], []
    if mode == 'calibration':
        maxima = {}
        for summary, _, rows in runs:
            for key, row in rows.items():
                if row['issues']:
                    continue
                scenario = next(s for s in summary['config']['scenarios'] if s['name'] == row['scenario'])
                maxima[scenario['kind']] = max(maxima.get(scenario['kind'], 0), row['achieved_work'])
        return {'mode': mode, 'verdict': 'CALIBRATION_ONLY', 'runs': [r[2] for r in runs],
                'max_valid_achieved_work': maxima,
                'suggested_working_range': {k: [v*.7, v*.85] for k, v in maxima.items()},
                'boundary': 'Measured stable maxima, not proven NGFW limits; validate selected working loads before comparison.'}
    keys = set(runs[0][2])
    if not keys or any(set(r[2]) != keys for r in runs):
        raise ValueError('different or missing measurement windows')
    for key in sorted(keys):
        rows = [r[2][key] for r in runs]
        row_issues = [x for r in rows for x in r['issues']]
        work = [r['achieved_work'] for r in rows]
        if not all(finite(v) and v > 0 for v in work) or max(work) / min(work) > 1.05:
            row_issues.append('actual workload differs by more than 5%')
        base = rows[0]['cpu_pct']
        cpu_deltas = [r['cpu_pct'] - base if finite(r['cpu_pct']) and finite(base) else None for r in rows]
        comparison = {'window': key, 'profiles': profiles, 'runs': rows,
                      'cpu_delta_from_first_pp': cpu_deltas, 'issues': row_issues}
        if mode == 'confirmation':
            control = (work[0] + work[-1]) / 2 if all(finite(v) for v in (work[0], work[-1])) else None
            candidate = rows[2]['metrics']
            comparison['candidate_work_within_5pct'] = control is not None and work[2] >= .95 * control
            q = [r['metrics'].get('latency_ms_bucket_upper_bounds', {}).get('p95') for r in rows]
            latency_bad = (all(finite(q[i]) for i in (0, 2, 3)) and
                           q[2] > (q[0]+q[3])/2 * 1.1 and q[2] > (q[0]+q[3])/2 + 5)
            comparison['candidate_p95_degradation'] = latency_bad if all(finite(v) for v in q) else None
            if not comparison['candidate_work_within_5pct'] or latency_bad:
                row_issues.append('candidate work/latency qualification threshold failed')
            if candidate.get('loss_pct', 0) > min(.5, rows[0]['metrics'].get('loss_pct', .5), rows[-1]['metrics'].get('loss_pct', .5)):
                row_issues.append('candidate UDP loss worse than control')
            scenario = rows[0]['scenario']
            if len([k for k in keys if runs[0][2][k]['scenario'] == scenario]) < 3:
                row_issues.append('fewer than three paired repetitions')
        comparisons.append(comparison)
        issues.extend(row_issues)
    gaps = ['central EDR receipt and useful-event preservation require independent evidence',
            'exact source rule difference and restoration receipt require review']
    if mode == 'confirmation':
        gaps += ['clean A clone/snapshot provenance required', 'candidate D rationale required',
                 '30/60-minute endurance required']
    # A supplemental bundle is declared evidence only; never auto-promote it to GO.
    return {'mode': mode, 'run_ids': [s.get('run_id') for s in summaries], 'windows': comparisons,
            'numeric_windows_eligible': not issues, 'issues': sorted(set(issues)),
            'declared_supplemental_evidence': evidence, 'gaps': gaps,
            'verdict': 'INCONCLUSIVE' if issues else 'REQUIRES_SEMANTIC_REVIEW',
            'production_recommendation': 'NO-GO_PENDING_QUALIFICATION',
            'boundary': 'No automatic causal H1/H2 or production GO verdict.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['legacy-h2', 'calibration', 'net', 'proc', 'confirmation', 'endurance'], default='legacy-h2')
    parser.add_argument('--runs', type=Path, nargs='+')
    parser.add_argument('--evidence', type=Path, help='private source/central evidence bundle, declared not automatically accepted')
    for name in ('a-before', 'b', 'c', 'a-after'):
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    if args.mode == 'legacy-h2':
        if not all((args.a_before, args.b, args.c, args.a_after)):
            parser.error('legacy H2 still requires full A/B/C-NET/A')
        result = evaluate([args.a_before, args.b, args.c, args.a_after])
    else:
        result = qualification_evaluate(args.mode, args.runs, read(args.evidence) if args.evidence else None)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
