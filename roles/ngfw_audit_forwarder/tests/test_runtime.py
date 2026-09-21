"""Synthetic file -> actual rsyslog -> TCP. No appliance or real audit logs."""
from pathlib import Path
import socket
import subprocess
import tempfile
import threading
import time
import unittest


class Forwarder(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.audit = self.root / 'audit.log'
        self.audit.write_text('type=USER msg=audit(1.001:1): SYNTHETIC-initial\n')
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(4)
        self.listener.settimeout(.1)
        self.stop = threading.Event()
        self.received = bytearray()
        self.thread = threading.Thread(target=self.receive, daemon=True)
        self.thread.start()
        config = Path('/test-source.conf').read_text()
        config = config.replace('/var/lib/ngfw-audit-forwarder', str(self.root))
        config = config.replace('/var/log/audit/audit.log', str(self.audit))
        config = config.replace('10.77.0.1', '127.0.0.1')
        config = config.replace('port="5514"', f'port="{self.listener.getsockname()[1]}"')
        self.config = self.root / 'sender.conf'
        self.config.write_text(config)
        self.errors = (self.root / 'stderr').open('w+')
        self.process = None

    def receive(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.listener.accept()
            except socket.timeout:
                continue
            with conn:
                conn.settimeout(.1)
                while not self.stop.is_set():
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        break
                    self.received.extend(chunk)

    def start(self):
        subprocess.run(['rsyslogd', '-N1', '-f', str(self.config)], check=True,
                       stdout=subprocess.DEVNULL, stderr=self.errors)
        self.process = subprocess.Popen(['rsyslogd', '-n', '-i', str(self.root / 'sender.pid'),
                                         '-f', str(self.config)], stdout=subprocess.DEVNULL, stderr=self.errors)

    def halt(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            self.process.wait(timeout=8)

    def expect(self, marker):
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if marker.encode() in self.received:
                return
            if self.process.poll() is not None:
                self.fail('rsyslog exited before delivery')
            time.sleep(.05)
        self.fail('synthetic marker not delivered: ' + marker)

    def append(self, marker):
        with self.audit.open('a') as out:
            out.write(f'type=USER msg=audit(1.002:2): {marker}\n')

    def tearDown(self):
        self.halt()
        self.stop.set()
        self.thread.join(timeout=2)
        self.listener.close()
        self.errors.close()
        self.temp.cleanup()

    def test_initial_tail_rotation_and_restart_offsets(self):
        self.start()
        self.expect('SYNTHETIC-initial')
        self.append('SYNTHETIC-tail')
        self.expect('SYNTHETIC-tail')
        self.audit.rename(self.root / 'audit.log.1')
        self.audit.write_text('type=USER msg=audit(1.003:3): SYNTHETIC-rotated\n')
        self.expect('SYNTHETIC-rotated')
        self.halt()
        self.assertTrue(list(self.root.glob('imfile-state:*')))
        before = bytes(self.received).count(b'SYNTHETIC-rotated')
        self.append('SYNTHETIC-offline')
        self.start()
        self.expect('SYNTHETIC-offline')
        self.assertEqual(bytes(self.received).count(b'SYNTHETIC-rotated'), before)
        self.assertIn(b' auditd - - - type=USER', self.received)
        self.assertIn(b'type=USER msg=audit(', self.received)

    def test_unavailable_destination_does_not_block_local_audit_file(self):
        self.stop.set()
        self.thread.join(timeout=2)
        self.listener.close()
        self.start()
        for i in range(5000):
            self.append(f'SYNTHETIC-offline-{i}')
        time.sleep(.5)
        self.assertIsNone(self.process.poll())
        self.assertIn('SYNTHETIC-offline-4999', self.audit.read_text())
        self.halt()


if __name__ == '__main__':
    unittest.main(verbosity=2)
