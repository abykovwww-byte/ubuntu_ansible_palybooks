"""Synthetic correctness fixtures, never performance evidence."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
from native_analysis.analyze import analyze, quantiles, histogram_quantiles, INVARIANTS
from native_analysis.catalog import extract
from native_analysis.query import build_request, capture


def fixture():
    rows = []
    for repeat, order in enumerate(('ABA', 'BAB', 'ABA')):
        for position, profile in enumerate(order):
            start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=10*(repeat*3+position))
            rows.append(dict(run_id=f'synthetic-{repeat}-{position}', block_id=f'synthetic-{repeat}',
                repeat=repeat, position=position, profile=profile, workload='short_tcp', regime='stable',
                start=start.isoformat(), end=(start+timedelta(minutes=5)).isoformat(),
                offered_load={'cps': 5000}, versions={'ngfw': 'synthetic'}, config_hashes={k: 'a'*64 for k in INVARIANTS},
                valid=True, outcome='COMPLETED', evidence=['synthetic-fixture-no-live-evidence'],
                achieved={'value': 5000 if profile == 'A' else 4500, 'unit': 'cps'},
                profile_evidence={'edr_installed': profile == 'B', 'audit_profile': 'stock' if profile == 'A' else 'full', 'audit_rule_count': 116 if profile == 'B' else 0},
                completeness={'expected': 30, 'observed': 30, 'missing': 0}, latency_ms=[1, 2, 3, 4, 100]))
    return {'schema_version': 1, 'synthetic': True, 'runs': rows}


class AnalysisTest(unittest.TestCase):
    def test_repeated_effect_and_scope(self):
        result = analyze(fixture())
        self.assertTrue(result['synthetic'])
        self.assertEqual(len(result['comparisons']), 3)
        self.assertTrue(result['findings'][0]['research_performance_effect_reproduced'])
        self.assertEqual(result['findings'][0]['drop_pct_range'], [10, 10])

    def test_missing_is_not_zero(self):
        source = fixture()
        source['runs'][0]['completeness']['missing'] = 1
        source['runs'][0]['completeness']['observed'] = 29
        result = analyze(source)
        self.assertFalse(result['runs'][0]['eligible'])
        self.assertFalse(result['findings'][0]['research_performance_effect_reproduced'])

    def test_stopped_edr_is_not_profile_a(self):
        source = fixture()
        source['runs'][0]['profile_evidence']['edr_installed'] = True
        self.assertIn('profile_not_established', analyze(source)['runs'][0]['reasons'])

    def test_parity_failure(self):
        source = fixture()
        source['runs'][1]['offered_load']['cps'] = 4900
        self.assertIn('parity_mismatch:offered_load', analyze(source)['rejected_blocks'][0]['reasons'])

    def test_no_mixing_stable_overload_or_configurations(self):
        source = fixture()
        for row in source['runs'][3:6]:
            row['regime'] = 'overload'
        for row in source['runs'][6:]:
            row['config_hashes']['policy'] = 'b'*64
        findings = analyze(source)['findings']
        self.assertEqual(len(findings), 3)
        self.assertFalse(any(r['research_performance_effect_reproduced'] for r in findings))

    def test_control_scatter_and_host_limit(self):
        source = fixture()
        source['runs'][2]['achieved']['value'] = 6000
        self.assertFalse(analyze(source)['findings'][0]['research_performance_effect_reproduced'])
        source = fixture()
        source['runs'][0]['outcome'] = 'HOST_LIMITED'
        self.assertFalse(analyze(source)['findings'][0]['research_performance_effect_reproduced'])

    def test_duplicate_receipt_rejected(self):
        source = fixture()
        source['runs'].append(copy.deepcopy(source['runs'][0]))
        with self.assertRaises(ValueError):
            analyze(source)

    def test_raw_quantiles_and_bounded_histogram(self):
        self.assertEqual(quantiles([1, 2, 3, 4, 100]), {'p50': 3, 'p95': 100, 'p99': 100})
        result = histogram_quantiles({'upper_bounds_ms': [1, 10, '+Inf'], 'counts': [50, 45, 5]})
        self.assertEqual(result['p95'], {'lower_ms': 1, 'upper_ms': 10, 'upper_unbounded': False})
        self.assertTrue(result['p99']['upper_unbounded'])
        with self.assertRaises(ValueError):
            histogram_quantiles({'upper_bounds_ms': [1, 10], 'counts': [50]})

    def test_duration_endurance(self):
        source = fixture()
        source['runs'][0]['regime'] = 'endurance'
        self.assertIn('measure_window_too_short', analyze(source)['runs'][0]['reasons'])


class CatalogTest(unittest.TestCase):
    def test_raw_target_nested_rows_and_unknowns_preserved(self):
        target = {'refId': 'A', 'measurement': 'synthetic', 'select': [[{'type': 'field', 'params': ['value']}]]}
        dashboard = {'title': 'synthetic', 'panels': [{'panels': [{'id': 1, 'title': 'Example', 'targets': [target]}]}]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'synthetic.json'
            path.write_text(json.dumps(dashboard), encoding='utf-8')
            result = extract(path)
        metric = result['metrics'][0]
        self.assertEqual(metric['formula']['target'], target)
        self.assertIsNone(metric['interval_seconds'])
        self.assertIsNone(metric['unit'])
        self.assertEqual(metric['kind'], 'UNKNOWN')


class QueryTest(unittest.TestCase):
    def test_read_only_influxql(self):
        for query in ('DROP DATABASE telegraf', 'SELECT * INTO other FROM cpu', 'SHOW DATABASES; DROP DATABASE x'):
            with self.assertRaises(ValueError):
                build_request('http://127.0.0.1:8086', '1', query)
        request = build_request('http://127.0.0.1:8086', '1', 'SHOW MEASUREMENTS', database='telegraf')
        self.assertIn('epoch=ns', request.full_url)

    def test_credentials_not_in_url(self):
        with patch.dict(os.environ, {'SYNTHETIC_TOKEN': 'synthetic-secret'}):
            req = build_request('http://127.0.0.1:8086', '1', 'SHOW DATABASES', username='reader', credential_env='SYNTHETIC_TOKEN')
        self.assertNotIn('synthetic-secret', req.full_url)
        with self.assertRaises(ValueError):
            build_request('http://user:secret@host:8086', '1', 'SHOW DATABASES')

    def test_failed_query_is_not_empty_measurement(self):
        class Response:
            headers = {'Content-Type': 'application/json'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"results":[{"error":"synthetic query error"}]}'
        with tempfile.TemporaryDirectory() as folder, patch('native_analysis.query.urlopen', return_value=Response()):
            destination = Path(folder)/'capture'
            with self.assertRaises(ValueError):
                capture(destination, 'http://127.0.0.1:8086', '1', 'SHOW DATABASES')
            self.assertTrue((destination/'raw.json').exists())
            self.assertEqual(json.loads((destination/'receipt.json').read_text())['status'], 'QUERY_ERROR')

    def test_partial_readback_fails_closed_and_preserves_raw(self):
        class Response:
            headers = {'Content-Type': 'application/json'}
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"results":[{"series":[{"partial":true,"name":"synthetic"}]}]}'
        with tempfile.TemporaryDirectory() as folder, patch('native_analysis.query.urlopen', return_value=Response()):
            destination = Path(folder)/'partial'
            with self.assertRaises(ValueError):
                capture(destination, 'http://127.0.0.1:8086', '1', 'SELECT * FROM synthetic')
            self.assertTrue((destination/'raw.json').exists())


if __name__ == '__main__':
    unittest.main()
