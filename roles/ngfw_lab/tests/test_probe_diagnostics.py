import io
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

from test_runner import r, e, config, ROOT
from traffic.probe_diagnostics import decode_failure, failure_payload


class ProbeDiagnostics(unittest.TestCase):
    def test_isolation_accepts_actual_endpoint_cli_failure_across_subprocess(self):
        backend = r.Backend(config() | {'campaign_id': 'test-campaign'})
        child = '''import sys
sys.path.insert(0, TRAFFIC)
import endpoint
if '--help' in sys.argv or 'traffic-server' in sys.argv:
    sys.exit(0)
def unreachable(*args, **kwargs):
    error = TimeoutError(110, 'SECRET request')
    error.ngfw_stage = 'http'
    raise error
endpoint.request = unreachable
sys.argv = ['endpoint.py', 'probe']
endpoint.main()
'''.replace('TRAFFIC', repr(str(ROOT / 'files/traffic')))
        backend.compose = [sys.executable, '-c', child]
        try:
            with patch.object(backend, 'state', return_value='shut off'), \
                    patch.object(backend, 'topology', return_value={'test': True}):
                evidence = backend.isolation()
            self.assertEqual(evidence['campaign_id'], 'test-campaign')
            self.assertEqual(evidence['client_to_receiver'], 'unreachable')
            self.assertEqual(evidence['client_probe_error'],
                             {'stage': 'http', 'exception_class': 'TimeoutError', 'errno': 110})
            self.assertNotIn('SECRET', json.dumps(evidence))
        finally:
            backend.pool.shutdown(wait=True)

    def test_isolation_rejects_success_command_failure_and_non_network_errors(self):
        valid = json.dumps(failure_payload('http', TimeoutError(110, 'SECRET')))
        cases = [(0, ''), (125, valid), (1, ''), (1, 'SECRET TimeoutError traceback'),
                 (1, json.dumps(failure_payload('http', ValueError('SECRET')))),
                 (1, json.dumps(failure_payload('http', OSError(13, 'SECRET')))),
                 (1, json.dumps(failure_payload('http', ConnectionResetError(104, 'SECRET')))),
                 (1, json.dumps(failure_payload('dns', TimeoutError(110, 'SECRET'))))]
        backend = r.Backend(config())
        try:
            for code, output in cases:
                with self.subTest(code=code, output=output), \
                        patch.object(backend, 'state', return_value='shut off'), \
                        patch.object(backend, 'probe_endpoint'), patch.object(backend, 'endpoint'), \
                        patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess(
                            ['docker'], code, output, 'SECRET TimeoutError')):
                    with self.assertRaises(r.Abort) as stopped:
                        backend.isolation()
                    self.assertNotIn('SECRET', str(stopped.exception))
            with patch.object(backend, 'state', side_effect=['shut off', 'running']), \
                    patch.object(backend, 'probe_endpoint'), patch.object(backend, 'endpoint'), \
                    patch.object(r.subprocess, 'run', return_value=subprocess.CompletedProcess(
                        ['docker'], 1, valid, '')):
                with self.assertRaisesRegex(r.Abort, 'changed state'):
                    backend.isolation()
        finally:
            backend.pool.shutdown(wait=True)

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
        def command(argv, **kwargs):
            if 'probe' in argv:
                return subprocess.CompletedProcess(argv, 1,
                    json.dumps(failure_payload('http', TimeoutError(110, 'SECRET'))), 'SECRET traceback')
            return subprocess.CompletedProcess(argv, 0, '', '')
        try:
            with patch.object(r.subprocess, 'run', side_effect=command), \
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
                 (subprocess.SubprocessError('SECRET'), 'SubprocessError'),
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

    def test_real_call_chain_rejects_unstructured_output_and_remains_an_abort(self):
        backend = r.Backend(config())
        try:
            result = subprocess.CompletedProcess(['docker'], 125, 'SECRET output', 'SECRET stderr')
            with patch.object(r.subprocess, 'run', return_value=result):
                with self.assertRaises(r.Abort) as stopped:
                    backend.call(['docker', 'SECRET argument'])
                self.assertIsInstance(stopped.exception, r.CommandFailure)
                self.assertEqual(str(stopped.exception), 'docker check failed (exit 125)')
                self.assertIsNone(stopped.exception.request_error)
                self.assertNotIn('SECRET', repr(vars(stopped.exception)))
                with self.assertRaises(r.ProbeFailure) as probe:
                    backend.probe_endpoint('traffic-client', '10.77.20.10')
            detail = probe.exception.diagnostic
            self.assertEqual(detail['layer'], 'command')
            self.assertEqual(detail['exception_class'], 'CommandFailure')
            self.assertEqual(detail['returncode'], 125)
            self.assertNotIn('SECRET', json.dumps(detail))
        finally:
            backend.pool.shutdown(wait=True)

    def test_failure_crosses_an_actual_subprocess_without_mocking_backend(self):
        backend = r.Backend(config())
        payload = json.dumps(failure_payload('http', ConnectionRefusedError(111, 'SECRET')))
        backend.compose = [sys.executable, '-c',
            'import sys; print(' + repr(payload) + '); sys.stderr.write("SECRET stderr"); sys.exit(1)']
        try:
            with self.assertRaises(r.ProbeFailure) as stopped:
                backend.probe_endpoint('traffic-client', '127.0.0.1')
            detail = stopped.exception.diagnostic
            self.assertEqual(detail['layer'], 'request')
            self.assertEqual(detail['exception_class'], 'ConnectionRefusedError')
            self.assertEqual(detail['stage'], 'http')
            self.assertEqual(detail['errno'], 111)
            self.assertEqual(detail['returncode'], 1)
            self.assertNotIn('SECRET', json.dumps(detail))
        finally:
            backend.pool.shutdown(wait=True)


if __name__ == '__main__':
    unittest.main()
