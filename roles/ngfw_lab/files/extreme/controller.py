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
    from . import plan
except ImportError:
    import plan


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


def validate_native(path, boot_id, now):
    """Validate fresh DB read-back, not a boolean declaring collection healthy."""
    path = Path(path)
    receipt = read_json(path)
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
    expected = receipt.get('required_measurements')
    if not isinstance(expected, list) or not expected or not all(isinstance(v, str) and v for v in expected):
        raise Blocked('native required measurement catalog is absent')
    points = raw.get('points')
    if not isinstance(points, list) or not points:
        raise Blocked('native DB query returned no points')
    newest = {}
    for point in points:
        stamp = epoch(point['time'])
        name = point['measurement']
        newest[name] = max(newest.get(name, 0), stamp)
    if any(name not in newest or not 0 <= now - newest[name] <= freshness for name in expected):
        raise Blocked('native required measurements missing/stale')
    return dict(receipt_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                query_sha256=receipt['query_evidence']['sha256'], collected_at=receipt['collected_at'],
                source_interval_seconds=interval, database=receipt['database'], newest=newest,
                query_response=raw,
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
    control_topology = json.loads(artifact(raw.get('control_topology_evidence'), base))
    if plan.fingerprint(control_topology) != raw['control_topology_hash']:
        raise Blocked('direct control topology evidence differs')
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
    def monitor(self):
        try:
            health = validate_native(self.native_health, self.boot_id, time.time())
        except (Blocked, OSError, ValueError, KeyError) as error:
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
        super().monitor()


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
    argv_set = plan.commands(window['offered'], campaign['target'], seconds, ident, campaign.get('source_ips', []))
    parts = window['offered'].get('components', [window['offered']])
    budget = duration_budget(window['offered'], seconds)
    jobs = []
    handles = []
    monitor.context = dict(run_id=window['run_id'], phase=phase, offered=window['offered'], profile=window['profile'])
    monitor.monitor()
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
            for job in jobs:
                if job['process'] is not None:
                    job['row'].update(exit_code=job['process'].poll(), process_end=job['row']['process_end'] or utc())
            for handle in handles:
                handle.flush()
                os.fsync(handle.fileno())
            legacy.save(monitor.directory / 'receipt.json', report)


def normalized(sample, baseline):
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
            and latency <= max(20, old_latency * 2) and audit.get('audit_lost') == 0)


def recover(monitor, baseline, seconds, timeout, monotonic=time.monotonic):
    if seconds < 120 or timeout < seconds:
        raise Blocked('recovery requires >=120s and a bounded normalization deadline')
    monitor.context = dict(monitor.context, phase='recovery', offered={})
    monitor.cancelled = False  # Load is already stopped; cancellation never restarts it.
    start = monotonic()
    monitor.pause(seconds)
    consecutive = 0
    while monotonic() - start <= timeout:
        monitor.monitor()
        consecutive = consecutive + 1 if normalized(monitor.window_samples[-1], baseline) else 0
        if consecutive >= 2:
            return {'status': 'RECOVERED', 'seconds': monotonic() - start, 'normalization': 'two consecutive healthy baseline-bound samples'}
        monitor.pause(monitor.config['sample_interval'])
    raise legacy.Abort('recovery did not normalize before deadline')


def execute_window(campaign, window, config, backend, measure_config, identity_argv,
                   identity_base, native_health, isolation_path, directory, recovery_timeout=600, campaign_base=None):
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
    try:
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
        risks = measurement.inventory_risks(before, measure_config)
        if risks:
            raise Blocked('; '.join(risks))
        identity = json.loads(backend.call(identity_argv, timeout=30))
        validate_identity(identity, campaign, window, before, topology, identity_base, time.time())
        legacy.save(directory / 'identity-before.json', identity)
        for name, reference in identity['artifacts'].items():
            if name in (*plan.HASH_FIELDS, 'profile'):
                (directory / ('identity-artifact-' + name + '.bin')).write_bytes(artifact(reference, identity_base))
        report.update(versions=identity['versions'], config_hashes=identity['config_hashes'],
                      profile_evidence=identity['profile_evidence'])
        report['native_preflight'] = validate_native(native_health, before['boot_id'], time.time())
        monitor = NativeRunner(config, backend, directory, report,
            legacy.Guard(config['limits'], True, 'warn'), {}, measurement.Guard(measure_config, initial_guest=before))
        monitor.native_health, monitor.boot_id = native_health, before['boot_id']
        previous_handlers = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[sig] = signal.signal(sig, lambda *_: setattr(monitor, 'cancelled', True))
        try:
            monitor.monitor()
            baseline = monitor.window_samples[-1]
            warmup = window['stabilization_seconds']
            if warmup:
                execute_components(monitor, window, campaign, 'stabilization', warmup, report)
            execute_components(monitor, window, campaign, 'measure', window['measure_seconds'], report)
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
            after = backend.extended_guest(inventory=True)
            legacy.save(directory / 'inventory-after.json', after)
            if before:
                changed = measurement.control_changes(before, after)
                if after.get('inventory') != before.get('inventory') or after.get('boot_id') != before.get('boot_id'):
                    changed.append('guest inventory/boot changed')
                topology_after = backend.topology()
                legacy.save(directory / 'topology-after.json', topology_after)
                if topology_after != topology:
                    changed.append('topology changed during window')
                identity_after = json.loads(backend.call(identity_argv, timeout=30))
                validate_identity(identity_after, campaign, window, after, topology_after, identity_base, time.time())
                legacy.save(directory / 'identity-after.json', identity_after)
                if changed:
                    report['gaps'] += changed
                    if report['outcome'] != 'SAFETY_STOP':
                        report['outcome'] = 'INVALID_MEASUREMENT'
        except (legacy.Abort, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
            report['gaps'].append('after inventory unavailable: ' + type(error).__name__)
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
                 'identity-argv-file', 'native-health', 'isolation'):
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
    measure = measurement.validate(read_json(args.measurement_config))
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
                args.campaign.parent)
        finally:
            backend.pool.shutdown(wait=True)
        print(json.dumps({'directory': str(directory), 'outcome': result['outcome'],
                          'valid': result['valid'], 'reason': result.get('reason')}))
    return 0 if result['outcome'] == 'COMPLETED' else 2


if __name__ == '__main__':
    raise SystemExit(main())
