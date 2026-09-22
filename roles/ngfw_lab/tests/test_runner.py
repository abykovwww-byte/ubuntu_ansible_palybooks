import copy
import csv
import importlib.util
import io
import json
from pathlib import Path
import socket
import socketserver
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'files'))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


r = load("runner", ROOT / "files/runner.py")
sys.path.insert(0, str(ROOT / "files/traffic"))
e = load("endpoint", ROOT / "files/traffic/endpoint.py")
LIMITS = dict(backlog_pct=75, backlog_seconds=30, disk_used_pct=80, cpu_pct=90,
              cpu_seconds=60, memory_used_pct=90, memory_seconds=60,
              log_projection_hours=24, udp_loss_pct=.5, tcp_drop_pct=20)


def config():
    return dict(duration=1, warmup=1, idle=0, repetitions=3, sample_interval=2,
                project_dir="/test", reports_dir="/test", target="10.77.20.10",
                management_address="10.77.0.20", mngt_address="10.77.0.10", management_port=22, mngt_port=443,
                ngfw_vm="ngfw", mngt_vm="mngt", limits=copy.deepcopy(LIMITS),
                scenarios=[dict(name="tcp", kind="tcp", parallel=4, mbps=100)])


def sample(now=0):
    return dict(monotonic=now, time="test", unavailable=[], dataplane_ok=True, management_ok=True,
                mngt_ok=True, vms_ok=True, host=dict(memory_used_pct=30, disk_used_pct=20),
                audit=dict(boot_id="boot-1", audit_lost=0, audit_backlog=0, audit_backlog_limit=8192,
                           cpu_total=100 + now * 100, cpu_idle=90 + now * 90, root_used_pct=20,
                           audit_disk_used_pct=20, memory_used_pct=40, log_bytes=10,
                           log_inode=1, event_serial=1, audit_free_bytes=1e9))


