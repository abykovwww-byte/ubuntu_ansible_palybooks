import copy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'extreme'))
spec = importlib.util.spec_from_file_location('extreme_controller', ROOT / 'extreme/controller.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def timestamp(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def file_ref(directory, name, value):
    data = json.dumps(value).encode()
    path = directory / name
    path.write_bytes(data)
    return {'path': name, 'sha256': hashlib.sha256(data).hexdigest()}


def aligned_query(directory, raw, offset_ns=0):
    """Synthetic clock/query receipts; never claims real NTP synchronization."""
    bounds = dict(started_epoch_ns=998_000_000_000, completed_epoch_ns=998_200_000_000,
                  started_monotonic_ns=98_000_000_000, completed_monotonic_ns=98_200_000_000)
    raw['query_bounds'] = bounds
    query_ref = file_ref(directory, 'query.json', raw)
    alignment = dict(schema_version=1, scope='bounded_clock_alignment_not_ntp_synchronization',
                     guest_boot_id=raw['guest_boot_id'], query_bounds=bounds,
                     query_evidence_sha256=query_ref['sha256'],
                     offset_min_ns=offset_ns-50_000_000, offset_max_ns=offset_ns+50_000_000,
                     limits=dict(max_uncertainty_seconds=1, max_offset_spread_seconds=.5, max_clock_step_seconds=.05))
    for name, send in (('before', 997_800_000_000), ('after', 998_300_000_000)):
        guest = dict(guest_epoch_ns=send+50_000_000-offset_ns,
                     guest_monotonic_ns=send-900_000_000_000+50_000_000,
                     guest_boot_id=raw['guest_boot_id'])
        alignment[name] = dict(host_send_epoch_ns=send, host_receive_epoch_ns=send+100_000_000,
                               host_send_monotonic_ns=send-900_000_000_000,
                               host_receive_monotonic_ns=send-900_000_000_000+100_000_000,
                               **guest, guest_evidence=file_ref(directory, 'guest-clock-' + name + '.json', guest))
    return query_ref, file_ref(directory, 'alignment.json', alignment)


class Clock:
    def __init__(self):
        self.now = 0
    def time(self):
        return self.now
    def sleep(self, value):
        self.now += value


class Process:
    def __init__(self, clock, finish=1, code=0):
        self.clock, self.finish, self.code = clock, finish, code
        self.terminated = False
    def poll(self):
        return self.code if self.terminated or self.clock.now >= self.finish else None
    def terminate(self):
        self.terminated = True
        self.code = -15
    def kill(self):
        self.terminated = True
        self.code = -9
    def wait(self, timeout=None):
        return self.poll()


class Monitor:
    def __init__(self, directory, clock, fail_at=None):
        self.directory, self.clock, self.fail_at = directory, clock, fail_at
        self.backend = SimpleNamespace(compose=['docker', 'compose'], endpoint=Mock())
        self.config = {'sample_interval': 2}
        self.cancelled = False
        self.context = {}
        self.calls = 0
        self.window_samples = []
    def monitor(self):
        self.calls += 1
        if self.calls == self.fail_at:
            raise c.legacy.WindowStop('GLOBAL_STOP', 'mandatory audit lost')
        self.window_samples.append(healthy())
    def pause(self, seconds):
        self.clock.sleep(seconds)
        self.monitor()


def healthy():
    return {'unavailable': [], 'dataplane_ok': True, 'management_ok': True,
            'mngt_ok': True, 'vms_ok': True, 'service_probe_ms': 2,
            'audit': {'audit_lost': 0, 'audit_backlog': 0, 'memory_used_pct': 40},
            'host': {'memory_used_pct': 30}}


def window(mixed=False):
    offered = {'kind': 'short-tcp', 'rate': 5000, 'concurrency': 512}
    if mixed:
        offered = {'kind': 'mixed', 'components': [
            {'kind': 'tcp', 'bps': 900000000, 'parallel': 4}, offered,
            {'kind': 'dns', 'rate': 5000, 'concurrency': 512}]}
    return {'run_id': 'run-A', 'profile': 'A', 'offered': offered}


class NativeReceiptTests(unittest.TestCase):
    def test_real_producer_schema_integrates_with_independent_controller_validator(self):
        producer_tests = ROOT.parents[1] / 'ngfw_metrics/tests/test_native_health.py'
        spec = importlib.util.spec_from_file_location('native_health_contract_fixture', producer_tests)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fixture = module.NativeHealthTests('test_skew_alignment_preserves_original_nanosecond_timestamp')
        fixture.setUp()
        try:
            receipt = fixture.collect()
            self.assertEqual(receipt['status'], 'HEALTHY')
            result = c.validate_native(fixture.output, module.BOOT, fixture.clock.time_ns() / 1e9)
            self.assertEqual(result['query_response']['points'][0]['source_timestamp_ns'], fixture.last_source_timestamp)
            self.assertLess(result['age_ranges']['native_measurement']['max_seconds'], 16)
        finally:
            fixture.tearDown()

    def fixture(self, directory):
        raw = {'database': 'lab', 'guest_boot_id': 'boot', 'partial': False,
               'points': [{'measurement': 'native_dp', 'time': timestamp(990), 'source_timestamp_ns': 990_000_000_000},
                          {'measurement': 'net', 'time': timestamp(995), 'source_timestamp_ns': 995_000_000_000}]}
        ref, clock_ref = aligned_query(directory, raw)
        receipt = {'schema_version': 2, 'status': 'HEALTHY', 'database': 'lab', 'guest_boot_id': 'boot', 'collected_at': timestamp(999),
                   'source_interval_seconds': 10, 'required_measurements': ['native_dp', 'net'],
                   'query_evidence': ref, 'clock_alignment': clock_ref}
        path = directory / 'health.json'
        path.write_text(json.dumps(receipt))
        return path, receipt, raw

    def test_fresh_points_hash_and_identity_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, receipt, raw = self.fixture(directory)
            self.assertEqual(c.validate_native(path, 'boot', 1000)['newest']['net'], 995)
            with self.assertRaises(c.Blocked):
                c.validate_native(path, 'other-boot', 1000)
            with self.assertRaises(c.Blocked):
                c.validate_native(path, 'boot', 1100)
            (directory / 'query.json').write_text('{}')
            with self.assertRaises(c.Blocked):
                c.validate_native(path, 'boot', 1000)

    def test_boolean_health_cannot_replace_real_points(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, receipt, raw = self.fixture(directory)
            raw['points'] = [raw['points'][1]]
            receipt['query_evidence'], receipt['clock_alignment'] = aligned_query(directory, raw)
            receipt['healthy'] = True
            path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(c.Blocked, 'missing/stale'):
                c.validate_native(path, 'boot', 1000)

    def test_failed_native_health_saved_before_legacy_monitor(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor = object.__new__(c.NativeRunner)
            monitor.directory = Path(tmp)
            monitor.native_health = Path(tmp) / 'missing.json'
            monitor.boot_id = 'boot'
            monitor.context = {'phase': 'measure'}
            with self.assertRaises(c.Blocked):
                monitor.monitor()
            row = json.loads((Path(tmp) / 'native-health-failures.ndjson').read_text())
            self.assertEqual(row['outcome'], 'INVALID_MEASUREMENT')
            self.assertIsNone(row['receipt'])

    def test_guest_offset_is_bounded_without_changing_raw_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, receipt, raw = self.fixture(directory)
            for point in raw['points']:
                point['source_timestamp_ns'] -= 22_000_000_000
                point['time'] = timestamp(point['source_timestamp_ns'] / 1e9)
            receipt['query_evidence'], receipt['clock_alignment'] = aligned_query(directory, raw, 22_000_000_000)
            path.write_text(json.dumps(receipt))
            result = c.validate_native(path, 'boot', 1000)
            self.assertEqual(result['query_response']['points'], raw['points'])
            self.assertEqual(result['age_ranges']['net'], {'min_seconds': 4.95, 'max_seconds': 5.05})
            raw['points'][1].update(time=timestamp(978), source_timestamp_ns=978_000_000_000)
            receipt['query_evidence'], receipt['clock_alignment'] = aligned_query(directory, raw, 22_000_000_000)
            path.write_text(json.dumps(receipt))
            with self.assertRaisesRegex(c.Blocked, 'missing/stale'):
                c.validate_native(path, 'boot', 1000)  # full interval reaches future for latest point

    def test_clock_identity_steps_limits_and_query_binding_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for change in ('no-proof', 'guest-tamper', 'step', 'wide-policy', 'wrong-query'):
                path, receipt, raw = self.fixture(directory)
                alignment = json.loads((directory / 'alignment.json').read_text())
                if change == 'no-proof':
                    del receipt['clock_alignment']
                elif change == 'guest-tamper':
                    (directory / 'guest-clock-before.json').write_text('{}')
                else:
                    if change == 'step':
                        alignment['before']['host_receive_epoch_ns'] += 100_000_000
                    elif change == 'wide-policy':
                        alignment['limits']['max_offset_spread_seconds'] = 2
                    else:
                        alignment['query_evidence_sha256'] = '0' * 64
                    receipt['clock_alignment'] = file_ref(directory, 'alignment.json', alignment)
                path.write_text(json.dumps(receipt))
                with self.subTest(change=change), self.assertRaises(c.Blocked):
                    c.validate_native(path, 'boot', 1000)

    def test_whole_window_drift_is_separate_from_freshness(self):
        before = {'clock_alignment': dict(offset_min_ns=21_950_000_000, offset_max_ns=22_050_000_000,
                                         clock_alignment_sha256='a'*64)}
        after = copy.deepcopy(before)
        after['clock_alignment']['offset_min_ns'] += 500_000_000
        after['clock_alignment']['offset_max_ns'] += 500_000_000
        self.assertEqual(c.window_clock_summary(before, after)['drift_max_seconds'], .6)
        after['clock_alignment']['offset_max_ns'] += 500_000_000
        with self.assertRaisesRegex(c.Blocked, 'drift'):
            c.window_clock_summary(before, after)

    def test_post_label_is_insufficient_when_clock_bracket_precedes_measure_end(self):
        base = dict(offset_min_ns=0, offset_max_ns=100_000_000, clock_alignment_sha256='a'*64)
        before = {'clock_alignment': dict(base, alignment={'after': {
            'host_receive_epoch_ns': 100_000_000_000, 'host_receive_monotonic_ns': 10_000_000_000}})}
        after = {'clock_alignment': dict(base, alignment={'before': {
            'host_send_epoch_ns': 109_000_000_000, 'host_send_monotonic_ns': 19_000_000_000}})}
        bounds = dict(started_epoch_ns=101_000_000_000, completed_epoch_ns=110_000_000_000,
                      started_monotonic_ns=11_000_000_000, completed_monotonic_ns=20_000_000_000)
        with self.assertRaisesRegex(c.Blocked, 'do not enclose') as caught:
            c.window_clock_summary(before, after, bounds)
        self.assertEqual(caught.exception.clock_summary['status'], 'INVALID')
        after['clock_alignment']['alignment']['before'].update(host_send_epoch_ns=111_000_000_000,
                                                               host_send_monotonic_ns=21_000_000_000)
        self.assertEqual(c.window_clock_summary(before, after, bounds)['status'], 'BOUNDED')
        after['clock_alignment']['alignment']['before']['host_send_monotonic_ns'] = 19_000_000_000
        with self.assertRaisesRegex(c.Blocked, 'do not enclose'):
            c.window_clock_summary(before, after, bounds)


class IdentityTests(unittest.TestCase):
    def prerequisites(self, directory):
        invariant = {name: 'a' * 64 for name in c.plan.HASH_FIELDS}
        campaign = {'campaign_id': 'campaign', 'target': '10.77.20.10', 'invariants': invariant,
                    'receipts': {}, 'workloads': []}
        raw_ref = file_ref(directory, 'raw.json', {'actual': 'fixture-only'})
        common = {'scope': 'isolated-ngfw-lab', 'campaign_id': 'campaign', 'target': campaign['target'],
                  'config_hashes': invariant, 'observed_at': timestamp(999), 'evidence': [raw_ref]}
        for name in ('isolation', 'snapshot_A', 'snapshot_B', 'rollback', 'profile_A', 'profile_B',
                     'native_collection', 'observer_impact', 'resource_reservations'):
            raw = dict(common, kind=name)
            if name.startswith('snapshot_'):
                raw.update(snapshot_name=name, vm='lab-vm', creation_state='shut off')
            if name == 'rollback':
                raw.update(steps=['verified restore procedure'], restore_verification_evidence=raw_ref)
            if name == 'observer_impact':
                raw['observer_configuration_sha256'] = invariant['observation']
            if name == 'resource_reservations':
                raw['headroom'] = dict(fd_slots=100, source_port_tuples=100, receiver_sessions=100, host_memory_bytes=100)
            campaign['receipts'][name] = file_ref(directory, name + '.json', raw)
        control_topology = {'path': 'direct-control-no-ngfw'}
        control = dict(common, kind='control_capacity', status='VALID', load_hash=c.plan.fingerprint(window()['offered']),
                       topology_hash=invariant['topology'], offered_achieved=True, resources_unlimited=True,
                       duration_seconds=300, path_kind='direct_control', traverses_ngfw=False,
                       endpoint_config_hashes={k: invariant[k] for k in ('generator', 'receiver')},
                       control_topology_hash=c.plan.fingerprint(control_topology),
                       control_topology_evidence=file_ref(directory, 'control-topology.json', control_topology))
        campaign['workloads'] = [{'offered': window()['offered'], 'control_capacity':
            {'receipt': file_ref(directory, 'control.json', control)}}]
        backend = Mock()
        backend.call.side_effect = lambda argv: 'Name: ' + argv[-1] + '\nDomain: lab-vm\nState: shutoff\n'
        return campaign, dict(window(), measure_seconds=300), backend, control

    def test_prerequisite_scope_and_current_snapshot_readback(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            campaign, selected, backend, control = self.prerequisites(directory)
            snapshots = c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 1000)
            self.assertEqual(set(snapshots), {'snapshot_A', 'snapshot_B'})
            backend.call.side_effect = lambda argv: 'Name: wrong\nDomain: lab-vm\nState: running\n'
            with self.assertRaisesRegex(c.Blocked, 'snapshot-info'):
                c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 1000)

    def test_direct_capacity_cannot_be_dut_path_or_short_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            campaign, selected, backend, control = self.prerequisites(directory)
            control['traverses_ngfw'] = True
            campaign['workloads'][0]['control_capacity']['receipt'] = file_ref(directory, 'control.json', control)
            with self.assertRaisesRegex(c.Blocked, 'direct-path'):
                c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 1000)
            control.update(traverses_ngfw=False, duration_seconds=10)
            campaign['workloads'][0]['control_capacity']['receipt'] = file_ref(directory, 'control.json', control)
            with self.assertRaisesRegex(c.Blocked, 'direct-path'):
                c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 1000)

    def test_prerequisite_stale_or_plan_boolean_does_not_grant_stress(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            campaign, selected, backend, control = self.prerequisites(directory)
            with self.assertRaisesRegex(c.Blocked, 'scope/identity/age'):
                c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 100000)
            campaign['receipts']['isolation'] = True
            with self.assertRaisesRegex(c.Blocked, 'reference'):
                c.validate_campaign_artifacts(campaign, selected, directory, backend, {'ngfw_vm': 'lab-vm'}, 1000)

    def test_live_audit_hash_and_installed_package_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            topology = {'actual': 'isolated'}
            profile = {'edr_installed': False, 'audit_profile': 'stock',
                       'rules_sha256': 'rulehash', 'auditd_conf_sha256': 'confhash'}
            refs = {name: file_ref(directory, name + '.json', topology if name == 'topology' else {'actual': name})
                    for name in c.plan.HASH_FIELDS}
            refs['profile'] = file_ref(directory, 'profile.json', profile)
            invariants = {name: ref['sha256'] for name, ref in refs.items() if name != 'profile'}
            invariants['topology'] = c.plan.fingerprint(topology)
            receipt = {'collected_at': timestamp(999), 'profile': 'A', 'guest_boot_id': 'boot',
                'profile_evidence': profile, 'artifacts': refs, 'config_hashes': invariants,
                'versions': {'ngfw': 'read-back-build'}}
            before = {'boot_id': 'boot', 'inventory': {'rules_sha256': 'rulehash', 'auditd_conf_sha256': 'confhash'}}
            c.validate_identity(receipt, {'invariants': invariants}, {'profile': 'A'}, before, topology, directory, 1000)
            changed = copy.deepcopy(before)
            changed['inventory']['rules_sha256'] = 'different'
            with self.assertRaises(c.Blocked):
                c.validate_identity(receipt, {'invariants': invariants}, {'profile': 'A'}, changed, topology, directory, 1000)
            receipt['profile_evidence']['edr_installed'] = True
            with self.assertRaisesRegex(c.Blocked, 'package absence'):
                c.validate_identity(receipt, {'invariants': invariants}, {'profile': 'A'}, before, topology, directory, 1000)

    def test_watchdog_includes_buildup_and_is_bounded(self):
        self.assertEqual(c.duration_budget({'kind': 'held', 'sessions': 60000, 'open_rate': 5000}, 3600), 3642)
        with self.assertRaises(c.Blocked):
            c.duration_budget({'kind': 'held', 'sessions': 60000, 'open_rate': 1}, 3600)


