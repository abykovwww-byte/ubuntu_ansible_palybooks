import json
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files" / "traffic"))
import held_receiver as held


class HeldReceiverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.json"
        self.receiver = held.Receiver("127.0.0.1", 0, self.path, idle_timeout=.25)

    def tearDown(self):
        self.receiver.close()
        self.tmp.cleanup()

    def connect(self):
        return socket.create_connection(("127.0.0.1", self.receiver.port), 2)

    def frame(self, seq=0, run=1, cid=0):
        return held.FRAME.pack(held.MAGIC, run, cid, seq, time.monotonic_ns())

    def exchange(self, sock, data):
        sock.sendall(data)
        result = b""
        while len(result) < len(data):
            part = sock.recv(len(data) - len(result))
            if not part:
                break
            result += part
        return result

    def test_legacy_echo_still_closes(self):
        with self.connect() as sock:
            self.assertEqual(self.exchange(sock, held.LEGACY), held.LEGACY)
            self.assertEqual(sock.recv(1), b"")

    def test_multiple_payloads_share_one_connection(self):
        with self.connect() as sock:
            for seq in range(3):
                frame = self.frame(seq)
                self.assertEqual(self.exchange(sock, frame), frame)
        self.receiver.close()
        row = json.loads(self.path.read_text())
        self.assertEqual(row["runs"]["1"]["connections"], 1)
        self.assertEqual(row["runs"]["1"]["messages"], 3)
        self.assertEqual(row["active"], 0)
        self.assertEqual(row["status"], "stopped")

    def test_identity_cannot_change(self):
        with self.connect() as sock:
            frame = self.frame()
            self.assertEqual(self.exchange(sock, frame), frame)
            self.assertEqual(self.exchange(sock, self.frame(1, run=2)), b"")

    def test_idle_connection_closes(self):
        with self.connect() as sock:
            frame = self.frame()
            self.assertEqual(self.exchange(sock, frame), frame)
            self.assertEqual(sock.recv(1), b"")

    def test_capacity_rejects_extra_connection(self):
        self.receiver.close()
        self.receiver = held.Receiver("127.0.0.1", 0, self.path, max_connections=1)
        with self.connect() as first:
            frame = self.frame()
            self.assertEqual(self.exchange(first, frame), frame)
            with self.connect() as extra:
                self.assertEqual(extra.recv(1), b"")
            frame = self.frame(1)
            self.assertEqual(self.exchange(first, frame), frame)

    def test_duplicate_id_does_not_inflate_count(self):
        with self.connect() as first:
            frame = self.frame()
            self.assertEqual(self.exchange(first, frame), frame)
            with self.connect() as second:
                self.assertEqual(self.exchange(second, frame), b"")
        self.receiver.close()
        row = json.loads(self.path.read_text())
        self.assertEqual(row["runs"]["1"]["connections"], 1)
        self.assertEqual(row["errors"], 1)

    def test_run_limit_keeps_legacy_available(self):
        self.receiver.close()
        self.receiver = held.Receiver("127.0.0.1", 0, self.path, max_runs=1)
        with self.connect() as sock:
            frame = self.frame()
            self.assertEqual(self.exchange(sock, frame), frame)
        with self.connect() as sock:
            self.assertEqual(self.exchange(sock, self.frame(run=2)), b"")
        with self.connect() as sock:
            self.assertEqual(self.exchange(sock, held.LEGACY), held.LEGACY)

if __name__ == "__main__":
    unittest.main()