"""Linux image integration. Run inside the built traffic image in CI, on loopback."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import unittest
import uuid


TRAFFIC = Path("/opt/traffic")
sys.path.insert(0, str(TRAFFIC))
import policy_probe
spec = importlib.util.spec_from_file_location("endpoint", TRAFFIC / "endpoint.py")
endpoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(endpoint)


class Runtime(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = subprocess.Popen([sys.executable, str(TRAFFIC / "endpoint.py"), "server"],
                                     env={**os.environ, "NGFW_POLICY_LISTEN_RANGES": "[[10000,10002],[20000,20002]]"})
        for _ in range(50):
            try:
                endpoint.request("http", "127.0.0.1")
                return
            except OSError:
                time.sleep(.1)
        raise RuntimeError("receiver did not become healthy")

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(timeout=10)

    def test_real_protocols(self):
        for kind in ("short-tcp", "http", "dns"):
            with self.subTest(kind=kind):
                result = endpoint.workload(kind, "127.0.0.1", 1, 20, 2)
                self.assertEqual(result["errors"], 0)
                self.assertGreater(result["successful_requests"], 0)

    def test_policy_probe_observes_actual_tuple(self):
        for port in [8080, 10000, 10001, 10002, 20000, 20002]:
            result = policy_probe.fetch("127.0.0.1", port)
            self.assertEqual(result["local"], "127.0.0.1")
            self.assertEqual(result["peer"], "127.0.0.1")
            self.assertEqual(result["port"], port)

    def test_thousand_listeners_share_one_thread(self):
        receiver = policy_probe.Receiver([[11000, 11999], [20100, 20199]], bind="127.0.0.1")
        try:
            self.assertEqual(len(receiver.listeners), 1100)
            for port in [11000, 11500, 11999, 20100, 20199]:
                self.assertEqual(policy_probe.fetch("127.0.0.1", port)["port"], port)
            self.assertIsNone(receiver.error)
        finally:
            receiver.close()

    def test_listener_command_checks_receiver_independently(self):
        result = subprocess.run([sys.executable, str(TRAFFIC / "policy_probe.py"), "listeners"],
            input=json.dumps({"receiver_ranges": [[10000, 10002], [20000, 20002]]}),
            capture_output=True, text=True, check=True, timeout=10)
        self.assertEqual(json.loads(result.stdout)["listener_count"], 6)

    def test_real_iperf_tcp_udp(self):
        for flags in ([], ["-u", "-l", "64"]):
            result = subprocess.run(["iperf3", "-c", "127.0.0.1", "-t", "1", "-b", "1M", "-J"] + flags,
                                    capture_output=True, text=True, timeout=10, check=True)
            data = json.loads(result.stdout)
            self.assertNotIn("error", data)
            self.assertIn("end", data)

    def start_job(self, timeout):
        job = uuid.uuid4().hex
        proc = subprocess.Popen([sys.executable, str(TRAFFIC / "job.py"), "run", "--timeout", str(timeout),
                                 job, "--", "python3", "-c", "import time; time.sleep(30)"])
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return job, proc

    def test_watchdog_kills_detached_workload(self):
        job, proc = self.start_job(1)
        self.assertEqual(proc.wait(timeout=5), 124)
        self.assertFalse(Path("/tmp", "ngfw-" + job + ".json").exists())

    def test_stop_kills_only_named_job(self):
        job, proc = self.start_job(10)
        marker = Path("/tmp", "ngfw-" + job + ".json")
        for _ in range(50):
            if marker.exists() and marker.stat().st_size:
                break
            time.sleep(.05)
        subprocess.run([sys.executable, str(TRAFFIC / "job.py"), "stop", job], check=True)
        self.assertNotEqual(proc.wait(timeout=5), 0)
        self.assertIsNone(self.server.poll())
        endpoint.request("http", "127.0.0.1")

    def test_stop_before_exec_start_prevents_late_load(self):
        job = uuid.uuid4().hex
        subprocess.run([sys.executable, str(TRAFFIC / "job.py"), "stop", job], check=True)
        result = subprocess.run([sys.executable, str(TRAFFIC / "job.py"), "run", "--timeout", "10",
                                 job, "--", "python3", "-c", "raise SystemExit(99)"], timeout=3)
        self.assertEqual(result.returncode, 125)


if __name__ == "__main__":
    unittest.main()
