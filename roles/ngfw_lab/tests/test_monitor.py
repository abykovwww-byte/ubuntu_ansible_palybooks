from datetime import datetime, timezone
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import monitor


class Monitor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.run = self.root / 'SYNTHETIC'
        self.run.mkdir()
        self.summary = {'status': 'running', 'audit_profile': 'A', 'config': {'secret': 'NEVER_EXPOSE'},
                        'results': [], 'measurement_config': {}, 'probe_argv': ['SECRET_ARGV']}
        (self.run / 'summary.json').write_text(json.dumps(self.summary), encoding='utf-8')
        self.store = monitor.Store(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_no_secret_projection_or_fake_zero(self):
        result = self.store.run('SYNTHETIC')
        self.assertTrue(result['stale'])
        self.assertNotIn('SECRET', json.dumps(result))
        row = monitor.project_sample({'measurement': {}})
        self.assertIsNone(row['guest_cpu_pct'])
        self.assertIsNone(row['backlog'])

    def test_live_stale_and_partial_line(self):
        (self.run / 'metrics.ndjson').write_text(json.dumps({'time': datetime.now(timezone.utc).isoformat()}) + '\n{', encoding='utf-8')
        result = self.store.run('SYNTHETIC')
        self.assertFalse(result['stale'])
        self.assertEqual(len(result['samples']), 1)
        self.assertEqual(len(self.store.runs()), 1)

    def test_verified_polling_marker_does_not_hide_core_load(self):
        row = monitor.project_sample({'measurement': {'guest': {
            'derived': {'cpu': {'cpu2': {'busy_pct': 100}, 'cpu0': {'busy_pct': 12}}},
            'busy_poll_baseline': {'expected_cores': ['cpu0', 'cpu2'],
                                   'verified_cores': ['cpu2', 'cpu99', 'NOT_A_CORE'],
                                   'evidence_sha256': 'PRIVATE_NOT_PROJECTED'}}}})
        self.assertEqual(row['verified_polling_cores'], ['cpu2'])
        self.assertEqual(row['cores'], {'cpu2': 100, 'cpu0': 12})
        self.assertNotIn('PRIVATE_NOT_PROJECTED', json.dumps(row))
        self.assertEqual(monitor.project_sample({})['verified_polling_cores'], [])

    def test_traversal_rejected(self):
        for path in ['..', '../secret', 'a/b', '%2e%2e', 'C:\\secret']:
            with self.assertRaises(ValueError):
                self.store.directory(path)

    def test_loopback_readonly_and_cross_origin_denied(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), monitor.handler(self.store))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
        try:
            for method, path, headers, expected in [
                ('GET', '/', {}, 200), ('GET', '/api/run/SYNTHETIC', {}, 200),
                ('GET', '/api/runs', {'Host': 'evil.test'}, 403),
                ('GET', '/api/runs', {'Origin': 'https://evil.test'}, 403),
                ('GET', '/api/runs', {'Sec-Fetch-Site': 'cross-site'}, 403),
                ('POST', '/api/start', {}, 501), ('GET', '/summary.json', {}, 404),
                ('GET', '/api/run/../secret', {}, 404)]:
                client.request(method, path, headers=headers)
                response = client.getresponse()
                self.assertEqual(response.status, expected)
                response.read()
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
