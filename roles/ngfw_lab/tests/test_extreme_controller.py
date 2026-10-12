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
    def fixture(self, directory):
        raw = {'database': 'lab', 'guest_boot_id': 'boot', 'partial': False,
               'points': [{'measurement': 'native_dp', 'time': timestamp(990)},
                          {'measurement': 'net', 'time': timestamp(995)}]}
        ref = file_ref(directory, 'query.json', raw)
        receipt = {'database': 'lab', 'guest_boot_id': 'boot', 'collected_at': timestamp(999),
                   'source_interval_seconds': 10, 'required_measurements': ['native_dp', 'net'],
                   'query_evidence': ref}
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
            receipt['query_evidence'] = file_ref(directory, 'query.json', raw)
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


if __name__ == '__main__':
    unittest.main()
