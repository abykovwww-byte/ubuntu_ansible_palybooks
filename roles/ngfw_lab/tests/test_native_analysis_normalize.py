"""Adapter regressions including trimmed iperf output; no live traffic or claim."""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
from native_analysis.analyze import INVARIANTS, analyze
from native_analysis.normalize import normalize_window, normalize_campaign, iperf_observation


def window(ident='synthetic-1', profile='A', kind='dns'):
    return {'run_id': ident, 'block_id': 'synthetic-block', 'repeat': 1, 'position': 0,
            'profile': profile, 'workload': kind, 'offered': {'kind': kind, 'rate': 5000}}


def receipt(w):
    return {'run_id': w['run_id'], 'profile': w['profile'], 'workload': w['workload'], 'regime': 'stable',
            'offered_load': w['offered'], 'versions': {'ngfw': 'SYNTHETIC'},
            'config_hashes': {key: 'a'*64 for key in INVARIANTS},
            'profile_evidence': {'edr_installed': w['profile'] == 'B', 'audit_profile': 'full' if w['profile'] == 'B' else 'stock', 'audit_rule_count': 116 if w['profile'] == 'B' else 0},
            'valid': True, 'window_validated': True, 'outcome': 'STABLE_LIMIT_NOT_ESTABLISHED',
            'evidence': ['SYNTHETIC-CONTROLLER-EVIDENCE'],
            'native_completeness': {'expected': 30, 'observed': 30, 'missing': 0, 'evidence': ['SYNTHETIC-NATIVE-POINTS']}}


def raw(w, start=1767225600, success=1350000):
    return {'record_type': 'final', 'run_id': w['run_id'], 'kind': w['offered']['kind'],
            'measure_start': start, 'time_end': start+300, 'seconds': 300,
            'successful_requests': success, 'attempted_requests': 1500000,
            'success_rate': success/300, 'achieved_rate': 5000, 'attempt_rate': 5000,
            'errors': 1500000-success, 'error_classes': {'synthetic': 1500000-success},
            'censored_at_boundary': 0, 'pending_requests': 0,
            'latency_histogram': {'bounds_ms': [1, 10], 'counts': [success-1, 0, 1],
                                  'cumulative': False, 'population': 'successful_requests', 'overflow': 'unbounded'},
            'stop_reason': None}


def iperf_312_tcp():
    """Trimmed observed schema/counters; no hosts, addresses, cookies or logs."""
    return {
        'start': {'version': 'iperf 3.12', 'timestamp': {'timesecs': 1791815601},
                  'test_start': {'protocol': 'TCP', 'num_streams': 4, 'duration': 300, 'reverse': 0}},
        'end': {
            'sum_sent': {'start': 0, 'end': 300.000072, 'seconds': 300.000072,
                         'bytes': 33749991424, 'bits_per_second': 899999555.3067734,
                         'retransmits': 0, 'sender': True},
            'sum_received': {'start': 0, 'end': 300.000214, 'seconds': 300.000214,
                             'bytes': 33749991424, 'bits_per_second': 899999129.3072877,
                             'sender': True},
        },
    }


