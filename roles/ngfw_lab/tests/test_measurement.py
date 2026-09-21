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
                audit=dict(enabled=1, failure=1, pid=100, lost=0, backlog=0, backlog_limit=8192,
                           rate_limit=0, backlog_wait_time=1, backlog_wait_time_actual=t),
                audit_fs=dict(device='254:1', total_bytes=100000, free_bytes=99999),
                audit_log=dict(inode=1, bytes=100+100*t))


def sample(t=0):
    return dict(monotonic=t, measurement={'host': snapshot(t), 'guest': snapshot(t)},
                service_probe_ms=5, collection_seconds=1)


def busy_baseline():
    return {'boot_id': '12345678-1234-1234-1234-123456789abc', 'observed_seconds': 300,
            'evidence_sha256': 'a' * 64,
            'cores': [{'cpu': 'cpu' + str(cpu), 'pid': 100, 'process_start_ticks': 10,
                       'process_comm': 'pt-ngfw', 'tid': 100 + cpu, 'thread_start_ticks': 20 + cpu,
                       'thread_comm': name, 'affinity': [cpu]} for cpu, name in ((2, 'ngfw2'), (3, 'bal'))]}


def busy_sample(t=0):
    row = sample(t)
    guest = row['measurement']['guest']
    guest['boot_id'] = busy_baseline()['boot_id']
    for side in ('guest', 'host'):
        cpu = row['measurement'][side]['cpu']
        for index in range(1, 4):
            cpu['cpu' + str(index)] = cpu['cpu0'].copy()
    guest['busy_poll_threads'] = {'values': {}, 'unavailable': []}
    for expected in busy_baseline()['cores']:
        guest['cpu'][expected['cpu']]['idle'] = 100
        guest['busy_poll_threads']['values'][f"{expected['pid']}:{expected['tid']}"] = {
            key: value for key, value in expected.items() if key != 'cpu'} | {
                'process_state': 'S', 'thread_state': 'R', 'user_ticks': 100 + 100 * t, 'system_ticks': 10}
    return row