class AuditEpochTests(unittest.TestCase):
    def fixture(self, directory):
        boot = '12345678-1234-1234-1234-123456789abc'
        fixed = dict(enabled=1, failure=1, pid=100, rate_limit=0, backlog_limit=8192, backlog_wait_time=1)
        before = dict(boot_id=boot, audit=dict(fixed, lost=60925),
                      inventory=dict(rules_sha256='a' * 64, auditd_conf_sha256='b' * 64))
        raw = dict(collected_at=timestamp(999), profile='B', snapshot=before)
        baseline = dict(schema_version=1, guest_boot_id=boot, lost=60925, readback_at=timestamp(999),
                        profile='B', rules_sha256='a' * 64, auditd_conf_sha256='b' * 64,
                        fixed_controls=fixed, incident_ref='synthetic-historical-incident',
                        source_evidence=file_ref(directory, 'audit-source.json', raw))
        path = directory / 'baseline.json'
        path.write_text(json.dumps(baseline))
        return path, baseline, before

    def test_epoch_requires_fresh_raw_identical_boot_loss_controls_and_incident(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, baseline, before = self.fixture(directory)
            proof, original, source = c.validate_audit_baseline(path, before, 1000, 'B')
            self.assertEqual(proof['baseline']['lost'], 60925)
            self.assertEqual(original, path.read_bytes())
            changed = copy.deepcopy(before)
            changed['audit']['lost'] = 60926
            with self.assertRaises(c.legacy.WindowStop) as caught:
                c.validate_audit_baseline(path, changed, 1000, 'B')
            self.assertEqual(caught.exception.outcome, 'GLOBAL_STOP')
            for field, value in (('pid', 101),):
                changed = copy.deepcopy(before)
                changed['audit'][field] = value
                with self.assertRaisesRegex(c.Blocked, 'differs'):
                    c.validate_audit_baseline(path, changed, 1000, 'B')
            with self.assertRaisesRegex(c.Blocked, 'stale'):
                c.validate_audit_baseline(path, before, 1060, 'B')
            baseline['incident_ref'] = ''
            path.write_text(json.dumps(baseline))
            with self.assertRaisesRegex(c.Blocked, 'incident'):
                c.validate_audit_baseline(path, before, 1000, 'B')
            baseline['incident_ref'] = 'synthetic-historical-incident'
            path.write_text(json.dumps(baseline))
            (directory / 'audit-source.json').write_text('{}')
            with self.assertRaisesRegex(c.Blocked, 'hash changed'):
                c.validate_audit_baseline(path, before, 1000, 'B')

    def test_epoch_binds_source_current_and_returned_profile_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path, baseline, before = self.fixture(directory)
            with self.assertRaisesRegex(c.Blocked, 'profile differs'):
                c.validate_audit_baseline(path, before, 1000, 'A')
            for field in ('rules_sha256', 'auditd_conf_sha256'):
                incomplete = baseline.copy()
                incomplete.pop(field)
                path.write_text(json.dumps(incomplete))
                with self.assertRaisesRegex(c.Blocked, 'hash unavailable'):
                    c.validate_audit_baseline(path, before, 1000, 'B')
            path.write_text(json.dumps(baseline))
            identity = dict(profile='B', profile_evidence=before['inventory'].copy())
            c.validate_audit_epoch_identity(baseline, identity)
            for field in ('rules_sha256', 'auditd_conf_sha256'):
                changed = copy.deepcopy(before)
                changed['inventory'][field] = 'c' * 64
                with self.assertRaisesRegex(c.Blocked, 'identity changed'):
                    c.validate_audit_baseline(path, changed, 1000, 'B')
                with self.assertRaisesRegex(c.Blocked, 'identity changed'):
                    c.validate_audit_epoch_inventory(baseline, changed)
                changed_identity = copy.deepcopy(identity)
                changed_identity['profile_evidence'][field] = 'c' * 64
                with self.assertRaisesRegex(c.Blocked, 'identity changed'):
                    c.validate_audit_epoch_identity(baseline, changed_identity)
            with self.assertRaisesRegex(c.Blocked, 'identity changed'):
                c.validate_audit_epoch_identity(baseline, identity | {'profile': 'A'})
            raw = dict(collected_at=timestamp(999), profile='A', snapshot=before)
            baseline['source_evidence'] = file_ref(directory, 'audit-source.json', raw)
            path.write_text(json.dumps(baseline))
            with self.assertRaisesRegex(c.Blocked, 'source profile differs'):
                c.validate_audit_baseline(path, before, 1000, 'B')
            raw['profile'] = 'B'
            raw['snapshot']['inventory']['rules_sha256'] = 'd' * 64
            baseline['source_evidence'] = file_ref(directory, 'audit-source.json', raw)
            path.write_text(json.dumps(baseline))
            with self.assertRaisesRegex(c.Blocked, 'identity changed'):
                c.validate_audit_baseline(path, before, 1000, 'B')

    def test_strict_legacy_and_explicit_epoch_preserve_original_counter(self):
        from test_runner import LIMITS, sample
        baseline = {'lost': 60925, 'guest_boot_id': 'boot-1'}
        row = sample()
        row['audit']['audit_lost'] = 60925
        self.assertIn('AuditD lost is nonzero', c.legacy.Guard(LIMITS, True).check(copy.deepcopy(row)))
        guard = c.EpochAuditGuard(LIMITS, baseline)
        self.assertEqual(guard.check(row), [])
        self.assertEqual(row['audit']['audit_lost'], 60925)
        later = sample(10)
        later['audit']['audit_lost'] = 60926
        self.assertIn('AuditD new loss since verified qualification epoch', guard.check(later))
        later['audit']['audit_lost'] = 0
        self.assertTrue(any('reset/regressed' in reason for reason in guard.audit_loss_reasons(later['audit'])))
        later['audit'].update(audit_lost=60925, boot_id='another-boot')
        self.assertTrue(any('boot changed' in reason for reason in guard.audit_loss_reasons(later['audit'])))
        self.assertNotIn('appliance rebooted', guard.check(later))

    def test_recovery_requires_same_absolute_epoch_not_zeroing_loss(self):
        row = healthy()
        row['audit'].update(audit_lost=60925, boot_id='boot')
        baseline = {'lost': 60925, 'guest_boot_id': 'boot'}
        self.assertFalse(c.normalized(row, row))
        self.assertTrue(c.normalized(row, row, baseline))
        changed = copy.deepcopy(row)
        changed['audit']['audit_lost'] += 1
        self.assertFalse(c.normalized(changed, row, baseline))


class ExecutionTests(unittest.TestCase):
    def run_case(self, tmp, fail_at=None, child_code=0, fail_spawn=False):
        clock = Clock()
        monitor = Monitor(Path(tmp), clock, fail_at)
        processes = []
        def popen(argv, stdout, stderr):
            if fail_spawn and processes:
                raise OSError('second exec failed')
            stdout.write('{"raw":"preserved"}\n')
            stderr.write('diagnostic\n')
            child = Process(clock, finish=1 if not processes else 100,
                            code=child_code if not processes else 0)
            processes.append(child)
            return child
        report = {'components': [], 'gaps': []}
        try:
            c.execute_components(monitor, window(mixed=True), {'target': '10.77.20.10'}, 'measure', 300,
                report, popen=popen, monotonic=clock.time, sleep=clock.sleep)
        except (c.legacy.Abort, OSError) as error:
            return monitor, processes, report, error
        return monitor, processes, report, None

    def test_guard_stops_all_jobs_and_keeps_failed_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, processes, report, error = self.run_case(tmp, fail_at=2)
            self.assertIsInstance(error, c.legacy.WindowStop)
            self.assertTrue(all(p.terminated for p in processes))
            self.assertEqual(monitor.backend.endpoint.call_count, 3)
            self.assertEqual(len(json.loads((Path(tmp) / 'receipt.json').read_text())['components']), 3)
            self.assertTrue(all((Path(tmp) / row['path']).read_text() for row in report['components']))

    def test_failed_component_stops_still_running_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, processes, report, error = self.run_case(tmp, child_code=7)
            self.assertIsInstance(error, c.Blocked)
            self.assertFalse(processes[0].terminated)
            self.assertTrue(all(p.terminated for p in processes[1:]))
            self.assertEqual(report['components'][0]['exit_code'], 7)
            # Local exec exit does not prove its remote process group stopped.
            self.assertEqual(monitor.backend.endpoint.call_count, 3)

    def test_partial_launch_failure_does_not_orphan_first_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            monitor, processes, report, error = self.run_case(tmp, fail_spawn=True)
            self.assertIsInstance(error, OSError)
            self.assertTrue(processes[0].terminated)
            self.assertEqual(len(report['components']), 2)
            self.assertIsNone(report['components'][1]['exit_code'])

    def test_recovery_waits_minimum_and_ignores_cpu_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            clock = Clock()
            monitor = Monitor(Path(tmp), clock)
            baseline = healthy()
            baseline['cpu_pct'] = 100
            result = c.recover(monitor, baseline, 120, 600, monotonic=clock.time)
            self.assertGreaterEqual(result['seconds'], 120)
            self.assertEqual(result['status'], 'RECOVERED')
            current = healthy()
            current['audit']['audit_lost'] = 1
            self.assertFalse(c.normalized(current, baseline))

    def test_malformed_after_inventory_still_writes_terminal_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = Mock()
            backend.extended_guest.side_effect = KeyError('malformed collector schema')
            selected = dict(window(), measure_seconds=300, workload='short-tcp')
            with patch.object(c, 'validate_campaign_artifacts', side_effect=c.Blocked('missing receipt')):
                report = c.execute_window({}, selected, {'sample_interval': 10}, backend, {}, [],
                                           Path(tmp), Path(tmp) / 'health.json', Path(tmp) / 'isolation.json', Path(tmp))
            self.assertEqual(report['outcome'], 'INVALID_MEASUREMENT')
            self.assertFalse(report['valid'])
            self.assertTrue((Path(tmp) / 'manifest.json').exists())
            self.assertTrue((Path(tmp) / 'receipts-index.json').exists())

    def test_missing_selected_targets_never_starts_guest_discovery(self):
        from test_measurement import config
        with tempfile.TemporaryDirectory() as tmp:
            backend = Mock()
            selected = dict(window(), measure_seconds=300, workload='short-tcp')
            report = c.execute_window({}, selected, {'sample_interval': 10}, backend, config(), [],
                Path(tmp), Path(tmp) / 'health.json', Path(tmp) / 'isolation.json', Path(tmp))
            self.assertEqual(report['outcome'], 'INVALID_MEASUREMENT')
            self.assertIn('runtime discovery forbidden', report['reason'])
            backend.extended_guest.assert_not_called()


if __name__ == '__main__':
    unittest.main()