class IperfReceiverTests(unittest.TestCase):
    def test_312_named_tcp_received_summary_uses_receiver_time_despite_local_sender_role(self):
        document = iperf_312_tcp()
        original = copy.deepcopy(document)
        result, observation, errors = iperf_observation(json.dumps(document), 'tcp', {})
        self.assertEqual(errors, [])
        self.assertEqual(observation['receiver_scope'], 'client.end.sum_received')
        self.assertIs(observation['receiver_summary']['sender'], True)
        self.assertEqual(observation['iperf_version'], 'iperf 3.12')
        self.assertEqual(observation['iperf_reverse'], 0)
        self.assertAlmostEqual(result['achieved']['value'], 899999129.3072877)
        self.assertNotEqual(result['achieved']['value'], document['end']['sum_sent']['bits_per_second'])
        self.assertEqual(result['end'], '2026-10-12T14:38:21.000214+00:00')
        self.assertEqual(document, original)

    def test_reverse_tcp_named_received_summary_keeps_receiver_metrics_and_source_metadata(self):
        document = iperf_312_tcp()
        document['start']['test_start']['reverse'] = 1
        for summary in document['end'].values():
            summary['sender'] = False
        result, observation, errors = iperf_observation(json.dumps(document), 'tcp', {})
        self.assertEqual(errors, [])
        self.assertEqual(observation['receiver_scope'], 'client.end.sum_received')
        self.assertIs(observation['receiver_summary']['sender'], False)
        self.assertEqual(observation['iperf_version'], 'iperf 3.12')
        self.assertEqual(observation['iperf_reverse'], 1)
        self.assertAlmostEqual(result['achieved']['value'], 899999129.3072877)
        self.assertNotEqual(result['achieved']['value'], document['end']['sum_sent']['bits_per_second'])

    def test_explicit_server_tcp_sum_received_keeps_same_semantics(self):
        for encoded in (False, True):
            with self.subTest(server_json_string=encoded):
                document = iperf_312_tcp()
                receiver = document['end'].pop('sum_received')
                server = {'end': {'sum_received': receiver}}
                document['server_output_json'] = json.dumps(server) if encoded else server
                result, observation, errors = iperf_observation(json.dumps(document), 'tcp', {})
                self.assertEqual(errors, [])
                self.assertEqual(observation['receiver_scope'], 'server.end.sum_received')
                self.assertAlmostEqual(result['achieved']['value'], 899999129.3072877)

    def test_sender_only_generic_sum_and_udp_sender_flag_remain_rejected(self):
        for kind in ('tcp', 'udp'):
            for summary in ('sum_sent', 'sum'):
                with self.subTest(kind=kind, summary=summary):
                    document = iperf_312_tcp()
                    document['start']['test_start']['protocol'] = kind.upper()
                    document['end'] = {summary: document['end']['sum_received']}
                    result, _, errors = iperf_observation(json.dumps(document), kind, {})
                    self.assertIn('receiver_summary_unavailable', errors)
                    self.assertNotIn('achieved', result)
        document = iperf_312_tcp()
        document['start']['test_start']['protocol'] = 'UDP'
        result, _, errors = iperf_observation(json.dumps(document), 'udp', {})
        self.assertIn('receiver_summary_unavailable', errors)
        self.assertNotIn('achieved', result)

    def test_generic_sum_still_requires_explicit_false_sender(self):
        for sender in (False, None, 0, 'false'):
            with self.subTest(sender=sender):
                document = iperf_312_tcp()
                receiver = document['end']['sum_received'] | {'sender': sender}
                document['end'] = {'sum': receiver}
                result, _, errors = iperf_observation(json.dumps(document), 'tcp', {})
                if sender is False:
                    self.assertEqual(errors, [])
                    self.assertAlmostEqual(result['achieved']['value'], 899999129.3072877)
                else:
                    self.assertIn('receiver_summary_unavailable', errors)

    def test_named_tcp_summary_still_checks_protocol_bytes_time_and_rate(self):
        for update in ({'bytes': -1}, {'bytes': True}, {'bytes': '33749991424'},
                       {'seconds': 0}, {'seconds': float('nan')}, {'end': 301},
                       {'bits_per_second': 100000000}):
            with self.subTest(update=update):
                document = iperf_312_tcp()
                document['end']['sum_received'].update(update)
                _, _, errors = iperf_observation(json.dumps(document), 'tcp', {})
                self.assertTrue(errors)
        document = iperf_312_tcp()
        document['start']['test_start']['protocol'] = 'UDP'
        self.assertIn('iperf_protocol_mismatch', iperf_observation(json.dumps(document), 'tcp', {})[2])
        document = iperf_312_tcp()
        document['end']['sum_received'] = ['malformed']
        self.assertIn('receiver_summary_unavailable', iperf_observation(json.dumps(document), 'tcp', {})[2])


class NormalizerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def store(self, name, data, jsonl=False):
        path = self.base / name
        path.write_text(json.dumps(data) + ('\n' if jsonl else ''), encoding='utf-8')
        return path

    def test_success_rate_not_attempt_rate_and_overflow(self):
        w = window()
        result = normalize_window(w, self.store('raw.jsonl', raw(w)), receipt(w))
        self.assertTrue(result['valid'], result['normalization_errors'])
        self.assertEqual(result['achieved'], {'value': 4500, 'unit': 'qps'})
        self.assertEqual(result['latency_histogram']['upper_bounds_ms'], [1, 10, '+Inf'])
        self.assertEqual(result['start'], '2026-01-01T00:00:00+00:00')
        self.assertEqual(len(result['raw_source']['sha256']), 64)

    def test_monitor_cadence_cannot_replace_native_completeness(self):
        w = window()
        observed = receipt(w)
        observed['completeness'] = observed.pop('native_completeness') | {'scope': 'monitor_cadence'}
        result = normalize_window(w, self.store('raw.jsonl', raw(w)), observed)
        self.assertFalse(result['valid'])
        self.assertNotIn('completeness', result)
        self.assertIn('native_full_window_completeness_unavailable', result['normalization_errors'])

    def test_plan_flags_do_not_fill_missing_observed_profile(self):
        w = window() | {'valid': True, 'window_validated': True}
        result = normalize_window(w, self.store('raw.jsonl', raw(w)), {})
        self.assertFalse(result['valid'])
        self.assertNotIn('profile_evidence', result)
        self.assertNotIn('profile', result)
        self.assertNotIn('offered_load', result)

    def test_failed_and_truncated_windows_preserved(self):
        w = window()
        result = normalize_window(w, self.base/'missing.jsonl', receipt(w))
        self.assertEqual(result['run_id'], w['run_id'])
        self.assertFalse(result['valid'])
        path = self.store('raw.jsonl', raw(w) | {'record_type': 'progress'})
        with path.open('a', encoding='utf-8') as file:
            file.write('\n{"truncated')
        result = normalize_window(w, path, receipt(w) | {'outcome': 'SAFETY_STOP'})
        self.assertEqual(result['outcome'], 'SAFETY_STOP')
        self.assertFalse(result['valid'])
        self.assertNotIn('achieved', result)

    def test_identity_and_counter_mismatch_invalid(self):
        w = window()
        result = normalize_window(w, self.store('raw.jsonl', raw(w) | {'run_id': 'other', 'pending_requests': 1}), receipt(w))
        self.assertFalse(result['valid'])
        self.assertIn('workload_identity_mismatch', result['normalization_errors'])
        self.assertIn('request_counter_conservation_failed', result['normalization_errors'])

    def test_iperf_receiver_bytes_and_actual_window(self):
        w = window(kind='tcp')
        document = {'start': {'test_start': {'protocol': 'TCP'}, 'timestamp': {'timesecs': 1767225600}},
                    'end': {'sum_sent': {'bytes': 90000000000, 'bits_per_second': 2400000000},
                            'sum_received': {'sender': False, 'bytes': 30000000000, 'seconds': 300, 'start': 0, 'end': 300}}}
        result = normalize_window(w, self.store('iperf.json', document), receipt(w))
        self.assertTrue(result['valid'], result['normalization_errors'])
        self.assertEqual(result['achieved']['value'], 800000000)
        self.assertEqual(result['workload_observation']['wall_timestamp_resolution_seconds'], 1)
        del document['end']['sum_received']
        result = normalize_window(w, self.store('sender-only.json', document), receipt(w))
        self.assertFalse(result['valid'])
        self.assertIn('receiver_summary_unavailable', result['normalization_errors'])

    def test_held_requires_independent_receiver_window(self):
        w = window(kind='held')
        data = raw(w) | {'requested_sessions': 20000, 'active_sessions_at_measure_end': 20000,
                         'minimum_active_measurement_sessions': 20000, 'confirmed_measurement_sessions': 20000,
                         'stale_echo_sessions': 0}
        result = normalize_window(w, self.store('held.jsonl', data), receipt(w))
        self.assertFalse(result['valid'])
        self.assertIn('independent_held_receiver_window_unverified', result['normalization_errors'])

    def test_pipeline_three_blocks_to_analyzer(self):
        windows, paths = [], []
        for repeat, order in enumerate(('ABA', 'BAB', 'ABA')):
            for position, profile in enumerate(order):
                w = window(f'synthetic-{repeat}-{position}', profile)
                w.update(block_id=f'synthetic-{repeat}', repeat=repeat, position=position)
                filename = w['run_id']+'.jsonl'
                self.store(filename, raw(w, 1767225600+(repeat*3+position)*600, 1500000 if profile == 'A' else 1350000))
                rec = receipt(w) | {'components': [{'kind': 'dns', 'phase': 'measure', 'path': filename, 'exit_code': 0}]}
                path = self.store(w['run_id']+'.receipt.json', rec)
                paths.append(path.name)
                windows.append(w)
        manifest = normalize_campaign({'synthetic': True, 'windows': windows}, paths, self.base)
        result = analyze(manifest)
        self.assertEqual(len(manifest['runs']), 9)
        self.assertTrue(result['synthetic'])
        self.assertTrue(result['findings'][0]['research_performance_effect_reproduced'])

    def test_missing_mixed_component_and_missing_receipt_preserved(self):
        w = window(kind='mixed')
        w['offered']['components'] = [{'kind': 'tcp', 'bps': 900000000}, {'kind': 'dns', 'rate': 5000}]
        output = normalize_campaign({'windows': [w]}, [], self.base)
        self.assertEqual(len(output['runs']), 2)
        self.assertTrue(all(not row['valid'] for row in output['runs']))
        self.assertNotEqual(output['runs'][0]['run_id'], output['runs'][1]['run_id'])

    def test_stabilization_is_preserved_but_not_analyzed_as_measurement(self):
        w = window()
        self.store('measure.jsonl', raw(w))
        observed = receipt(w) | {'components': [
            {'kind': 'dns', 'phase': 'stabilization', 'path': 'stabilization.jsonl', 'exit_code': 0},
            {'kind': 'dns', 'phase': 'measure', 'path': 'measure.jsonl', 'exit_code': 0}]}
        path = self.store('receipt.json', observed)
        output = normalize_campaign({'windows': [w]}, [path.name], self.base)
        self.assertEqual(len(output['runs']), 1)
        self.assertTrue(output['runs'][0]['valid'], output['runs'][0]['normalization_errors'])
        self.assertEqual(len(output['runs'][0]['nonmeasurement_components']), 1)

    def test_failed_normalization_reason_survives_analysis(self):
        w = window()
        row = raw(w) | {'stop_reason': 'synthetic stop'}
        normalized = normalize_window(w, self.store('failed.jsonl', row), receipt(w))
        output = analyze({'schema_version': 1, 'synthetic': True, 'runs': [normalized]})
        self.assertEqual(output['runs'][0]['stop_reason'], 'synthetic stop')
        self.assertIn('workload_stopped_early', output['runs'][0]['normalization_errors'])

    def test_missing_component_phase_is_not_assumed_to_be_measurement(self):
        w = window()
        self.store('raw.jsonl', raw(w))
        path = self.store('receipt.json', receipt(w) | {'components': [{'kind': 'dns', 'path': 'raw.jsonl', 'exit_code': 0}]})
        output = normalize_campaign({'windows': [w]}, [path.name], self.base)
        self.assertFalse(output['runs'][0]['valid'])
        self.assertEqual(len(output['runs'][0]['nonmeasurement_components']), 1)

    def test_controller_stop_reason_survives_missing_workload(self):
        w = window()
        observed = receipt(w) | {'outcome': 'SAFETY_STOP', 'reason': 'synthetic native query stopped', 'gaps': ['native data stale']}
        run = normalize_window(w, self.base/'missing.jsonl', observed)
        output = analyze({'schema_version': 1, 'runs': [run], 'synthetic': True})
        self.assertEqual(output['runs'][0]['stop_reason'], 'synthetic native query stopped')
        self.assertEqual(output['runs'][0]['controller_gaps'], ['native data stale'])


if __name__ == '__main__':
    unittest.main()
