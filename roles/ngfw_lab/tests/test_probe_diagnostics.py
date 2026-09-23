import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_runner import r, e, config
from traffic.probe_diagnostics import decode_failure, failure_payload


class ProbeDiagnostics(unittest.TestCase):
    def test_cli_failure_has_only_bounded_metadata_and_still_exits_nonzero(self):
        error = TimeoutError(110, 'SECRET response body')
        error.ngfw_stage = 'http'
        with patch.object(sys, 'argv', ['endpoint.py', 'probe']), \
                patch.object(e, 'request', side_effect=error), \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
            with self.assertRaises(SystemExit) as stopped:
                e.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(decode_failure(output.getvalue()),
                         {'stage': 'http', 'exception_class': 'TimeoutError', 'errno': 110})
        self.assertNotIn('SECRET', output.getvalue())

    def test_success_preserves_silent_probe_and_one_second_timeout(self):
        with patch.object(sys, 'argv', ['endpoint.py', 'probe']), \
                patch.object(e, 'request') as request, \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
            e.main()
        request.assert_called_once_with('http', '10.77.20.10', timeout=1)
        self.assertEqual(output.getvalue(), '')

    def test_parser_rejects_untrusted_payload_without_copying_text(self):
        valid = failure_payload('connect', ConnectionRefusedError(111, 'SECRET'))
        bad_errors = [valid['error'] | {'message': 'SECRET'},
                      valid['error'] | {'stage': 'SECRET'},
                      valid['error'] | {'exception_class': 'SECRET'},
                      valid['error'] | {'errno': True},
                      valid['error'] | {'errno': 1000000}]
        values = ['SECRET traceback', 'x' * 1025, json.dumps({'schema_version': True, 'error': valid['error']})]
        values += [json.dumps(valid | {'error': value}) for value in bad_errors]
        for value in values:
            with self.subTest(value=value[:80]):
                self.assertIsNone(decode_failure(value))

    def test_request_failure_survives_sample_without_becoming_a_success(self):
        backend = r.Backend(config())
        error = subprocess.CalledProcessError(1, ['SECRET argv'],
            output=json.dumps(failure_payload('http', TimeoutError(110, 'SECRET'))), stderr='SECRET traceback')
        try:
            with patch.object(backend, 'endpoint', side_effect=error), \
                    patch.object(backend, 'call', return_value=''), \
                    patch.object(backend, 'host', return_value={}), \
                    patch.object(backend, 'audit', return_value={}), \
                    patch.object(backend, 'state', return_value='running'), \
                    patch.object(backend, 'tcp_ok', return_value=True):
                row = backend.sample()
            self.assertIn('dataplane_ok', row['unavailable'])
            self.assertNotIn('dataplane_ok', row)
            self.assertNotIn('service_probe_ms', row)
            detail = row['check_errors']['dataplane_ok']
            self.assertEqual(detail['layer'], 'request')
            self.assertEqual(detail['exception_class'], 'TimeoutError')
            self.assertEqual(detail['errno'], 110)
            self.assertEqual(detail['returncode'], 1)
            self.assertGreaterEqual(detail['elapsed_ms'], 0)
            self.assertNotIn('SECRET', json.dumps(row))
        finally:
            backend.pool.shutdown(wait=True)

    def test_command_timeout_or_unstructured_failure_is_not_a_network_verdict(self):
        cases = [(subprocess.TimeoutExpired(['SECRET'], 4, output='SECRET', stderr='SECRET'), 'TimeoutExpired'),
                 (subprocess.CalledProcessError(125, ['SECRET'], output='SECRET', stderr='SECRET'), 'CalledProcessError'),
                 (PermissionError(13, 'SECRET'), 'OSError'),
                 (ValueError('SECRET'), 'ValueError')]
        backend = r.Backend(config())
        try:
            for error, expected in cases:
                with self.subTest(expected=expected), patch.object(backend, 'endpoint', side_effect=error):
                    with self.assertRaises(r.ProbeFailure) as caught:
                        backend.probe_endpoint('traffic-client', '10.77.20.10')
                    detail = caught.exception.diagnostic
                    self.assertEqual(detail['layer'], 'command')
                    self.assertEqual(detail['exception_class'], expected)
                    self.assertNotIn('SECRET', json.dumps(detail))
        finally:
            backend.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
