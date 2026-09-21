import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import measure_probe as probe
import measurement as m


def config():
    return dict(schema_version=1, process_names=['auditd'], host_temperature_keys=['cpu/package'],
                guest_disk_devices=['vda1'], host_disk_devices=[], temperature_start_c=60,
                temperature_stop_c=80, single_core_pct=90, single_core_seconds=10,
                disk_await_ms=50, disk_await_seconds=10, service_probe_ms=1000,
                service_probe_seconds=10, max_sample_seconds=15)


def snapshot(t=0):
    cpu = dict(zip(probe.CPU_FIELDS, [100 + 20*t, 0, 30, 100 + 80*t, 0, 0, 0, 0]))
    disk = dict(major_minor='254:1', reads=10+t, writes=20+t, read_ms=100+10*t,
                write_ms=200+20*t, read_sectors=10+2*t, write_sectors=20+2*t,
                io_ms=300+30*t, weighted_ms=500+50*t, in_flight=0)
    return dict(monotonic=t, boot_id='boot', cpu={'cpu': cpu, 'cpu0': cpu.copy()},
                disks={'vda1': disk}, temperature_c={'cpu/package': 45},
                audit=dict(enabled=1, failure=1, pid=100, lost=0, backlog=0, backlog_limit=8192, backlog_wait_time_actual=t),
                audit_fs=dict(device='254:1', total_bytes=100000, free_bytes=99999),
                audit_log=dict(inode=1, bytes=100+100*t))


def sample(t=0):
    return dict(monotonic=t, measurement={'host': snapshot(t), 'guest': snapshot(t)},
                service_probe_ms=5, collection_seconds=1)


class Probe(unittest.TestCase):
    def test_cpu_excludes_guest_double_count(self):
        self.assertEqual(sum(probe.cpu_counters('cpu 1 2 3 4 5 6 7 8 99 99')['cpu'].values()), 36)

    def test_disk_fields_and_audit_allowlist(self):
        disk = probe.disk_counters('254 1 vda1 10 0 20 30 40 0 50 60 0 70 80')['vda1']
        self.assertEqual((disk['reads'], disk['write_ms'], disk['weighted_ms']), (10, 60, 80))
        self.assertEqual(probe.status('enabled 1\nbacklog_wait_time_actual 9\nSECRET password\nlost abc'),
                         {'enabled': 1, 'backlog_wait_time_actual': 9})

    def test_config_does_not_export_secrets_or_exec_args(self):
        value = probe.config_summary('disk_full_action = exec /SECRET/script\naction_mail_acct = SECRET\nflush = INCREMENTAL_ASYNC\nlog_file = /private/path')
        self.assertNotIn('SECRET', json.dumps(value))
        self.assertEqual(value['disk_full_action'], 'REDACTED')
        self.assertFalse(value['standard_log_path'])

    def test_source_is_standalone_valid_python(self):
        compile(m.source(['auditd'], True), '<probe>', 'exec')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual proc/sys needs Linux CI')
    def test_real_read_only_host_snapshot(self):
        row = probe.snapshot({'host_only': True})
        self.assertTrue(row['cpu']['cpu'])
        self.assertTrue(row['boot_id'])
        self.assertNotIn('audit', row)
        self.assertNotIn('inventory', row)


class Measurements(unittest.TestCase):
    def test_derived_cpu_disk_log_units(self):
        values = m.derive(snapshot(0), snapshot(10))
        self.assertAlmostEqual(values['cpu']['cpu']['busy_pct'], 20)
        self.assertEqual(values['disks']['vda1']['await_ms'], 15)
        self.assertEqual(values['disks']['vda1']['write_bytes_s'], 1024)
        self.assertEqual(values['log_bytes_per_second'], 100)
        self.assertEqual(values['audit_wait_delta'], 10)

    def test_rotation_reset_and_boot_are_gaps(self):
        current = snapshot(10)
        current['audit_log']['inode'] = 2
        self.assertIsNone(m.derive(snapshot(), current)['log_bytes_per_second'])
        current['cpu']['cpu']['user'] = 0
        self.assertNotIn('cpu', m.derive(snapshot(), current)['cpu'])
        current['boot_id'] = 'other'
        self.assertFalse(m.derive(snapshot(), current)['cpu'])

    def test_optional_missing_not_zero(self):
        row = snapshot(5)
        row['audit'].pop('backlog_wait_time_actual')
        self.assertIsNone(m.derive(snapshot(), row)['audit_wait_delta'])
        self.assertNotIn('cpu_total', m.legacy_guest({'cpu': None}))

    def test_missing_sensor_and_wrong_disk_block(self):
        guard = m.Guard(config())
        row = sample()
        row['measurement']['host']['temperature_c'] = {}
        row['measurement']['guest']['audit_fs']['device'] = '0:1'
        self.assertEqual(len(guard.check(row, initial=True)), 2)

    def test_temperature_start_and_stop(self):
        row = sample()
        row['measurement']['host']['temperature_c']['cpu/package'] = 65
        self.assertTrue(m.Guard(config()).check(row, initial=True))
        self.assertFalse(m.Guard(config()).check(row))

    def test_single_core_pressure_held_and_reset(self):
        guard = m.Guard(config())
        self.assertFalse(guard.check(sample()))
        for t in (5, 15):
            row = sample(t)
            row['measurement']['guest']['cpu']['cpu0']['idle'] = 100
            result = guard.check(row)
            self.assertEqual(any('single-core' in r for r in result), t == 15)

    def test_missing_daemon_and_deadline_stop(self):
        row = sample()
        row['measurement']['guest']['audit']['pid'] = 0
        row['collection_seconds'] = 31
        self.assertEqual(len(m.Guard(config()).check(row)), 2)

    def test_unknown_threshold_or_nan_rejected(self):
        for change in ({'temperature_stop_c': None}, {'single_core_pct': float('nan')}, {'x': 1}):
            with self.assertRaises(ValueError):
                m.validate(config() | change)

    def test_config_only_fail_closed_and_missing_block(self):
        row = snapshot()
        row['inventory'] = dict(rules_sha256='a', auditd_conf_sha256='b', auditd_active=True,
                                dataplane_active=True, auditd_conf={k: 'syslog' for k in
                                ('space_left_action', 'admin_space_left_action', 'max_log_file_action',
                                 'disk_full_action', 'disk_error_action', 'overflow_action')})
        self.assertFalse(m.inventory_risks(row))
        row['inventory']['auditd_conf']['disk_full_action'] = 'halt'
        self.assertTrue(m.inventory_risks(row))

    def test_context_exact_and_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'context.json'
            value = dict(campaign_id='demo', test_id='T10', comparison_id='pair', workload_id='tcp', profile_id='C-NET')
            path.write_text(json.dumps(value), encoding='utf-8')
            self.assertEqual(m.load_context(path), value)
            self.assertEqual(m.manifest(tmp)['files'][0]['file'], 'context.json')
            value['test_id'] = 'T00-R'
            path.write_text(json.dumps(value), encoding='utf-8')
            with self.assertRaises(ValueError):
                m.load_context(path)


if __name__ == '__main__':
    unittest.main()
