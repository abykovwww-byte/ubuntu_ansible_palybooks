import io
import json
import os
from pathlib import Path
import socket
import stat
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
import interactive_probe as p
import measurement


SOURCE = Path(p.__file__).with_name('measure_probe.py').read_text(encoding='utf-8')


def baseline():
    return {'boot_id': '12345678-1234-1234-1234-123456789abc', 'observed_seconds': 60,
            'evidence_sha256': 'a'*64, 'cores': [{'cpu': 'cpu1', 'pid': 100,
                'process_start_ticks': 1000, 'process_comm': 'main-thread', 'tid': 110,
                'thread_start_ticks': 1010, 'thread_comm': 'worker', 'affinity': [1]}]}


def response(nonce, clock=1):
    return {'nonce': nonce, 'snapshot': {'schema_version': 1, 'monotonic': clock, 'wall_time': 12345}}


class Protocol(unittest.TestCase):
    def test_config_is_validated_and_bounded_without_executing_fields(self):
        path = Mock()
        config = dict(schema_version=1, process_names=['auditd'], host_temperature_keys=['cpu/package'],
            guest_disk_devices=['vda1'], host_disk_devices=[], temperature_start_c=60,
            temperature_stop_c=80, single_core_pct=90, single_core_seconds=10,
            disk_await_ms=50, disk_await_seconds=10, service_probe_ms=1000,
            service_probe_seconds=10, max_sample_seconds=15, guest_busy_poll_baseline=baseline())
        # No filesystem or credential fixture.
        path.open.side_effect = lambda *args, **kwargs: io.StringIO(json.dumps(config))
        self.assertEqual(p.load_measurement_config(path)['guest_busy_poll_baseline'], baseline())
        config['callback'] = 'do not execute'
        with self.assertRaises(ValueError):
            p.load_measurement_config(path)
        path.open.side_effect = lambda *args, **kwargs: io.StringIO('x'*65537)
        with self.assertRaises(p.ProbeError):
            p.load_measurement_config(path)

    def test_baseline_source_variants_are_pinned_to_fixed_thread_targets(self):
        selected = baseline()
        allowed = p.source_allowlist(SOURCE, ['auditd', 'main-thread'], selected)
        self.assertEqual(len(allowed), 4)
        for names in (None, ['auditd', 'main-thread']):
            for inventory in (False, True):
                rendered = measurement.source(names, inventory, selected)
                digest = p.hashlib.sha256(rendered.encode()).hexdigest()
                options = p.request_options({'nonce': 'a'*32, 'source_sha256': digest}, allowed)
                self.assertEqual(options['thread_targets'], [{'pid': 100, 'tid': 110}])
        # Omitting/changing the pinned targets cannot silently downgrade measurement.
        for rendered in (measurement.source(), measurement.source().replace("'inventory': False", "'inventory': True")):
            digest = p.hashlib.sha256(rendered.encode()).hexdigest()
            with self.assertRaises(p.ProbeError):
                p.request_options({'nonce': 'a'*32, 'source_sha256': digest}, allowed)
        invalid = baseline()
        invalid['cores'][0]['pid'] = '../arbitrary'
        with self.assertRaises(ValueError):
            p.source_allowlist(SOURCE, baseline=invalid)

    def test_allowlist_matches_real_runner_default_and_selected(self):
        allowed = p.source_allowlist(SOURCE, ['auditd', 'vxagent'])
        self.assertEqual(len(allowed), 4)
        for names in (None, ['auditd', 'vxagent']):
            for inventory in (False, True):
                digest = p.hashlib.sha256(measurement.source(names, inventory).encode()).hexdigest()
                request = {'nonce': 'a'*32, 'source_sha256': digest}
                self.assertEqual(p.request_options(request, allowed)['inventory'], inventory)

    def test_arbitrary_source_and_extra_fields_rejected(self):
        allowed = p.source_allowlist(SOURCE)
        digest = p.hashlib.sha256((measurement.source()+'\nprint("do not execute")').encode()).hexdigest()
        with self.assertRaises(p.ProbeError):
            p.request_options({'nonce': 'a'*32, 'source_sha256': digest}, allowed)
        with self.assertRaises(p.ProbeError):
            p.request_options({'nonce': 'a'*32, 'source_sha256': next(iter(allowed)), 'command': 'id'}, allowed)
        for nonce in ('', 'x'*32, True, 'a'*33):
            with self.assertRaises(p.ProbeError):
                p.request_options({'nonce': nonce, 'source_sha256': next(iter(allowed))}, allowed)

    def test_names_and_changed_deployed_source_rejected(self):
        for names in (['$(touch bad)'], ['a'*16], ['auditd\n'], 'auditd', ['x']*65, False):
            with self.assertRaises(p.ProbeError):
                p.source_allowlist(SOURCE, names)
        with self.assertRaises(p.ProbeError):
            p.source_allowlist('missing placeholder')
        with self.assertRaises(p.ProbeError):
            p.source_allowlist('OPTIONS = {}\nOPTIONS = {}')

    def test_nonce_monotonic_and_schema_freshness(self):
        self.assertEqual(p.checked_reply(response('a'*32, 2), 'a'*32, 1)['monotonic'], 2)
        for reply in (response('b'*32), response('a'*32, 1), response('a'*32, float('nan')),
                      {'nonce': 'a'*32, 'snapshot': {'schema_version': 1}},
                      {'nonce': 'a'*32, 'snapshot': response('a'*32)['snapshot'], 'extra': 1}):
            with self.assertRaises(p.ProbeError):
                p.checked_reply(reply, 'a'*32, 1)

    def test_auth_prompts_and_split_ready_marker(self):
        marker = b'NGFW_PROBE_READY_0123456789'
        prompt = b'[NGFW guest sudo] Password: '
        self.assertEqual(p.auth_output(prompt, marker), (prompt, b'', False))
        first = prompt + marker[:12]
        visible, pending, ready = p.auth_output(first, marker)
        self.assertEqual((visible, pending, ready), (prompt, marker[:12], False))
        for ending in (b'\n', b'\r\n'):
            self.assertEqual(p.auth_output(pending+marker[12:]+ending+b'next', marker), (b'', b'next', True))

    def test_ready_framing_all_splits_lf_crlf_and_not_arbitrary_output(self):
        marker = b'NGFW_PROBE_READY_0123456789'
        for ending in (b'\n', b'\r\n'):
            packet = marker + ending
            for split in range(1, len(packet)):
                visible, retained, ready = p.auth_output(b'banner\r\n'+packet[:split], marker)
                self.assertEqual(visible, b'banner\r\n')
                self.assertFalse(ready)
                self.assertEqual(p.auth_output(retained+packet[split:]+b'next', marker), (b'', b'next', True))
        for data in (b'generic READY\r\n', marker+b'-not-the-marker\n', marker.lower()+b'\n'):
            self.assertEqual(p.auth_output(data, marker), (data, b'', False))
        # Neither the unterminated marker nor a split CR means authenticated.
        for data in (marker, marker+b'\r'):
            self.assertEqual(p.auth_output(data, marker), (b'', data, False))

    def test_guest_program_is_fixed_valid_python_with_lifetime_and_root_check(self):
        script = p.guest_program(SOURCE, p.source_allowlist(SOURCE, ['OPTIONS']), 'READY', 600)
        compile(script, '<guest>', 'exec')
        self.assertIn('os.geteuid() != 0', script)
        self.assertIn('signal.setitimer(signal.ITIMER_REAL, LIFETIME)', script)
        self.assertIn("json.dumps(request['options'],sort_keys=True) not in allowed_encoded", script)
        self.assertNotIn("exec(request", script)

    def test_guest_session_rejects_replayed_snapshot(self):
        session = p.GuestSession(SOURCE, p.source_allowlist(SOURCE), 600)
        session.fd = 88
        session.previous = 2
        session.pending = bytearray(json.dumps(response('a'*32, 2)).encode()+b'\r\n')
        with patch.object(p.os, 'write', return_value=1), self.assertRaises(p.ProbeError):
            session.snapshot('a'*32, {'inventory': False, 'process_names': p.DEFAULT_NAMES})


