"""Run INSIDE the actual unprivileged receiver image, with isolated CI tmpfs data."""
import gzip
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
import uuid

sys.path.insert(0, '/opt/collector')
import collector as c


def eventually(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.1)
    raise AssertionError('condition did not become true')


class Runtime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.child = subprocess.Popen([sys.executable, '/opt/collector/collector.py', 'serve'])
        eventually(lambda: (c.RUNTIME / 'rotation-ok').exists() and c.ctl('stats') == 0)

    @classmethod
    def tearDownClass(cls):
        cls.child.terminate()
        cls.child.wait(timeout=15)
        if cls.child.returncode != 0:
            raise AssertionError('collector did not shut down cleanly')

    def send(self, port, text, transport='tcp', source='127.0.0.1'):
        kind = socket.SOCK_STREAM if transport == 'tcp' else socket.SOCK_DGRAM
        with socket.socket(socket.AF_INET, kind) as sock:
            sock.settimeout(2)
            sock.bind((source, 0))
            sock.connect(('127.0.0.1', port))
            sock.sendall(text.encode() + (b'\n' if transport == 'tcp' else b''))

    def rows(self, stream):
        path = c.DATA / stream / 'events.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_01_tcp_udp_both_streams_and_json_escaping(self):
        for stream, port in [('auditd', 5514), ('ngfw', 5515)]:
            for transport in ('tcp', 'udp'):
                nonce = uuid.uuid4().hex
                raw = '<134>1 2026-01-01T00:00:00Z synthetic app - - - CEF:0|TEST|LAB|1|x|"' + nonce + '\\|1|'
                if transport == 'udp':
                    raw += '\nsecond line\tvalue'
                self.send(port, raw, transport)
                eventually(lambda: any(nonce in row['raw'] for row in self.rows(stream)))
                row = next(row for row in self.rows(stream) if nonce in row['raw'])
                self.assertEqual(row['raw'], raw)
                self.assertEqual(row['source_ip'], '127.0.0.1')
                self.assertEqual(row['stream'], stream)
                self.assertTrue(row['received_at'])
                other = 'ngfw' if stream == 'auditd' else 'auditd'
                self.assertFalse(any(nonce in row['raw'] for row in self.rows(other)))

    def test_02_unlisted_sender_is_not_written(self):
        nonce = uuid.uuid4().hex
        for port in (5514, 5515):
            for transport in ('tcp', 'udp'):
                self.send(port, '<134>synthetic denied ' + nonce, transport, source='127.0.0.2')
        time.sleep(.5)
        self.assertFalse(any(nonce in row['raw'] for stream in ('auditd', 'ngfw') for row in self.rows(stream)))

    def test_03_no_root_capabilities_and_health(self):
        self.assertEqual(os.getuid(), 10001)
        status = Path('/proc/self/status').read_text()
        self.assertIn('CapEff:\t0000000000000000', status)
        self.assertEqual(c.health(), 0)
        for stream in ('auditd', 'ngfw'):
            self.assertEqual((c.DATA / stream / 'events.jsonl').stat().st_mode & 0o777, 0o600)
        with self.assertRaises(OSError):
            Path('/forbidden-write').write_text('no')

    def test_04_rotate_reopen_compress_and_retention(self):
        for index in range(4):
            nonce = uuid.uuid4().hex
            self.send(5514, '<134>synthetic rotation ' + nonce)
            eventually(lambda: any(nonce in row['raw'] for row in self.rows('auditd')))
            subprocess.run(['/usr/sbin/logrotate', '--force', '--state', str(c.STATE / 'logrotate.status'),
                            str(c.RUNTIME / 'logrotate.conf')], check=True, timeout=10)
        nonce = uuid.uuid4().hex
        self.send(5514, '<134>synthetic after rotation ' + nonce)
        eventually(lambda: any(nonce in row['raw'] for row in self.rows('auditd')))
        self.assertTrue((c.DATA / 'auditd/events.jsonl.1').is_file())
        archive = c.DATA / 'auditd/events.jsonl.2.gz'
        self.assertTrue(archive.is_file())
        with gzip.open(archive, 'rt') as file:
            self.assertIn('rotation', json.loads(file.readline())['raw'])
        self.assertFalse(list((c.DATA / 'auditd').glob('events.jsonl.3*')))
        self.assertEqual(c.health(), 0)

    def test_05_health_detects_stale_rotation(self):
        marker = c.RUNTIME / 'rotation-ok'
        before = marker.stat().st_mtime
        try:
            os.utime(marker, (time.time() - 100, time.time() - 100))
            self.assertEqual(c.health(), 1)
        finally:
            os.utime(marker, (before, before))

    def test_06_supervisor_fails_if_daemon_dies(self):
        # A second instance on separate runtime paths would be misleading; verify
        # the primary process exits nonzero, then restart it for clean tearDown.
        pid = int((c.RUNTIME / 'syslog-ng.pid').read_text())
        os.kill(pid, signal.SIGKILL)
        self.child.wait(timeout=10)
        self.assertNotEqual(self.child.returncode, 0)
        type(self).child = subprocess.Popen([sys.executable, '/opt/collector/collector.py', 'serve'])
        eventually(lambda: c.ctl('stats') == 0)
        eventually(lambda: c.health() == 0)


if __name__ == '__main__':
    unittest.main()