class Guards(unittest.TestCase):
    def test_cpu_action_defaults_to_stop_and_rejects_unknown_values(self):
        self.assertEqual(r.Guard(LIMITS, True).cpu_action, 'stop')
        for action in ('stop', 'warn'):
            self.assertEqual(r.Guard(LIMITS, True, action).cpu_action, action)
        for action in (None, True, '', 'ignore', 'WARN', [], {}):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, 'cpu_action'):
                r.Guard(LIMITS, True, action)

    def test_warn_total_cpu_records_observation_and_resets_hold_on_recovery(self):
        guard = r.Guard(LIMITS, True, cpu_action='warn')
        for now in (0, 5, 65, 70, 75, 134, 135):
            row = sample(now)
            row['audit']['cpu_idle'] = 90 if now < 70 else 590
            self.assertEqual(guard.check(row), [])
            self.assertEqual(row.get('warnings', []),
                             ['sustained appliance CPU'] if now in (65, 135) else [], now)
            if now:
                self.assertEqual(row['audit']['cpu_pct'], 0 if now == 70 else 100)

    def test_warn_preserves_access_lost_disk_and_missing_audit_stops(self):
        mutations = [(key + ' failed', lambda row, key=key: row.update({key: False}))
                     for key in ('dataplane_ok', 'management_ok', 'mngt_ok', 'vms_ok')]
        mutations += [
            ('AuditD lost is nonzero', lambda row: row['audit'].update(audit_lost=1)),
            ('appliance filesystem full threshold', lambda row: row['audit'].update(audit_disk_used_pct=80)),
            ('host report filesystem full threshold', lambda row: row['host'].update(disk_used_pct=80)),
            ('host metrics unavailable', lambda row: row.pop('host')),
            ('AuditD probe incomplete or unavailable', lambda row: row.pop('audit')),
        ]
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                row = sample()
                mutate(row)
                self.assertIn(expected, r.Guard(LIMITS, True, cpu_action='warn').check(row))
                self.assertFalse(row.get('warnings'))

    def test_warn_preserves_sustained_backlog_memory_and_boot_stops(self):
        guard = r.Guard(LIMITS, True, cpu_action='warn')
        for now in (0, 5, 65):
            row = sample(now)
            row['audit'].update(cpu_idle=90, audit_backlog=7000, memory_used_pct=90)
            row['host']['memory_used_pct'] = 90
            reasons = guard.check(row)
        self.assertEqual(set(reasons), {'sustained AuditD backlog', 'sustained host memory pressure',
                                        'sustained appliance memory pressure'})
        self.assertEqual(row['warnings'], ['sustained appliance CPU'])
        row = sample(70)
        row['audit']['boot_id'] = 'new-boot'
        self.assertIn('appliance rebooted', guard.check(row))

    def test_extended_guest_preserves_selected_baseline_in_source(self):
        selected = {'explicit': 'baseline is validated by measurement.source'}
        backend = r.Backend(config(), measurement_argv=['fixed-probe'],
                            measurement_config={'process_names': ['auditd', 'pt-ngfw'],
                                                'guest_busy_poll_baseline': selected})
        try:
            with patch.object(r.measurement, 'source', return_value='PINNED_SOURCE') as source, \
                    patch.object(backend, 'call', return_value='{"schema_version":1}') as call:
                self.assertEqual(backend.extended_guest(inventory=True), {'schema_version': 1})
            source.assert_called_once_with(['auditd', 'pt-ngfw'], True, selected, None, None)
            call.assert_called_once_with(['fixed-probe'], timeout=12, input_text='PINNED_SOURCE')
        finally:
            backend.pool.shutdown(wait=True)

    def test_extended_guest_forwards_exact_process_targets_but_host_keeps_discovery(self):
        targets = [{'pid': 585, 'start_ticks': 400, 'comm': 'auditd'},
                   {'pid': 686, 'start_ticks': 500, 'comm': 'pt-ngfw'}]
        selected = {'explicit': 'validated by measurement.source'}
        names = ['auditd', 'pt-ngfw']
        boot = '12345678-1234-1234-1234-123456789abc'
        backend = r.Backend(config(), measurement_argv=['fixed-probe'], measurement_config={
            'process_names': names, 'guest_busy_poll_baseline': selected, 'guest_process_targets': targets,
            'guest_process_boot_id': boot})
        try:
            for inventory in (False, True):
                with self.subTest(inventory=inventory), \
                        patch.object(r.measurement, 'source', return_value='PINNED_SOURCE') as source, \
                        patch.object(backend, 'call', return_value='{"schema_version":1}') as call:
                    self.assertEqual(backend.extended_guest(inventory=inventory), {'schema_version': 1})
                source.assert_called_once_with(names, inventory, selected, targets, boot)
                self.assertIs(source.call_args.args[3], targets)
                call.assert_called_once_with(['fixed-probe'], timeout=12, input_text='PINNED_SOURCE')
            with patch.object(r.measure_probe, 'snapshot', return_value={'host': 'snapshot'}) as snapshot:
                self.assertEqual(backend.extended_host(), {'host': 'snapshot'})
            snapshot.assert_called_once_with({'host_only': True, 'process_names': names})
        finally:
            backend.pool.shutdown(wait=True)

    def test_no_audit_cannot_pass_as_observed(self):
        s = sample()
        s.pop("audit")
        self.assertTrue(r.Guard(LIMITS, True).check(s))
        self.assertEqual(r.Guard(LIMITS, False).check(s), [])

    def test_lost_and_reboot(self):
        guard = r.Guard(LIMITS, True)
        self.assertEqual(guard.check(sample()), [])
        s = sample(5)
        s["audit"].update(audit_lost=1, boot_id="boot-2")
        reasons = guard.check(s)
        self.assertIn("AuditD lost is nonzero", reasons)
        self.assertIn("appliance rebooted", reasons)

    def test_sustained_backlog_resets(self):
        guard = r.Guard(LIMITS, True)
        for now in (0, 10, 29):
            s = sample(now)
            s["audit"]["audit_backlog"] = 7000
            self.assertEqual(guard.check(s), [])
        self.assertEqual(guard.check(sample(30)), [])
        for now in (31, 60, 61):
            s = sample(now)
            s["audit"]["audit_backlog"] = 7000
            self.assertEqual(bool(guard.check(s)), now == 61)

    def test_cpu_pressure_and_rotation_gap(self):
        guard = r.Guard(LIMITS, True)
        guard.check(sample())
        for now in (5, 65):
            s = sample(now)
            s["audit"].update(cpu_idle=90, log_inode=2, log_bytes=0)
            reasons = guard.check(s)
            if now == 5:
                self.assertIn("log_rate_gap", s["audit"])
            else:
                self.assertIn("sustained appliance CPU", reasons)

    def test_disk_and_management_abort_immediately(self):
        s = sample()
        s["management_ok"] = False
        s["audit"]["audit_disk_used_pct"] = 80
        reasons = r.Guard(LIMITS, True).check(s)
        self.assertIn("management_ok failed", reasons)
        self.assertIn("appliance filesystem full threshold", reasons)

    def test_probe_rejects_nonfinite_and_missing_numbers(self):
        self.assertEqual(r.parse_probe("audit_lost=nan\nother=oops\nboot_id=a\naudit_backlog=12"),
                         {"boot_id": "a", "audit_backlog": 12})