class Probe(unittest.TestCase):
    def test_auditd_version_uses_installed_package_not_daemon_cli(self):
        with patch.object(probe, 'command', return_value='auditd\n1:3.0.9-1\ninstall ok installed\n') as call:
            package = probe.auditd_package()
        self.assertEqual(package, {'name': 'auditd', 'version': '1:3.0.9-1',
                                   'status': 'install ok installed', 'origin': 'dpkg-query'})
        self.assertEqual(call.call_args.args[0][0], 'dpkg-query')
        for raw in ('auditd\n1:3.0.9-1\ndeinstall ok config-files\n', 'garbage',
                    'other\n1:3.0.9-1\ninstall ok installed\n'):
            with patch.object(probe, 'command', return_value=raw), self.assertRaises(ValueError):
                probe.auditd_package()

    def test_inventory_version_unavailable_is_a_gap_not_an_exception(self):
        with patch.object(probe, 'command', side_effect=FileNotFoundError), \
                patch.object(probe, 'text', side_effect=FileNotFoundError), \
                patch.object(probe.AUDIT_CONF.__class__, 'read_bytes', side_effect=FileNotFoundError):
            row = probe.inventory()
        self.assertIsNone(row['auditd_version'])
        self.assertIsNone(row['auditd_package'])
        self.assertIn('auditd_version', row['gaps'])
        self.assertEqual(row['effective_defaults'], {})

    def test_selected_threads_read_only_exact_targets_and_detect_identity_race(self):
        process = dict(comm='pt-ngfw', state='S', start_ticks=10, user_ticks=100, system_ticks=10)
        thread = dict(comm='ngfw2', state='R', start_ticks=22, user_ticks=100, system_ticks=10)
        with patch.object(probe, 'task_stat', side_effect=[process, thread, process, thread]) as read, \
                patch.object(probe.os, 'sched_getaffinity', return_value={2}, create=True) as affinity:
            rows = probe.selected_thread_counters([{'pid': 100, 'tid': 102}])
        self.assertEqual(rows['unavailable'], [])
        self.assertEqual(rows['values']['100:102']['affinity'], [2])
        self.assertEqual(rows['values']['100:102']['thread_start_ticks'], 22)
        self.assertEqual(read.call_count, 4)
        self.assertTrue(all(args.args[0].as_posix().endswith(('100/stat', '100/task/102/stat'))
                            for args in read.call_args_list))
        affinity.assert_called_once_with(102)
        with patch.object(probe, 'task_stat', side_effect=[process, thread, process, thread | {'start_ticks': 99}]), \
                patch.object(probe.os, 'sched_getaffinity', return_value={2}, create=True):
            rows = probe.selected_thread_counters([{'pid': 100, 'tid': 102}])
        self.assertEqual(rows, {'values': {}, 'unavailable': ['100:102']})
        with patch.object(probe, 'task_stat', side_effect=FileNotFoundError):
            self.assertEqual(probe.selected_thread_counters([{'pid': 100, 'tid': 102}])['unavailable'], ['100:102'])
        for targets in ([], [{'pid': '../1', 'tid': 1}], [{'pid': True, 'tid': 1}],
                        [{'pid': 1, 'tid': 1}] * 17):
            with self.assertRaises(ValueError):
                probe.selected_thread_counters(targets)

    @unittest.skipUnless(sys.platform.startswith('linux'), 'actual task stat/affinity needs Linux CI')
    def test_real_selected_current_process_thread(self):
        pid = os.getpid()
        rows = probe.selected_thread_counters([{'pid': pid, 'tid': pid}])
        self.assertEqual(rows['unavailable'], [])
        current = rows['values'][f'{pid}:{pid}']
        self.assertEqual(current['affinity'], sorted(os.sched_getaffinity(pid)))
        self.assertEqual(current['process_start_ticks'], current['thread_start_ticks'])

    def test_source_with_busy_baseline_is_standalone_and_exact_targeted(self):
        rendered = m.source(['auditd', 'pt-ngfw'], True, busy_baseline())
        compile(rendered, '<busy-probe>', 'exec')
        self.assertIn("'thread_targets': [{'pid': 100, 'tid': 102}, {'pid': 100, 'tid': 103}]", rendered)
        default_options = next(line for line in m.source().splitlines() if line.startswith('OPTIONS = '))
        self.assertNotIn('thread_targets', default_options)

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
    def test_fixed_control_changes_ignore_only_dynamic_counters(self):
        before, after = snapshot(), snapshot(5)
        after['audit'].update(backlog=42, lost=1, backlog_wait_time_actual=10)
        self.assertEqual(m.control_changes(before, after), [])
        for field in m.AUDIT_FIXED_CONTROLS:
            changed = copy.deepcopy(after)
            changed['audit'][field] = before['audit'][field] + 1
            self.assertEqual(m.control_changes(before, changed), ['fixed AuditD control changed: ' + field])
            del changed['audit'][field]
            self.assertEqual(m.control_changes(before, changed), ['fixed AuditD control unavailable: ' + field])

    def test_fixed_control_completeness_is_required_for_inventory_and_first_sample(self):
        for field in m.AUDIT_FIXED_CONTROLS:
            row = sample()
            del row['measurement']['guest']['audit'][field]
            reason = 'fixed AuditD control unavailable: ' + field
            self.assertIn(reason, m.inventory_risks(row['measurement']['guest']))
            self.assertIn(reason, m.Guard(config()).check(row, initial=True))

    def test_control_drift_and_daemon_restart_stop_including_safe_failure_transitions(self):
        for field, value in (('enabled', 0), ('failure', 0), ('pid', 101), ('rate_limit', 10),
                             ('backlog_limit', 4096), ('backlog_wait_time', 0)):
            guard = m.Guard(config())
            self.assertFalse(guard.check(sample(0)))
            row = sample(5)
            row['measurement']['guest']['audit'][field] = value
            self.assertIn('fixed AuditD control changed: ' + field, guard.check(row))
        guard = m.Guard(config())
        first = sample(0)
        first['measurement']['guest']['audit']['failure'] = 0
        self.assertFalse(guard.check(first))
        self.assertIn('fixed AuditD control changed: failure', guard.check(sample(5)))

    def test_initial_inventory_pins_first_sample_controls_and_boot(self):
        inventory = snapshot(0)
        first = sample(5)
        first['measurement']['guest']['audit']['failure'] = 0
        self.assertIn('fixed AuditD control changed: failure',
                      m.Guard(config(), initial_guest=inventory).check(first, initial=True))
        first = sample(5)
        first['measurement']['guest']['boot_id'] = 'new-boot'
        self.assertIn('guest boot changed', m.Guard(config(), initial_guest=inventory).check(first, initial=True))
        self.assertFalse(m.Guard(config(), initial_guest=inventory).check(sample(5), initial=True))

    def test_busy_baseline_strict_schema_and_bounds(self):
        approved = config() | {'guest_busy_poll_baseline': busy_baseline()}
        self.assertEqual(m.validate(approved), approved)
        invalid = [None, {}, busy_baseline() | {'observed_seconds': 59},
                   busy_baseline() | {'observed_seconds': float('nan')},
                   busy_baseline() | {'evidence_sha256': 'unknown'}, busy_baseline() | {'boot_id': 'other'},
                   busy_baseline() | {'cores': []}, busy_baseline() | {'extra': True}]
        for field, value in (('cpu', 'cpu512'), ('pid', 0), ('tid', True), ('process_start_ticks', -1),
                             ('thread_start_ticks', 0), ('process_comm', 'a' * 16), ('thread_comm', '../bad'),
                             ('affinity', [2, 3]), ('affinity', [1]), ('extra', True)):
            modified = busy_baseline()
            modified['cores'][0][field] = value
            invalid.append(modified)
        duplicate = busy_baseline()
        duplicate['cores'][1] = copy.deepcopy(duplicate['cores'][0])
        invalid.append(duplicate)
        for value in invalid:
            with self.subTest(baseline=value), self.assertRaises(ValueError):
                m.validate(config() | {'guest_busy_poll_baseline': value})

    def test_busy_poll_only_skips_verified_guest_absolute_core_check(self):
        guard = m.Guard(config() | {'guest_busy_poll_baseline': busy_baseline()})
        for t in (0, 5, 15):
            row = busy_sample(t)
            self.assertFalse(guard.check(row, initial=(t == 0)))
        guest = row['measurement']['guest']
        self.assertEqual(guest['derived']['cpu']['cpu2']['busy_pct'], 100)
        self.assertEqual(guest['derived']['cpu']['cpu3']['busy_pct'], 100)
        self.assertEqual(guest['busy_poll_baseline']['verified_cores'], ['cpu2', 'cpu3'])
        # No baseline means exactly the old absolute per-core STOP behavior.
        original = m.Guard(config())
        for t in (0, 5, 15):
            reasons = original.check(busy_sample(t))
        self.assertTrue(any('sustained single-core CPU: cpu2' in risk for risk in reasons))

    def test_busy_poll_never_exempts_host_or_guest_management_cores(self):
        for side, core in (('guest', 'cpu0'), ('guest', 'cpu1'), ('host', 'cpu2')):
            guard = m.Guard(config() | {'guest_busy_poll_baseline': busy_baseline()})
            for t in (0, 5, 15):
                row = busy_sample(t)
                row['measurement'][side]['cpu'][core]['idle'] = 100
                reasons = guard.check(row)
            self.assertIn(side + ' sustained single-core CPU: ' + core, reasons)

    def test_busy_poll_task_changes_fail_closed_every_sample(self):
        changes = [('boot_id', 'other')]
        for field, value in (('pid', 200), ('tid', 202), ('process_start_ticks', 11),
                             ('thread_start_ticks', 23), ('process_comm', 'other'), ('thread_comm', 'other'),
                             ('affinity', [1]), ('affinity', [2, 3]), ('thread_state', 'T')):
            changes.append((field, value))
        for field, value in changes:
            guard = m.Guard(config() | {'guest_busy_poll_baseline': busy_baseline()})
            self.assertFalse(guard.check(busy_sample(0)))
            row = busy_sample(5)
            guest = row['measurement']['guest']
            if field == 'boot_id':
                guest[field] = value
            else:
                guest['busy_poll_threads']['values']['100:102'][field] = value
            self.assertTrue(any('busy-poll' in risk for risk in guard.check(row)))
            self.assertEqual(guest['busy_poll_baseline']['verified_cores'], [])
        for missing in ('busy_poll_threads', '100:102'):
            row = busy_sample()
            guest = row['measurement']['guest']
            if missing == 'busy_poll_threads':
                del guest[missing]
            else:
                del guest['busy_poll_threads']['values'][missing]
            self.assertTrue(m.Guard(config() | {'guest_busy_poll_baseline': busy_baseline()}).check(row))

    def test_busy_poll_unknown_cpu_and_all_guest_cores_rejected_before_load(self):
        unknown = busy_baseline()
        unknown['cores'][0].update(cpu='cpu9', affinity=[9])
        self.assertTrue(any('unavailable CPU' in risk for risk in
                            m.Guard(config() | {'guest_busy_poll_baseline': unknown}).check(busy_sample(), initial=True)))
        row = busy_sample()
        del row['measurement']['guest']['cpu']['cpu0']
        del row['measurement']['guest']['cpu']['cpu1']
        self.assertTrue(any('at least one guest core' in risk for risk in
                            m.Guard(config() | {'guest_busy_poll_baseline': busy_baseline()}).check(row, initial=True)))

    def test_busy_poll_does_not_bypass_temperature_deadline_or_disk_checks(self):
        guard_config = config() | {'guest_busy_poll_baseline': busy_baseline()}
        row = busy_sample()
        row['measurement']['host']['temperature_c']['cpu/package'] = 61
        row['collection_seconds'] = 31
        row['measurement']['guest']['audit_fs']['device'] = '9:9'
        reasons = m.Guard(guard_config).check(row, initial=True)
        self.assertIn('host temperature threshold: cpu/package', reasons)
        self.assertIn('measurement collection deadline exceeded', reasons)
        self.assertIn('selected guest disks do not include audit filesystem device', reasons)

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

    def test_current_edr_failure_zero_or_one_is_allowed_but_panic_or_missing_is_not(self):
        for value in (0, 1):
            row = sample()
            row['measurement']['guest']['audit']['failure'] = value
            self.assertFalse(m.Guard(config()).check(row, initial=True))
        for value in (2, None):
            row = sample()
            row['measurement']['guest']['audit']['failure'] = value
            self.assertTrue(any('controls unavailable or unsafe' in risk
                                for risk in m.Guard(config()).check(row, initial=True)))
        row = sample()
        del row['measurement']['guest']['audit']['failure']
        self.assertTrue(any('controls unavailable or unsafe' in risk
                            for risk in m.Guard(config()).check(row, initial=True)))

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

    def test_exact_reviewed_default_preserves_absent_configured_value(self):
        row = snapshot()
        cfg = {k: 'syslog' for k in ('space_left_action', 'admin_space_left_action',
               'max_log_file_action', 'disk_full_action', 'disk_error_action')}
        package = {'name': 'auditd', 'version': '1:3.0.9-1',
                   'status': 'install ok installed', 'origin': 'dpkg-query'}
        row['inventory'] = dict(rules_sha256='a', auditd_conf_sha256='b', auditd_active=True,
                                dataplane_active=True, auditd_conf=cfg, auditd_package=package,
                                effective_defaults=probe.effective_defaults(cfg, package))
        self.assertNotIn('overflow_action', cfg)
        self.assertFalse(m.inventory_risks(row))
        self.assertEqual(row['inventory']['effective_defaults']['overflow_action']['origin'],
                         'verified-package-default')
        # A default never overrides an explicitly configured unsafe value.
        cfg['overflow_action'] = 'halt'
        self.assertTrue(any('overflow_action' in risk for risk in m.inventory_risks(row)))
        self.assertEqual(probe.effective_defaults(cfg, package), {})

    def test_unknown_versions_and_unverified_default_claims_stay_blocked(self):
        cfg = {k: 'syslog' for k in ('space_left_action', 'admin_space_left_action',
               'max_log_file_action', 'disk_full_action', 'disk_error_action')}
        package = {'name': 'auditd', 'version': '1:3.0.9-1',
                   'status': 'install ok installed', 'origin': 'dpkg-query'}
        known = probe.effective_defaults(cfg, package)
        for version in ('1:3.0.9-2', '3.0.9', '1:3.0.9-1+vendor1', '1:4.0.2-2'):
            changed = package | {'version': version}
            self.assertEqual(probe.effective_defaults(cfg, changed), {})
            row = snapshot()
            row['inventory'] = dict(rules_sha256='a', auditd_conf_sha256='b', auditd_active=True,
                                    dataplane_active=True, auditd_conf=cfg, auditd_package=changed,
                                    effective_defaults=known)
            self.assertTrue(any('overflow_action' in risk for risk in m.inventory_risks(row)))
        row['inventory']['auditd_package'] = package
        for defaults in ({}, {'overflow_action': {'value': 'syslog'}}):
            row['inventory']['effective_defaults'] = defaults
            self.assertTrue(any('overflow_action' in risk for risk in m.inventory_risks(row)))
        row['inventory']['effective_defaults'] = known
        for value in (0, 1):
            row['audit']['failure'] = value
            self.assertFalse(m.inventory_risks(row))
        for value in (2, None):
            row['audit']['failure'] = value
            self.assertTrue(any('failure=0 or 1' in risk for risk in m.inventory_risks(row)))
        del row['audit']['failure']
        self.assertTrue(any('failure=0 or 1' in risk for risk in m.inventory_risks(row)))
        row['audit']['failure'] = 0
        row['audit']['lost'] = 1
        self.assertTrue(any('lost=0' in risk for risk in m.inventory_risks(row)))

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
