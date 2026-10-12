import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'files/extreme'))
spec = importlib.util.spec_from_file_location('control_capacity', ROOT / 'files/extreme/control_capacity.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def limits():
    return dict(success_ratio=.95, max_error_ratio=.005, max_udp_loss_pct=.5,
                min_fd_reserve=4096, min_tuple_reserve=4096, max_memory_ratio=.85,
                max_pids_ratio=.85, max_throttled_ratio=.01, host_memory_stop_pct=90,
                host_disk_stop_pct=80, sample_seconds=10, temperature_stop_c=85)


def sample(stamp=0):
    row = dict(processes=[dict(pid=1, start_ticks=1, fds=100, fd_soft=131072, fd_hard=131072)],
               ports=[1024, 65535], reserved_ports='5201,5353,8080,9000', sockets=[],
               cpu=dict(nr_periods=100 + stamp, nr_throttled=0), memory_current=1000000,
               memory_max=2147483648, memory_events=dict(high=0, max=0, oom=0, oom_kill=0),
               pids_current=10, pids_max=192, pids_events=dict(max=0), held=None)
    return dict(time=stamp, endpoints={s: copy.deepcopy(row) for s in c.SERVICES})


class ResourceTests(unittest.TestCase):
    def test_thresholds_are_explicit_and_bounded(self):
        self.assertEqual(c.capacity_limits(limits()), limits())
        for update in ({'success_ratio': .1}, {'sample_seconds': 1}, {'min_tuple_reserve': 0}, {'temperature_stop_c': float('nan')}):
            with self.subTest(update=update), self.assertRaises(c.Blocked):
                c.capacity_limits(limits() | update)

    def test_actual_tuple_space_excludes_reserved_and_occupied_ports(self):
        row = sample()['endpoints']['traffic-client']
        total, free = c.tuple_capacity(row)
        self.assertEqual(total, 64508)
        self.assertEqual(total, free)
        row['sockets'] = [{'local': '0100007F:0400'}] * 3
        self.assertEqual(c.tuple_capacity(row)[1], total - 1)

    def test_missing_samples_gaps_and_affirmative_pressure_prevent_capacity_claim(self):
        self.assertTrue(c.resources_proven([], limits()))
        self.assertTrue(c.resources_proven([sample(0), sample(40)], limits()))
        self.assertEqual(c.resources_proven([sample(0), sample(10)], limits()), [])
        for field, value in (('memory_current', 2100000000), ('pids_current', 190)):
            after = sample(10)
            after['endpoints']['traffic-client'][field] = value
            self.assertTrue(c.resources_proven([sample(0), after], limits()))
        after = sample(10)
        after['endpoints']['traffic-server']['cpu']['nr_throttled'] = 8
        self.assertIn('traffic-server:cgroup_throttling', c.resources_proven([sample(0), after], limits()))
        after = sample(10)
        after['endpoints']['traffic-server']['memory_events']['oom_kill'] = 1
        self.assertIn('traffic-server:memory_events', c.resources_proven([sample(0), after], limits()))

    def test_cpu_busy_diagnostic_alone_is_not_capacity_failure(self):
        samples = [sample(0), sample(10)]
        for row in samples:
            row['host'] = {'cpu_percent': 100}
        self.assertEqual(c.resources_proven(samples, limits()), [])


class MeasurementTests(unittest.TestCase):
    def test_false_management_or_receiver_health_stops_and_preserves_sample(self):
        for failed in ('management', 'receiver'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                backend = Mock(config=dict(target='10.88.0.3', management_address='10.77.0.20',
                                            management_port=22, mngt_address='10.77.0.10', mngt_port=443))
                backend.host.return_value = dict(memory_used_pct=20, disk_used_pct=20)
                backend.endpoint.return_value = json.dumps(sample()['endpoints']['traffic-client'])
                backend.tcp_ok.return_value = failed != 'management'
                backend.probe_endpoint.return_value = failed != 'receiver'
                monitor = c.Monitor(backend, Path(tmp), {}, limits())
                with patch.object(c.Path, 'glob', return_value=[Mock(read_text=lambda: '40000')]):
                    with self.assertRaisesRegex(c.Blocked, 'health failed'):
                        monitor.monitor()
                self.assertIn('Blocked', (Path(tmp) / 'resource-samples.ndjson').read_text())

    def iperf(self, receiver=True, seconds=300, bps=900000000):
        value = {'start': {'test_start': {'protocol': 'TCP'}, 'timestamp': {'timesecs': 1700000000}},
                 'end': {'sum_sent': {'bytes': 100, 'seconds': seconds}}}
        if receiver:
            value['end']['sum_received'] = dict(bytes=int(bps * seconds / 8), seconds=seconds,
                                                start=0, end=seconds, sender=False, bits_per_second=bps)
        return value

    def test_measured_receiver_rate_duration_and_failed_windows_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            report = {'run_id': 'run', 'components': [dict(kind='tcp', path='raw.json', phase='measure', exit_code=0)]}
            for receiver, seconds, bps, expected in ((True, 300, 900000000, False),
                    (False, 300, 900000000, True), (True, 10, 900000000, True), (True, 300, 1000000, True)):
                with self.subTest(receiver=receiver, seconds=seconds, bps=bps):
                    c.save(directory / 'raw.json', self.iperf(receiver, seconds, bps))
                    _, issues = c.measure_results(report, directory, {'kind': 'tcp', 'bps': 900000000}, 300, limits(), [])
                    self.assertEqual(bool(issues), expected)
            report['components'][0]['exit_code'] = 124
            c.save(directory / 'raw.json', self.iperf())
            self.assertTrue(c.measure_results(report, directory, {'kind': 'tcp', 'bps': 900000000}, 300, limits(), [])[1])

    def test_held_client_success_without_independent_receiver_window_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'raw.json').write_text(json.dumps(dict(record_type='final', seconds=300, measure_start=0,
                                                time_end=300, errors=0, attempted_requests=6000)) + '\n')
            report = {'run_id': 'run', 'components': [dict(kind='held', path='raw.json', phase='measure', exit_code=0)]}
            with patch.object(c, 'extreme_observation', return_value=({}, {}, [])):
                _, issues = c.measure_results(report, directory, {'kind': 'held', 'sessions': 20000}, 300, limits(), [])
            self.assertIn('held:receiver_sustained_held_population_unproven', issues)

    def test_blocked_run_still_preserves_terminal_receipt_and_receiver_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            c.save(directory / 'anchor.json', {})
            backend = Mock(config={'target': '10.77.20.10'}, compose=['docker', 'compose'])
            backend.call.return_value = 'receiver log\n'
            campaign = {'workloads': [{'id': 'tcp', 'offered': {'kind': 'tcp', 'bps': 900000000}}]}
            with patch.object(c, 'validate_anchor'), patch.object(c, 'inspect_endpoints', side_effect=c.Blocked('wrong route')):
                report = c.run_capacity(backend, campaign, directory / 'anchor.json', 'tcp', directory, 300, limits())
            self.assertEqual(report['status'], 'BLOCKED')
            self.assertTrue((directory / 'receipt.json').exists())
            self.assertTrue((directory / 'receiver.log').exists())
            self.assertNotIn('resources_unlimited', report)

    def test_finalize_refuses_unmeasured_or_failed_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'receipt.json'
            c.save(path, {'status': 'CAPACITY_NOT_PROVEN'})
            backend = Mock()
            with self.assertRaises(c.Blocked):
                c.finalize(backend, path)
            backend.call.assert_not_called()

    def test_finalize_rejects_changed_return_route_despite_matching_topology_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'receipt.json'
            c.save(path, dict(status='MEASURED_PENDING_DUT_RETURN', evidence=[], anchor={}, campaign={}))
            anchor = dict(configs={'image': 'synthetic'}, topology_hash=c.plan.fingerprint({'same': 'topology'}),
                          state={'routes': {'client': [{'dst': 'default', 'gateway': '10.77.10.1'}]},
                                 'addresses': {'client': '10.77.10.10'}})
            returned = dict(configs=anchor['configs'], routes={'client': []}, addresses=anchor['state']['addresses'])
            backend = Mock()
            backend.topology.return_value = {'same': 'topology'}
            with patch.object(c.controller, 'artifact', return_value=json.dumps(anchor).encode()), \
                    patch.object(c, 'validate_anchor'), patch.object(c, 'inspect_endpoints', return_value=returned):
                with self.assertRaisesRegex(c.Blocked, 'return configuration'):
                    c.finalize(backend, path)
            self.assertFalse((path.parent / 'control-capacity.json').exists())
            self.assertTrue((path.parent / 'dut-return.json').exists())

    def test_finalization_requires_new_negative_isolation_and_restored_positive_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            topology, routes, addresses = {'synthetic': 'topology'}, {'client': []}, {'client': '10.77.10.10'}
            anchor = dict(configs={}, topology_hash=c.plan.fingerprint(topology), target='10.77.20.10',
                          state=dict(routes=routes, addresses=addresses))
            now = c.time.time()
            report = dict(status='MEASURED_PENDING_DUT_RETURN', evidence=[], anchor={},
                          campaign={'campaign_id': 'synthetic', 'invariants': {'generator': 'a'*64, 'receiver': 'b'*64}},
                          finished_at=c.controller.datetime.fromtimestamp(now - 100, c.controller.timezone.utc).isoformat(),
                          offered={'kind': 'tcp', 'bps': 900000000}, duration_seconds=300,
                          offered_achieved=True, resources_unlimited=True, resource_scope='synthetic fixture')
            c.save(directory / 'receipt.json', report)
            c.save(directory / 'direct-before.json', {'mode': 'direct', 'synthetic': True})
            backend = Mock(config={'target': anchor['target']})
            backend.topology.return_value = topology
            returned = dict(configs={}, routes=routes, addresses=addresses)
            negative = dict(checked_at=now - 200, topology=topology, ngfw_state='shut off',
                            client_to_receiver='unreachable', receiver_local_health=True)
            with patch.object(c.controller, 'artifact', return_value=json.dumps(anchor).encode()), \
                    patch.object(c, 'validate_anchor'), patch.object(c, 'inspect_endpoints', return_value=returned):
                c.save(directory / 'isolation.json', negative)
                with self.assertRaisesRegex(c.Blocked, 'predates direct run'):
                    c.finalize(backend, directory / 'receipt.json', directory / 'isolation.json')
                negative['checked_at'] = now - 50
                c.save(directory / 'isolation.json', negative)
                backend.probe_endpoint.return_value = False
                with self.assertRaisesRegex(c.Blocked, 'restored DUT dataplane health failed'):
                    c.finalize(backend, directory / 'receipt.json', directory / 'isolation.json')
                self.assertFalse((directory / 'control-capacity.json').exists())
                backend.probe_endpoint.return_value = True
                result = c.finalize(backend, directory / 'receipt.json', directory / 'isolation.json')
                self.assertEqual(result['status'], 'VALID')
                self.assertIn(c.reference(directory / 'isolation.json'), result['evidence'])


class IaCTests(unittest.TestCase):
    def render(self, mode, enabled=True):
        import jinja2
        import yaml
        values = yaml.safe_load((ROOT / 'defaults/main.yml').read_text())
        values.update(ngfw_lab_extreme_enabled=enabled, ngfw_lab_endpoint_path=mode,
                      ngfw_lab_policy_listener_ranges=[[10000, 10991], [20000, 20099]])
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters['to_nice_json'] = json.dumps
        for key in ('ngfw_lab_traffic_client_cpus', 'ngfw_lab_traffic_server_cpus',
                    'ngfw_lab_traffic_client_memory', 'ngfw_lab_traffic_server_memory'):
            values[key] = env.from_string(str(values[key])).render(values)
        return yaml.safe_load(env.from_string((ROOT / 'templates/compose.yml.j2').read_text()).render(values))

    def test_modes_keep_identical_endpoint_settings_and_only_one_nic(self):
        dut, direct = self.render('ngfw'), self.render('direct')
        self.assertTrue(direct['networks']['control']['internal'])
        self.assertEqual(set(direct['networks']), {'control'})
        for name in c.SERVICES:
            a, b = dut['services'][name], direct['services'][name]
            self.assertEqual(len(a.pop('networks')), 1)
            self.assertEqual(len(b.pop('networks')), 1)
            self.assertEqual(a, b)
            self.assertEqual(a['ulimits']['nofile']['soft'], 131072)
            self.assertEqual(a['cpus'], 2)
            self.assertEqual(a['mem_limit'], 2147483648)
            self.assertNotIn('ports', a)
        server = dut['services']['traffic-server']
        self.assertIn('/opt/extreme/load.py', server['entrypoint'])
        self.assertIn('--with-iperf', server['command'])
        self.assertEqual(json.loads(server['environment']['NGFW_POLICY_LISTEN_RANGES']), [[10000, 10991], [20000, 20099]])

    def test_default_remains_legacy_and_disabled(self):
        value = self.render('ngfw', False)
        self.assertIn('/opt/traffic/endpoint.py', value['services']['traffic-server']['entrypoint'])
        self.assertNotIn('control', value['networks'])
        self.assertNotIn('ulimits', value['services']['traffic-client'])

    def docker_config(self, project, legacy=False):
        value = self.render('ngfw' if legacy else 'direct', not legacy)
        for service in value['services'].values():
            if not legacy:
                service['networks'] = {'control': None}
            elif service['mem_limit'] == '512m':
                service['mem_limit'] = 536870912
            service['volumes'] = [dict(type='bind', source=str(project / 'extreme'), target='/opt/extreme', read_only=True)]
        return value

    def test_candidate_privilege_dual_network_and_resource_regressions_fail_before_apply(self):
        project = Path('synthetic-project').resolve()
        value = self.docker_config(project)
        self.assertTrue(c.validate_install_config(value, project))
        for update in ({'ports': ['9000:9000']}, {'networks': {'control': None, 'left': None}},
                       {'privileged': True}, {'cpus': 1}, {'mem_limit': 536870912},
                       {'ulimits': {'nofile': {'soft': 1024, 'hard': 131072}}}):
            with self.subTest(update=update), self.assertRaises(c.Blocked):
                bad = copy.deepcopy(value)
                bad['services']['traffic-client'].update(update)
                c.validate_install_config(bad, project)

    def test_same_mode_source_promotion_forces_recreate_and_waits_for_receiver(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project, stage, reports = base / 'project', base / 'stage', base / 'reports'
            for path in (project, stage, reports, stage / 'extreme'):
                path.mkdir()
            (stage / 'extreme/load.py').write_text('# public new revision\n')
            candidate = stage / 'compose.pending.yml'
            candidate.write_text('services: {}\n')
            value = self.docker_config(project)
            backend = Mock(config={'project_dir': str(project), 'reports_dir': str(reports)}, compose=['docker', 'compose'])
            backend.call.side_effect = [json.dumps(value), '', '']
            backend.probe_endpoint.return_value = True
            with patch.object(c.runner, 'validate'), patch.object(c, 'inspect_endpoints', return_value={'mode': 'direct'}):
                c.install_endpoints(backend, candidate, stage)
            self.assertEqual((project / 'extreme/load.py').read_text(), '# public new revision\n')
            self.assertIn('--force-recreate', backend.call.call_args_list[-1].args[0])
            backend.probe_endpoint.assert_called_once_with('traffic-server', '127.0.0.1')
            self.assertEqual(c.controller.read_json(reports / 'endpoint-switch.json')['status'], 'INSTALLED')

    def test_legacy_no_start_promotes_monitor_assets_without_build_or_start_and_downgrade_is_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project, stage, reports = base / 'project', base / 'stage', base / 'reports'
            for path in (project, stage, reports, stage / 'monitor'):
                path.mkdir()
            (stage / 'monitor/index.html').write_text('<p>public monitor</p>')
            candidate = stage / 'compose.pending.yml'
            candidate.write_text('services: {}\n')
            backend = Mock(config={'project_dir': str(project), 'reports_dir': str(reports)}, compose=['docker', 'compose'])
            backend.call.return_value = json.dumps(self.docker_config(project, legacy=True))
            with patch.object(c.runner, 'validate'):
                c.install_endpoints(backend, candidate, stage, legacy=True, start=False)
            self.assertEqual(backend.call.call_count, 1)
            backend.probe_endpoint.assert_not_called()
            self.assertTrue((project / 'monitor/index.html').exists())
            self.assertFalse((reports / '.extreme-managed').exists())
            self.assertEqual(c.controller.read_json(reports / 'endpoint-switch.json')['status'], 'STAGED_NOT_STARTED')
            (reports / '.extreme-managed').write_text('already managed')
            with self.assertRaisesRegex(c.Blocked, 'cannot downgrade'):
                c.reject_legacy_downgrade(backend.config)

    def test_failed_extreme_transition_receipt_also_prevents_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            c.save(Path(tmp) / 'endpoint-switch.json', {'status': 'FAILED', 'managed_extreme': True})
            with self.assertRaises(c.Blocked):
                c.reject_legacy_downgrade({'reports_dir': tmp})


if __name__ == '__main__':
    unittest.main()