class Planning(unittest.TestCase):
    def test_warn_cli_requires_extended_transport_and_disallows_traffic_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg_path, measurement_path = root / 'runner.json', root / 'measurement.json'
            cfg_path.write_text(json.dumps(config()), encoding='utf-8')
            measurement_path.write_text('{}', encoding='utf-8')
            for extra in ([], ['--traffic-only'],
                          ['--traffic-only', '--measurement-argv-file', str(root / 'private-argv.json')]):
                argv = ['runner.py', 'run', '--config', str(cfg_path),
                        '--measurement-config', str(measurement_path)] + extra
                with self.subTest(extra=extra), patch.object(sys, 'argv', argv), \
                        patch.object(r.measurement, 'validate', return_value={'cpu_action': 'warn'}), \
                        patch.object(r, 'Backend') as backend, patch.object(sys, 'stderr', new_callable=io.StringIO) as error:
                    with self.assertRaises(SystemExit) as raised:
                        r.main()
                    self.assertEqual(raised.exception.code, 2)
                    self.assertIn('cpu_action=warn requires extended measurements and cannot use traffic-only', error.getvalue())
                    backend.assert_not_called()

    def test_main_wires_same_cpu_policy_to_both_guards_and_pinned_inventory(self):
        # Exercise the real CLI orchestration without Linux operations, subprocesses or traffic.
        for cpu_action in (None, 'stop', 'warn'):
            with self.subTest(cpu_action=cpu_action), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg = config() | {'reports_dir': str(root)}
                cfg_path, measurement_path, argv_path = root / 'runner.json', root / 'measurement.json', root / 'argv.json'
                extended = dict(schema_version=1, process_names=['auditd'], host_temperature_keys=['cpu/package'],
                                guest_disk_devices=['vda1'], host_disk_devices=[], temperature_start_c=60,
                                temperature_stop_c=80, single_core_pct=90, single_core_seconds=10,
                                disk_await_ms=50, disk_await_seconds=10, service_probe_ms=1000,
                                service_probe_seconds=10, max_sample_seconds=15)
                if cpu_action is not None:
                    extended['cpu_action'] = cpu_action
                cfg_path.write_text(json.dumps(cfg), encoding='utf-8')
                measurement_path.write_text(json.dumps(extended), encoding='utf-8')
                argv_path.write_text(json.dumps(['fixed-private-probe', 'python3', '-']), encoding='utf-8')
                (root / 'isolation.json').write_text('{}', encoding='utf-8')
                inventory = {'boot_id': 'boot', 'audit': dict(enabled=1, failure=0, pid=100,
                             rate_limit=0, backlog_limit=8192, backlog_wait_time=1)}
                backend = Mock()
                backend.topology.return_value = {}
                backend.extended_guest.return_value = inventory
                backend.endpoint.return_value = 'iperf3 synthetic'
                argv = ['runner.py', 'run', '--config', str(cfg_path), '--measurement-config',
                        str(measurement_path), '--measurement-argv-file', str(argv_path)]
                fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
                with patch.object(sys, 'argv', argv), patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                        patch.object(r, 'os', SimpleNamespace(name='posix', umask=Mock())), \
                        patch.object(r, 'Backend', return_value=backend), patch.object(r, 'Runner') as runner, \
                        patch.object(r, 'validate_isolation'), patch.object(r.measurement, 'inventory_risks', return_value=[]), \
                        patch.object(r.signal, 'signal'), patch.object(sys, 'stdout', new_callable=io.StringIO):
                    self.assertEqual(r.main(), 0)
                expected = cpu_action or 'stop'
                self.assertEqual(runner.call_args.args[4].cpu_action, expected)
                self.assertEqual(runner.call_args.args[6].config.get('cpu_action', 'stop'), expected)
                self.assertEqual(runner.return_value.extra_guard.config.get('cpu_action', 'stop'), expected)
                self.assertEqual(runner.return_value.extra_guard.previous['guest'], inventory)
                runner.return_value.run.assert_called_once_with()
                saved = json.loads(next(root.glob('*/summary.json')).read_text(encoding='utf-8'))
                self.assertEqual(saved['measurement_config'].get('cpu_action', 'stop'), expected)
                backend.pool.shutdown.assert_called_once_with(wait=True)

    def test_main_final_inventory_invalidates_completed_windows_on_selected_process_change(self):
        # No mutation is present before/during runner.run(); only its final read-back changes.
        boot = '12345678-1234-1234-1234-123456789abc'
        for change in ('unchanged', 'pid-reused', 'pid-disappeared'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                cfg_path, measurement_path, argv_path = root / 'runner.json', root / 'measurement.json', root / 'argv.json'
                extended = dict(schema_version=1, process_names=['auditd'], host_temperature_keys=['cpu/package'],
                                guest_disk_devices=['vda1'], host_disk_devices=[], temperature_start_c=60,
                                temperature_stop_c=80, single_core_pct=90, single_core_seconds=10,
                                disk_await_ms=50, disk_await_seconds=10, service_probe_ms=1000,
                                service_probe_seconds=10, max_sample_seconds=15,
                                guest_process_targets=[{'comm': 'auditd', 'pid': 100, 'start_ticks': 10}],
                                guest_process_boot_id=boot)
                before = dict(boot_id=boot, audit=dict(enabled=1, failure=0, pid=100, lost=0,
                              rate_limit=0, backlog_limit=8192, backlog_wait_time=1),
                              processes=dict(mode='selected', scan_truncated=False, unavailable=[],
                              requested_not_found=[], values={'100': dict(comm='auditd', start_ticks=10, state='S')}),
                              inventory=dict(rules_sha256='a' * 64, auditd_conf_sha256='b' * 64,
                              auditd_active=True, dataplane_active=True, auditd_conf={key: 'syslog' for key in (
                                  'space_left_action', 'admin_space_left_action', 'max_log_file_action',
                                  'disk_full_action', 'disk_error_action', 'overflow_action')}))
                after = copy.deepcopy(before)
                if change == 'pid-reused':
                    after['processes']['values']['100']['start_ticks'] = 11
                elif change == 'pid-disappeared':
                    after['processes'].update(values={}, unavailable=['100'], requested_not_found=['auditd'])
                self.assertEqual(r.measurement.inventory_risks(before, extended), [])
                cfg_path.write_text(json.dumps(config() | {'reports_dir': str(root)}), encoding='utf-8')
                measurement_path.write_text(json.dumps(extended), encoding='utf-8')
                argv_path.write_text(json.dumps(['fixed-private-probe', 'python3', '-']), encoding='utf-8')
                (root / 'isolation.json').write_text('{}', encoding='utf-8')
                backend = Mock()
                backend.topology.return_value = {}
                backend.extended_guest.side_effect = [before, after]
                backend.endpoint.return_value = 'iperf3 synthetic'
                argv = ['runner.py', 'run', '--config', str(cfg_path), '--measurement-config',
                        str(measurement_path), '--measurement-argv-file', str(argv_path)]
                fake_fcntl = SimpleNamespace(flock=Mock(), LOCK_EX=1, LOCK_NB=2)
                with patch.object(sys, 'argv', argv), patch.dict(sys.modules, {'fcntl': fake_fcntl}), \
                        patch.object(r, 'os', SimpleNamespace(name='posix', umask=Mock())), \
                        patch.object(r, 'Backend', return_value=backend), patch.object(r, 'Runner') as runner, \
                        patch.object(r, 'validate_isolation'), patch.object(r.signal, 'signal'), \
                        patch.object(sys, 'stdout', new_callable=io.StringIO):
                    self.assertEqual(r.main(), 0 if change == 'unchanged' else 2)
                runner.return_value.run.assert_called_once_with()
                self.assertEqual(backend.extended_guest.call_count, 2)
                directory = next(root.glob('*/summary.json')).parent
                saved = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['status'], 'completed' if change == 'unchanged' else 'aborted')
                if change != 'unchanged':
                    self.assertIn('guest selected process identity/state changed: 100', saved['gaps'])
                self.assertEqual(json.loads((directory / 'inventory-after.json').read_text(encoding='utf-8')), after)
                backend.pool.shutdown.assert_called_once_with(wait=True)

    def test_monitor_clears_stale_warnings_and_persists_unique_history_after_recovery(self):
        rows = [sample(now) for now in (0, 5, 65, 70, 75)]
        for row in rows:
            row['warnings'] = ['stale backend warning']
            row['audit']['cpu_idle'] = 90 if row['monotonic'] < 75 else 590
        backend = Mock()
        backend.sample.side_effect = rows
        report = {'gaps': []}
        with tempfile.TemporaryDirectory() as tmp:
            runner = r.Runner(config(), backend, Path(tmp), report,
                              r.Guard(LIMITS, True, cpu_action='warn'), {})
            runner.context = {'phase': 'measure', 'scenario': 'synthetic'}
            for _ in rows:
                runner.monitor()
            recorded = [json.loads(line) for line in (Path(tmp) / 'metrics.ndjson').read_text(encoding='utf-8').splitlines()]
            saved = json.loads((Path(tmp) / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual([row['warnings'] for row in recorded],
                         [[], [], ['sustained appliance CPU'], ['sustained appliance CPU'], []])
        self.assertEqual(saved['warnings'], ['sustained appliance CPU'])
        self.assertEqual(saved['current']['warnings'], [])
        self.assertEqual(saved['current']['stop_reasons'], [])
        self.assertNotIn('stale backend warning', json.dumps(saved))

    def test_ngfw_cpu_topology_is_one_socket_for_cfggen(self):
        domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        self.assertIn(
            '<topology sockets="1" cores="{{ ngfw_lab_ngfw_vcpus }}" threads="1"/>',
            domain,
        )
        self.assertNotIn("<numa>", domain)

    def test_domains_have_stable_uuids_for_redefinition(self):
        defaults = (ROOT / "defaults/main.yml").read_text()
        ngfw_domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        mngt_domain = (ROOT / "templates/mngt-domain.xml.j2").read_text()
        self.assertIn('ngfw_lab_ngfw_uuid: "c365a5c0-2dd4-4a1f-8ae7-4fd9b9b506d5"', defaults)
        self.assertIn('ngfw_lab_mngt_uuid: "eee9f2dc-4fdf-4d1b-a91c-f3c6b571e2fa"', defaults)
        self.assertIn("<uuid>{{ ngfw_lab_ngfw_uuid }}</uuid>", ngfw_domain)
        self.assertIn("<uuid>{{ ngfw_lab_mngt_uuid }}</uuid>", mngt_domain)

    def test_ngfw_interface_order_matches_appliance_roles(self):
        domain = (ROOT / "templates/ngfw-domain.xml.j2").read_text()
        management = domain.index('mac address="52:54:00:77:00:20"')
        sync = domain.index('mac address="52:54:00:77:30:07"')
        left = domain.index('mac address="52:54:00:77:10:01"')
        right = domain.index('mac address="52:54:00:77:20:01"')
        unused = domain.index("{% for nic_number in range(3, 7) %}")
        self.assertLess(management, sync)
        self.assertLess(sync, left)
        self.assertLess(left, right)
        self.assertLess(right, unused)

    def test_lab_operator_access_is_managed_without_sudo(self):
        tasks = (ROOT / "tasks/main.yml").read_text()
        defaults = (ROOT / "defaults/main.yml").read_text()
        guide = (ROOT.parents[1] / "docs/ngfw-auditd-pilot.md").read_text()
        task_start = tasks.index("- name: Grant PT NGFW lab operators access to system libvirt")
        task_end = tasks.index("\n- name:", task_start + 1)
        operator_task = tasks[task_start:task_end]
        self.assertIn("groups:\n      - libvirt", operator_task)
        self.assertIn("append: true", operator_task)
        self.assertIn("ngfw_lab_operator_users:", defaults)
        self.assertNotIn("sudo virsh", guide)
        self.assertIn("virsh -c qemu:///system start pt-ngfw-mngt", guide)

    def test_xdr_relay_is_opt_in_and_guest_restricted(self):
        defaults = (ROOT / "defaults/main.yml").read_text()
        tasks = (ROOT / "tasks/main.yml").read_text()
        socket_unit = (ROOT / "templates/ngfw-lab-xdr-relay.socket.j2").read_text()
        service_unit = (ROOT / "templates/ngfw-lab-xdr-relay.service.j2").read_text()
        self.assertIn("ngfw_lab_xdr_relay_enabled: false", defaults)
        self.assertIn('ngfw_lab_xdr_relay_allowed_ip: "{{ ngfw_lab_management_address }}"', defaults)
        self.assertIn('from_ip: "{{ ngfw_lab_xdr_relay_allowed_ip }}"', tasks)
        self.assertIn("ListenStream={{ ngfw_lab_xdr_relay_listen_address }}", socket_unit)
        self.assertIn("systemd-socket-proxyd {{ ngfw_lab_xdr_relay_target_host }}", service_unit)

    def test_libvirt_definitions_only_run_for_changed_xml(self):
        tasks = (ROOT / "tasks/main.yml").read_text()
        network_start = tasks.index("- name: Define isolated libvirt networks")
        network_end = tasks.index("\n- name:", network_start + 1)
        domain_start = tasks.index("- name: Define PT NGFW virtual machines")
        domain_end = tasks.index("\n- name:", domain_start + 1)
        self.assertIn("\n  when: item.changed\n", tasks[network_start:network_end])
        self.assertIn("\n    - item.changed\n", tasks[domain_start:domain_end])

    def test_aggregate_tcp_bitrate_not_multiplied_by_streams(self):
        cfg = r.validate(config())
        cmd = r.command(cfg, cfg["scenarios"][0], 600)
        self.assertEqual(cmd[cmd.index("-b") + 1], "25000000")

    def test_udp_small_payload_explicit(self):
        cmd = r.command(config(), dict(kind="udp", mbps=5, length=64), 10)
        self.assertEqual(cmd[cmd.index("-l") + 1], "64")

    def test_bad_limits_and_duration_fail_before_workload(self):
        for key, value in (("duration", 0), ("repetitions", -1), ("warmup", 99999)):
            cfg = config()
            cfg[key] = value
            with self.assertRaises(ValueError):
                r.validate(cfg)

    def test_isolation_topology_and_age(self):
        evidence = dict(checked_at=1, topology={"network": "a"})
        r.validate_isolation(evidence, {"network": "a"}, 5)
        for topology, now in (({"network": "b"}, 5), ({"network": "a"}, 90000)):
            with self.assertRaises(r.Abort):
                r.validate_isolation(evidence, topology, now)

    def test_baseline_uses_measurement_median_only(self):
        baseline = dict(status="completed", signature="a", results=[
            dict(phase="warmup", scenario="tcp", metrics=dict(received_bps=1)),
            dict(phase="measure", scenario="tcp", metrics=dict(received_bps=100)),
            dict(phase="measure", scenario="tcp", metrics=dict(received_bps=200))])
        self.assertEqual(r.baseline_values(baseline, "a"), {"tcp": 150})
        with self.assertRaises(r.Abort):
            r.baseline_values(baseline, "b")

    def test_iperf_receiver_not_sender_and_missing_summary(self):
        raw = dict(end=dict(sum_received=dict(bits_per_second=12, seconds=2),
                            sum_sent=dict(bits_per_second=20, retransmits=3)))
        self.assertEqual(r.result_metrics(dict(kind="tcp"), raw)["received_bps"], 12)
        with self.assertRaises(r.Abort):
            r.result_metrics(dict(kind="tcp"), {"end": {}})

    def test_matrix_order_repeats_and_idle(self):
        cfg = config()
        runner = r.Runner(cfg, None, None, {}, None, {})
        events = []
        runner.workload = lambda s, n, phase, seconds: events.append((n, phase, seconds))
        runner.pause = lambda seconds: events.append(("idle", seconds))
        runner.monitor = lambda: None
        runner.run()
        self.assertEqual(events, [event for n in (1, 2, 3) for event in
                                  ((n, "warmup", 1), (n, "measure", 1), ("idle", 0))])

    def test_partial_reports_are_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            report = dict(status="aborted", mode="traffic-only", audit_profile="P0", started_at="test",
                          results=[], gaps=["AuditD unavailable"], reason="management unavailable")
            r.write_report(Path(tmp), report)
            self.assertEqual(json.loads((Path(tmp) / "summary.json").read_text())["status"], "aborted")
            self.assertIn("management unavailable", (Path(tmp) / "report.md").read_text())

    def test_aborted_monitor_stops_remote_job_not_only_docker_cli(self):
        class Process:
            returncode = None

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = 1

            def wait(self, timeout):
                return self.returncode

        class Backend:
            compose = ["docker", "compose"]

            def endpoint(self, service, argv):
                self.stopped = (service, argv)

        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            runner = r.Runner(config(), backend, Path(tmp), dict(gaps=[], results=[], status="running", audit_profile="test", mode="test", started_at="test"), None, {})
            calls = [0]

            def monitor():
                calls[0] += 1
                if calls[0] == 2:
                    raise r.Abort("AuditD lost is nonzero")

            runner.monitor = monitor
            with patch.object(r.subprocess, "Popen", return_value=Process()):
                with self.assertRaisesRegex(r.Abort, "lost"):
                    runner.workload(config()["scenarios"][0], 1, "measure", 1)
            self.assertEqual(backend.stopped[0], "traffic-client")
            self.assertEqual(backend.stopped[1][2], "stop")

    def test_failed_cli_still_cancels_remote_job(self):
        class Process:
            returncode = 1

            def poll(self):
                return 1

        class Backend:
            compose = ["docker", "compose"]

            def endpoint(self, service, argv):
                self.stopped = argv

        with tempfile.TemporaryDirectory() as tmp:
            backend = Backend()
            runner = r.Runner(config(), backend, Path(tmp), dict(gaps=[], results=[], status="running", audit_profile="test", mode="test", started_at="test"), None, {})
            runner.monitor = lambda: None
            with patch.object(r.subprocess, "Popen", return_value=Process()):
                with self.assertRaises(r.Abort):
                    runner.workload(config()["scenarios"][0], 1, "measure", 1)
            self.assertEqual(backend.stopped[2], "stop")

    def test_successful_application_metrics_contract_is_unchanged(self):
        raw = dict(successful_requests=100, errors=0, requests_per_second=50)
        for kind in ('http', 'short-tcp', 'dns'):
            self.assertIs(r.result_metrics(dict(kind=kind), raw), raw)


class Endpoints(unittest.TestCase):
    def test_dns_is_static_and_checks_transaction(self):
        query = b"\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + e.QUESTION
        self.assertEqual(e.dns_answer(query)[:2], b"\x12\x34")
        self.assertIsNone(e.dns_answer(query[:-1]))
        self.assertIsNone(e.dns_answer(query.replace(b"invalid", b"example")))

    def test_bounded_workload_records_errors(self):
        with patch.object(e, "request", side_effect=ConnectionRefusedError(111, "SECRET exception argument")):
            # Leave time for Windows worker startup; 50 ms could expire before any call.
            result = e.workload("http", "127.0.0.1", .5, 100, 1)
        self.assertGreater(result["errors"], 0)
        diagnostic = result['error_diagnostics']
        self.assertEqual(diagnostic['counts'], [dict(stage='unknown', exception_class='ConnectionRefusedError',
                                                   errno=111, count=result['errors'])])
        self.assertEqual(len(diagnostic['samples']), min(e.ERROR_SAMPLE_LIMIT, result['errors']))
        self.assertEqual(diagnostic['samples_omitted'], max(0, result['errors'] - e.ERROR_SAMPLE_LIMIT))
        for row in diagnostic['samples']:
            self.assertGreaterEqual(row['offset_seconds'], 0)
            self.assertLess(row['offset_seconds'], result['seconds'])
            self.assertGreaterEqual(row['duration_ms'], 0)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(r.qualification.classify(r.result_metrics(dict(kind="http"), result))[0], "GLOBAL_STOP")

    def test_error_diagnostics_have_hard_group_and_sample_bounds(self):
        diagnostic = e.ErrorDiagnostics()
        count = e.ERROR_GROUP_LIMIT + 20
        for index in range(count):
            diagnostic.record(e.RequestFailure('recv', OSError(index, 'SECRET text')), index, 20)
        result = diagnostic.result()
        self.assertEqual(len(result['counts']), e.ERROR_GROUP_LIMIT)
        self.assertEqual(result['other_errors'], 20)
        self.assertEqual(sum(row['count'] for row in result['counts']) + result['other_errors'], count)
        self.assertEqual(len(result['samples']), e.ERROR_SAMPLE_LIMIT)
        self.assertEqual(result['samples_omitted'], count - e.ERROR_SAMPLE_LIMIT)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_real_request_stage_reaches_workload_diagnostics_across_workers(self):
        with patch.object(e.socket, 'create_connection', side_effect=ConnectionRefusedError(111, 'SECRET')):
            result = e.workload('short-tcp', '10.77.20.10', .5, 100, 2)
        self.assertGreater(result['errors'], 0)
        self.assertEqual(result['successful_requests'], 0)
        self.assertEqual(result['error_diagnostics']['counts'], [dict(
            stage='connect', exception_class='ConnectionRefusedError', errno=111, count=result['errors'])])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_successful_workload_has_empty_error_diagnostics(self):
        with patch.object(e, 'request', return_value=None):
            result = e.workload('http', '10.77.20.10', .5, 100, 2)
        self.assertGreater(result['successful_requests'], 0)
        self.assertEqual(result['errors'], 0)
        self.assertEqual(result['error_diagnostics'], dict(counts=[], other_errors=0, samples=[], samples_omitted=0))
        self.assertIsNotNone(result['latency_ms_bucket_upper_bounds']['p95'])

    def test_unknown_exception_class_name_and_non_numeric_errno_are_not_exported(self):
        private_type = type('SECRET_CLASS', (OSError,), {})
        for number in ('SECRET_ERRNO', True, 99999999):
            error = private_type('SECRET argument')
            error.errno = number
            diagnostic = e.ErrorDiagnostics()
            diagnostic.record(e.RequestFailure('recv', error), 0, 1)
            result = diagnostic.result()
            self.assertEqual(result['counts'][0], dict(stage='recv', exception_class='OSError', errno=None, count=1))
            self.assertNotIn('SECRET', json.dumps(result))

    def test_tcp_failure_stage_is_recorded_without_retry_or_exception_text(self):
        for stage in ('connect', 'send', 'recv'):
            with self.subTest(stage=stage):
                sock = MagicMock()
                sock.__enter__.return_value = sock
                sock.recv.return_value = e.PAYLOAD
                error = TimeoutError(110, 'SECRET destination or payload')
                connect = Mock(return_value=sock)
                if stage == 'connect':
                    connect.side_effect = error
                else:
                    getattr(sock, 'sendall' if stage == 'send' else 'recv').side_effect = error
                with patch.object(e.socket, 'create_connection', connect):
                    with self.assertRaises(TimeoutError) as raised:
                        e.request('short-tcp', '10.77.20.10')
                connect.assert_called_once_with(('10.77.20.10', 9000), 2)
                detail = e.RequestFailure(raised.exception.ngfw_stage, raised.exception)
                self.assertEqual(detail.stage, stage)
                self.assertEqual(detail.exception_class, 'TimeoutError')
                self.assertEqual(detail.errno, 110)
                self.assertNotIn('SECRET', json.dumps(vars(detail)))
                self.assertLessEqual(sock.sendall.call_count, 1)
                self.assertLessEqual(sock.recv.call_count, 1)

    def test_tcp_partial_response_read_semantics_unchanged(self):
        sock = MagicMock()
        sock.__enter__.return_value = sock
        sock.recv.side_effect = [e.PAYLOAD[:2], e.PAYLOAD[2:]]
        with patch.object(e.socket, 'create_connection', return_value=sock):
            e.request('short-tcp', '10.77.20.10')
        sock.sendall.assert_called_once_with(e.PAYLOAD)
        self.assertEqual([call.args for call in sock.recv.call_args_list], [(len(e.PAYLOAD),), (len(e.PAYLOAD) - 2,)])

    def test_http_failure_uses_opaque_http_stage_and_preserves_timeout(self):
        conn = Mock()
        conn.getresponse.side_effect = e.http.client.BadStatusLine('SECRET raw response')
        with patch.object(e.http.client, 'HTTPConnection', return_value=conn) as constructor:
            with self.assertRaises(e.http.client.BadStatusLine) as raised:
                e.request('http', '10.77.20.10', timeout=1)
        constructor.assert_called_once_with('10.77.20.10', 8080, timeout=1)
        conn.request.assert_called_once_with('GET', '/health')
        conn.close.assert_called_once_with()
        detail = e.RequestFailure(raised.exception.ngfw_stage, raised.exception)
        self.assertEqual(detail.stage, 'http')
        self.assertEqual(detail.exception_class, 'BadStatusLine')
        self.assertNotIn('SECRET', json.dumps(vars(detail)))

    def test_dns_failure_stage_distinguishes_transport_from_response_validation(self):
        for stage in ('connect', 'send', 'recv', 'dns'):
            with self.subTest(stage=stage):
                sock = MagicMock()
                sock.__enter__.return_value = sock
                sock.recv.return_value = b'SECRET bad response'
                if stage != 'dns':
                    getattr(sock, stage).side_effect = OSError(111, 'SECRET transport')
                with patch.object(e.socket, 'socket', return_value=sock):
                    with self.assertRaises(ValueError if stage == 'dns' else OSError) as raised:
                        e.request('dns', '10.77.20.10')
                detail = e.RequestFailure(raised.exception.ngfw_stage, raised.exception)
                self.assertEqual(detail.stage, stage)
                sock.settimeout.assert_called_once_with(2)
                sock.connect.assert_called_once_with(('10.77.20.10', 5353))
                self.assertLessEqual(sock.send.call_count, 1)
                self.assertLessEqual(sock.recv.call_count, 1)
                self.assertNotIn('SECRET', json.dumps(vars(detail)))


if __name__ == "__main__":
    unittest.main()
