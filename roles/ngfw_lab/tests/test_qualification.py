import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from test_runner import r, e, config, ROOT
import qualification as q
import campaign as c
from test_campaign import runs
from test_measurement import config as measure_config, sample as measure_sample
import measurement


def counters(errors=2, attempts=6000, rate=50):
    return dict(attempted_requests=attempts, successful_requests=attempts-errors, errors=errors,
                attempt_rate=50, success_rate=(attempts-errors)/120, requests_per_second=(attempts-errors)/120,
                requested_rate=rate, achieved_rate=50, error_rate_pct=100*errors/attempts,
                skipped_slots=0, scheduler_lag_max_ms=.2, generator_limited=False,
                error_diagnostics={'counts': [{'stage': 'connect', 'exception_class': 'TimeoutError',
                                               'errno': 110, 'count': errors}]})


def report():
    return dict(status='running', mode='test', audit_profile='B-EDR-CURRENT', started_at='test', results=[], gaps=[])


class Qualification(unittest.TestCase):
    def test_partial_completed_campaign_cannot_be_a_control(self):
        for status in ('INVALID', 'SCENARIO_STOP', 'WARN'):
            baseline = {'status': 'completed', 'signature': 'same', 'results': [
                {'phase': 'measure', 'scenario': 'tcp', 'status': status, 'metrics': {'received_bps': 100}}]}
            with self.assertRaises(r.Abort):
                r.baseline_values(baseline, 'same')

    def test_classifications(self):
        self.assertEqual(q.classify(counters(2))[0], 'WARN')
        self.assertEqual(q.classify(counters(2), repeated=True)[0], 'SCENARIO_STOP')
        self.assertEqual(q.classify(counters(6))[0], 'SCENARIO_STOP')
        self.assertEqual(q.classify(counters(60))[0], 'GLOBAL_STOP')
        self.assertEqual(q.classify(counters(6000))[0], 'GLOBAL_STOP')

    def test_generator_limited_never_means_NGFW_limit(self):
        raw = counters(0, rate=100)
        value = r.result_metrics({'kind': 'http', 'rate': 100}, raw)
        self.assertTrue(value['generator_limited'])
        self.assertEqual(value['requested_rate'], 100)
        self.assertEqual(value['achieved_rate'], 50)
        self.assertEqual(q.classify(value)[0], 'INVALID')

    def test_udp_PPS_excludes_lost_sequence_numbers(self):
        raw = {'end': {'sum_sent': {'bits_per_second': 20e6, 'bytes': 256000, 'seconds': 1},
                       'sum_received': {'bits_per_second': 20e6, 'bytes': 255936, 'seconds': 1,
                                        'packets': 4000, 'lost_packets': 1, 'lost_percent': .025, 'jitter_ms': .1}}}
        result = r.result_metrics({'kind': 'udp', 'mbps': 20, 'length': 64}, raw)
        self.assertEqual(result['received_report_pps'], 3999)
        self.assertEqual(result['receiver_sequence_packets'], 4000)

    def test_existing_T00_binding_preserves_source_and_checks_negative_proof(self):
        evidence = dict(topology={'network': 'a'}, receiver_local_health=True,
                        client_to_receiver='unreachable', ngfw_state='shut off')
        before = copy.deepcopy(evidence)
        binding = r.bind_isolation(evidence, evidence['topology'], 'same-campaign', 'operator-record-1')
        self.assertEqual(evidence, before)
        self.assertEqual(binding['evidence_sha256'], r.fingerprint(evidence))
        with self.assertRaises(r.Abort):
            r.bind_isolation(evidence | {'client_to_receiver': 'reachable'}, evidence['topology'], 'same-campaign', 'record')

    def test_iperf_requested_and_achieved_load_differ(self):
        iperf = {'end': {'sum_sent': {'bits_per_second': 80e6, 'bytes': 1e8, 'seconds': 10},
                         'sum_received': {'bits_per_second': 79e6, 'bytes': 9.8e7, 'seconds': 10}}}
        value = r.result_metrics({'kind': 'tcp', 'mbps': 100}, iperf)
        self.assertEqual((value['requested_bps'], value['achieved_bps']), (100e6, 79e6))
        self.assertEqual(q.classify(value)[0], 'INVALID')

    def test_rare_errors_retry_then_stop_only_this_kind_with_health_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config() | {'warmup': 0, 'repetitions': 1, 'scenarios': [
                dict(name='tcp-low', kind='short-tcp', rate=50, concurrency=16),
                dict(name='tcp-high', kind='short-tcp', rate=100, concurrency=32),
                dict(name='dns', kind='dns', rate=50, concurrency=16)]}
            saved = report()
            runner = r.Runner(cfg, Mock(compose=['docker']), Path(tmp), saved, None, {})
            health = []
            runner.monitor = lambda: health.append(runner.context.copy())
            launches = []
            def launch(argv, stdout, stderr):
                kind = argv[argv.index('--kind')+1]
                launches.append((kind, len(health)))
                json.dump(counters(2 if kind == 'short-tcp' else 0), stdout)
                return Mock(returncode=0, poll=Mock(return_value=0))
            with patch.object(r.subprocess, 'Popen', side_effect=launch), \
                    patch.object(q, 'resources', return_value=({}, [], [])):
                runner.run()
            self.assertEqual([v[0] for v in launches], ['short-tcp', 'short-tcp', 'dns'])
            self.assertGreater(launches[-1][1], launches[-2][1])
            rows = json.loads((Path(tmp)/'summary.json').read_text())['results']
            self.assertEqual([v['status'] for v in rows], ['WARN', 'SCENARIO_STOP', 'PASS'])
            self.assertEqual(rows[0]['metrics']['errors'], 2)
            self.assertEqual(rows[1]['attempt'], 2)
            with (Path(tmp)/'results.csv').open(newline='') as stream:
                csv_rows = list(csv.DictReader(stream))
            self.assertEqual(csv_rows[1]['status'], 'SCENARIO_STOP')
            self.assertIn('SCENARIO_STOP', (Path(tmp)/'report.md').read_text())
            self.assertEqual(len(list(Path(tmp).glob('*measure-attempt*.json'))), 3)

    def test_severe_error_stops_entire_campaign_and_saves_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = config() | {'warmup': 0, 'repetitions': 1, 'scenarios': [
                dict(name=k, kind=k, rate=50, concurrency=16) for k in ('http', 'dns')]}
            runner = r.Runner(cfg, Mock(compose=['docker']), Path(tmp), report(), None, {})
            runner.monitor = lambda: None
            def launch(argv, stdout, stderr):
                json.dump(counters(61), stdout)
                return Mock(returncode=0, poll=Mock(return_value=0))
            with patch.object(r.subprocess, 'Popen', side_effect=launch) as call, \
                    patch.object(q, 'resources', return_value=({}, [], [])):
                with self.assertRaises(r.WindowStop) as stopped:
                    runner.run()
            self.assertEqual(stopped.exception.outcome, 'GLOBAL_STOP')
            self.assertEqual(call.call_count, 1)
            for name in ('summary.json', 'results.csv', 'report.md'):
                self.assertIn('GLOBAL_STOP', (Path(tmp)/name).read_text())

    def test_resource_quota_not_host_CPU_percentage(self):
        topology = {'endpoints': [dict(service=k, cpus=2e9) for k in ('traffic-client', 'traffic-server')]}
        rows = [{'container_resources': {e['service']: {'cpu_pct': 196, 'memory_bytes': 123}
                                          for e in topology['endpoints']}} for _ in range(3)]
        summary, gaps, limited = q.resources(rows, topology)
        self.assertFalse(gaps)
        self.assertEqual(len(limited), 2)
        self.assertEqual(summary['traffic-server']['memory_peak_bytes'], 123)

    def test_campaign_bound_isolation_has_no_arbitrary_day_expiry(self):
        evidence = dict(checked_at=0, topology={'network': 'a'}, campaign_id='campaign')
        r.validate_isolation(evidence, evidence['topology'], 200000, None, 'campaign')
        with self.assertRaises(r.Abort):
            r.validate_isolation(evidence, {'network': 'b'}, 200000, None, 'campaign')
        with self.assertRaises(r.Abort):
            r.validate_isolation(evidence, evidence['topology'], 200000, None, 'another')

    def test_temperature_only_hard_safety_threshold(self):
        cfg = measure_config() | {'qualification_mode': True, 'cpu_action': 'warn'}
        guard = measurement.Guard(cfg)
        row = measure_sample()
        row['measurement']['host']['temperature_c']['cpu/package'] = 70
        self.assertFalse(any('temperature' in reason for reason in guard.check(row, initial=True)))
        row = measure_sample(5)
        row['measurement']['host']['temperature_c']['cpu/package'] = 80
        self.assertTrue(any('temperature' in reason for reason in guard.check(row)))

    def test_plans_are_opt_in_and_Ansible_never_runs_a_campaign(self):
        plans = {p.stem: json.loads(p.read_text()) for p in (ROOT/'files/plans').glob('*.json')}
        self.assertEqual(set(plans), {'edr-calibration', 'edr-ablation', 'edr-confirmation', 'edr-endurance'})
        value = config() | plans['edr-calibration'] | {'campaign_id': 'test'}
        r.validate(value)
        self.assertEqual([s['mbps'] for s in value['scenarios'] if s['kind'] == 'tcp'], [100,300,600,900])
        tasks = (ROOT/'tasks/main.yml').read_text()
        self.assertNotIn('audit_profile_ctl.py\n      - apply', tasks)
        self.assertIn('runner.py"\n      - plan', tasks)
        self.assertNotIn('runner.py"\n      - run', tasks)


class Comparisons(unittest.TestCase):
    def values(self):
        old = runs()
        values = [old[1], old[2], copy.deepcopy(old[1])]
        for i, (run, profile) in enumerate(zip(values, ['B-EDR-CURRENT','C-EDR-NET','B-EDR-CURRENT'])):
            run[0].update(audit_profile=profile, started_at=str(i*2), finished_at=str(i*2+1))
            run[0]['context']['profile_id'] = profile
        return values
    def test_comparison_rejects_observer_workload_and_restoration_drift(self):
        for changed in ('signature', 'measurement_config', 'rules_sha256'):
            values = self.values()
            if changed == 'rules_sha256':
                values[-1][1]['inventory'][changed] = 'different'
            else:
                values[1][0][changed] = 'different'
            with patch.object(c, 'windows', side_effect=values), self.assertRaises(ValueError):
                c.qualification_evaluate('net', ['b1','c','b2'])


if __name__ == '__main__':
    unittest.main()