@unittest.skipUnless(sys.platform.startswith('linux'), 'private Unix socket/peer identity needs Linux')
class UnixTransport(unittest.TestCase):
    def test_private_measurement_config_requires_full_validated_schema(self):
        config = dict(schema_version=1, process_names=['auditd'], host_temperature_keys=['cpu/package'],
            guest_disk_devices=['vda1'], host_disk_devices=[], temperature_start_c=60,
            temperature_stop_c=80, single_core_pct=90, single_core_seconds=10,
            disk_await_ms=50, disk_await_seconds=10, service_probe_ms=1000,
            service_probe_seconds=10, max_sample_seconds=15, guest_busy_poll_baseline=baseline())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'measurement.json'
            path.write_text(json.dumps(config))
            self.assertEqual(p.load_measurement_config(path)['guest_busy_poll_baseline'], baseline())
            config['unknown'] = 'not accepted'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                p.load_measurement_config(path)

    def test_idle_guest_eof_or_unexpected_output_stops_broker_promptly(self):
        for data in (b'', b'unexpected guest diagnostic'):
            endpoint = p.PrivateEndpoint()
            endpoint.start()
            read_fd, write_fd = os.pipe()
            stopped = []
            session = SimpleNamespace(fd=read_fd, pending=bytearray())
            def run():
                try:
                    p.serve(endpoint, session, {}, 60)
                except p.ProbeError as exc:
                    stopped.append(str(exc))
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                if data:
                    os.write(write_fd, data)
                os.close(write_fd)
                write_fd = None
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertEqual(len(stopped), 1)
                self.assertIn('guest', stopped[0])
            finally:
                if write_fd is not None:
                    os.close(write_fd)
                os.close(read_fd)
                endpoint.close()

    def test_real_pty_fixed_guest_loop_two_samples_and_unknown_options_eof(self):
        # Synthetic child, not SSH or a real NGFW. Only child UID check is stubbed.
        import pty
        synthetic = ("import time\nOPTIONS = {}\n"
                     "def snapshot(options):\n"
                     "    return {'schema_version':1,'monotonic':time.monotonic(),'wall_time':time.time()}\n")
        allowed = p.source_allowlist(synthetic)
        session = p.GuestSession(synthetic, allowed, 60)
        pid, fd = pty.fork()
        if pid == 0:
            os.geteuid = lambda: 0
            try:
                exec(compile(session.program, '<synthetic-guest>', 'exec'), {})
            except BaseException:
                os._exit(1)
            os._exit(0)
        session.pid, session.fd = pid, fd
        try:
            deadline = time.monotonic()+3
            ready = bytearray()
            while b'\n' not in ready:
                if not p.select.select([fd], [], [], max(0, deadline-time.monotonic()))[0]:
                    self.fail('synthetic PTY bootstrap not ready')
                ready.extend(os.read(fd, 4096))
            self.assertEqual(ready.strip(), session.marker.encode())
            options = next(iter(allowed.values()))
            first = session.snapshot('a'*32, options)
            second = session.snapshot('b'*32, options)
            self.assertGreater(second['snapshot']['monotonic'], first['snapshot']['monotonic'])
            with self.assertRaises((OSError, p.ProbeError)):
                session.snapshot('c'*32, {'inventory': False, 'process_names': ['not-allowed']})
        finally:
            session.close()

    def test_endpoint_exact_cleanup_preserves_unowned_file(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        root = endpoint.root
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(endpoint.socket_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(endpoint.argv_path.stat().st_mode), 0o600)
        argv = json.loads(endpoint.argv_path.read_text())
        self.assertEqual(argv[-2:], ['python3', '-'])
        other = root/'operator-note'
        other.write_text('keep')
        endpoint.close()
        self.assertFalse(endpoint.socket_path.exists())
        self.assertFalse(endpoint.argv_path.exists())
        self.assertEqual(other.read_text(), 'keep')
        other.unlink()
        root.rmdir()

    def test_endpoint_cleanup_does_not_follow_replacement(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        original = endpoint.argv_path
        original.unlink()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'keep'
            target.write_text('preserve')
            original.symlink_to(target)
            endpoint.close()
            self.assertEqual(target.read_text(), 'preserve')
            self.assertTrue(original.is_symlink())
            original.unlink()
            endpoint.root.rmdir()

    def test_complete_endpoint_cleanup_is_idempotent(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        endpoint.close()
        endpoint.close()
        self.assertFalse(endpoint.root.exists())

    def test_endpoint_cleanup_preserves_operator_modified_argv(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        time.sleep(0.01)
        endpoint.argv_path.write_text('operator replacement')
        endpoint.close()
        self.assertEqual(endpoint.argv_path.read_text(), 'operator replacement')
        endpoint.argv_path.unlink()
        endpoint.root.rmdir()

    def test_socket_reader_eof_size_extra_data_and_deadline(self):
        for payload, maximum in ((b'', 100), (b'x'*101, 100), (b'{}\nextra', 100)):
            left, right = socket.socketpair()
            with left, right:
                if payload:
                    right.sendall(payload)
                right.shutdown(socket.SHUT_WR)
                with self.assertRaises(p.ProbeError):
                    p.recv_line(left, maximum, time.monotonic()+1)
        left, right = socket.socketpair()
        with left, right, self.assertRaises(p.ProbeError):
            p.recv_line(left, 100, time.monotonic()-1)

    def test_client_checks_fresh_nonce_and_returns_only_snapshot(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        errors = []
        def responder():
            try:
                conn, _ = endpoint.listener.accept()
                with conn:
                    request = p.recv_line(conn, 2048, time.monotonic()+1)
                    self.assertEqual(set(request), {'nonce', 'source_sha256'})
                    conn.sendall(json.dumps(response(request['nonce'], 3)).encode()+b'\n')
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=responder, daemon=True)
        worker.start()
        try:
            row = p.client(endpoint.socket_path, measurement.source().encode())
            self.assertEqual(row['monotonic'], 3)
            self.assertNotIn('nonce', row)
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
        finally:
            endpoint.close()

    def test_client_rejects_nonprivate_socket(self):
        endpoint = p.PrivateEndpoint()
        endpoint.start()
        try:
            endpoint.socket_path.chmod(0o666)
            with self.assertRaises(p.ProbeError):
                p.client(endpoint.socket_path, measurement.source().encode())
        finally:
            endpoint.socket_path.chmod(0o600)
            endpoint.remember(endpoint.socket_path)
            endpoint.close()


if __name__ == '__main__':
    unittest.main()
