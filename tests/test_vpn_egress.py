import base64
import dataclasses
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "roles/vpn_egress/files/gateway.py"
spec = importlib.util.spec_from_file_location("vpn_gateway", RUNTIME)
gateway = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gateway
spec.loader.exec_module(gateway)


def uri(country="DE", **changes):
    values = {"security": "tls", "type": "ws", "sni": "fixture.example", "host": "fixture.example", "path": "/ws"}
    values.update(changes)
    label = "".join(chr(ord(c) - 65 + 0x1F1E6) for c in country)
    return "vless://00000000-0000-4000-8000-000000000001@fixture.example:443?" + gateway.urllib.parse.urlencode(values) + "#" + label


class SubscriptionTests(unittest.TestCase):
    def test_plain_base64_dedup_and_no_private_labels(self):
        text = (uri() + "\n" + uri() + " PRIVATE LABEL").encode()
        plain, rejected = gateway.parse_subscription(text)
        encoded, _ = gateway.parse_subscription(base64.b64encode(text))
        self.assertEqual(plain, encoded)
        self.assertEqual(len(plain), 1)
        self.assertEqual(plain[0].country, "DE")
        self.assertEqual(rejected, 0)
        self.assertNotIn("PRIVATE LABEL", repr(plain))

    def test_insecure_and_unexpected_transports_are_not_enabled(self):
        for changes in ({"security": "none"}, {"allowInsecure": "1"}, {"type": "xhttp"}, {"encryption": "unexpected"}, {"path": "/\r\ninjected"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                gateway.parse_subscription(uri(**changes).encode())
        nodes, rejected = gateway.parse_subscription((uri() + "\n" + uri(security="none")).encode())
        self.assertEqual((len(nodes), rejected), (1, 1))

    def test_other_countries_are_tried_first(self):
        # Endpoint identities differ even when provider country labels match.
        nodes = [gateway.Node(str(i), c, {}) for i, c in enumerate(["DE", "DE", "NL", "NL", "FR"])]
        ordered = gateway.candidate_order(nodes, nodes[0])
        self.assertEqual([n.country for n in ordered], ["NL", "FR", "DE", "NL"])
        self.assertNotIn(nodes[0], ordered)

    def test_internal_proxy_needs_no_password_and_has_only_vless_outbound(self):
        node = gateway.parse_subscription(uri().encode())[0][0]
        config = gateway.xray_config(node, 1081, serve_clients=True)
        self.assertEqual([o["protocol"] for o in config["outbounds"]], ["vless"])
        self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
        self.assertEqual(config["inbounds"][1]["settings"]["auth"], "noauth")
        self.assertEqual(config["inbounds"][2]["settings"], {})
        self.assertNotIn("accounts", json.dumps(config))
        self.assertEqual(len(gateway.xray_config(node, 1081)["inbounds"]), 1)
        self.assertFalse(node.outbound["streamSettings"]["tlsSettings"]["allowInsecure"])

    def test_pool_events_are_durable_and_not_repeated_each_scan(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch("builtins.print"):
            state = gateway.State(directory)
            state.update(state="all_down")
            outage = state.snapshot()["outage_id"]
            state.update(phase="scanning")
            state.update(state="all_down")
            self.assertEqual(len(state.snapshot()["events"]), 1)
            restored = gateway.State(directory)
            restored.update(state="healthy", country="FR")
            events = restored.snapshot()["events"]
            self.assertEqual([e["kind"] for e in events], ["pool_down", "pool_recovered"])
            self.assertEqual([e["outage_id"] for e in events], [outage, outage])
            restored.update(state="healthy")
            self.assertEqual(len(restored.snapshot()["events"]), 2)
            self.assertNotIn("outbound", json.dumps(restored.snapshot()))

    def test_probe_does_not_inherit_proxy_bypass_or_disable_tls(self):
        worker = object.__new__(gateway.Worker)
        worker.process = mock.Mock()
        worker.process.poll.return_value = None
        worker.port = 1081
        with mock.patch.object(gateway.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"302")) as run:
            self.assertTrue(worker.healthy())
            args = run.call_args.args[0]
            self.assertEqual(args[args.index("--noproxy") + 1], "")
            self.assertIn("socks5h://127.0.0.1:1081", args)
            self.assertNotIn("--insecure", args)
            self.assertNotIn("--location", args)
        with mock.patch.object(gateway.subprocess, "run", return_value=mock.Mock(returncode=7, stdout=b"302")):
            self.assertFalse(worker.healthy())

    def test_full_pool_failure_then_country_recovery(self):
        import threading
        nodes = [gateway.Node(str(i), c, {}) for i, c in enumerate(["DE", "DE", "NL", "FR"])]
        with tempfile.TemporaryDirectory() as directory, mock.patch("builtins.print"):
            state = gateway.State(directory)
            with mock.patch.object(gateway, "probe", return_value=False) as probe:
                selected, worker = gateway.choose_worker(nodes, nodes[0], state, threading.Event())
                self.assertEqual((selected, worker), (None, None))
                self.assertEqual(probe.call_count, len(nodes))
                self.assertEqual(state.snapshot()["state"], "all_down")
            fake = mock.Mock()
            fake.ready.return_value = fake.healthy.return_value = True
            with mock.patch.object(gateway, "probe", side_effect=lambda n: n.country == "FR"), mock.patch.object(gateway, "Worker", return_value=fake):
                selected, worker = gateway.choose_worker(nodes, nodes[0], state, threading.Event())
                self.assertEqual(selected.country, "FR")
                self.assertIs(worker, fake)
                self.assertEqual([e["kind"] for e in state.snapshot()["events"]], ["pool_down", "pool_recovered"])

    def test_successful_candidate_with_failed_promotion_is_not_claimed_healthy(self):
        import threading
        nodes = [gateway.Node("a", "DE", {}), gateway.Node("b", "NL", {})]
        fake = mock.Mock()
        fake.ready.return_value = True
        fake.healthy.return_value = False
        with tempfile.TemporaryDirectory() as directory, mock.patch("builtins.print"):
            state = gateway.State(directory)
            with mock.patch.object(gateway, "probe", return_value=True), mock.patch.object(gateway, "Worker", return_value=fake):
                selected, worker = gateway.choose_worker(nodes, None, state, threading.Event())
                self.assertEqual((selected, worker), (None, None))
                self.assertEqual(fake.close.call_count, 2)
                self.assertEqual(state.snapshot()["state"], "all_down")


@unittest.skipUnless(shutil.which("xray"), "Xray image checks run in CI")
class XrayRuntimeTests(unittest.TestCase):
    def test_actual_pinned_core_accepts_generated_tls_config(self):
        node = gateway.parse_subscription(uri(fp="chrome").encode())[0][0]
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "config.json"
            gateway.write_json(config, gateway.xray_config(node, 1081, serve_clients=True))
            result = subprocess.run(["xray", "run", "-test", "-config", str(config)], capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_real_http_proxy_without_password_and_no_fallback_after_worker_stop(self):
        import http.server
        import threading

        class Target(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"fixture-response")
            def log_message(self, *_):
                pass

        target = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Target)
        threading.Thread(target=target.serve_forever, daemon=True).start()
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            endpoint = reservation.getsockname()[1]
        with tempfile.TemporaryDirectory() as directory:
            server_config = Path(directory) / "server.json"
            gateway.write_json(server_config, {"log": {"loglevel": "none"}, "inbounds": [{"listen": "127.0.0.1", "port": endpoint,
                "protocol": "vless", "settings": {"clients": [{"id": "00000000-0000-4000-8000-000000000001"}], "decryption": "none"}}],
                "outbounds": [{"protocol": "freedom"}]})
            server = subprocess.Popen(["xray", "run", "-config", str(server_config)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            import time
            deadline = time.monotonic() + 5
            while True:
                try:
                    with socket.create_connection(("127.0.0.1", endpoint), timeout=.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        server.terminate()
                        server.wait(timeout=5)
                        self.fail("Synthetic VLESS endpoint did not start")
                    time.sleep(.1)
            node = gateway.parse_subscription(uri().encode())[0][0]
            node.outbound["settings"]["vnext"][0].update(address="127.0.0.1", port=endpoint)
            # Plain VLESS is permitted only on this localhost test fixture.
            node.outbound["streamSettings"] = {"network": "tcp", "security": "none"}
            worker = gateway.Worker(node, 1081, serve_clients=True)
            destination = f"http://127.0.0.1:{target.server_port}/"
            command = ["curl", "--silent", "--noproxy", "", "--max-time", "5", "--proxy", "http://127.0.0.1:8080", destination]
            try:
                self.assertTrue(worker.ready())
                response = subprocess.run(command, capture_output=True)
                self.assertEqual((response.returncode, response.stdout), (0, b"fixture-response"))
                worker.close()
                failed = subprocess.run(command, capture_output=True)
                self.assertNotEqual(failed.returncode, 0)
                # The target remains available directly: failure is the proxy boundary.
                direct = subprocess.run(["curl", "--silent", "--noproxy", "*", destination], capture_output=True)
                self.assertEqual(direct.stdout, b"fixture-response")
            finally:
                if worker.process.poll() is None:
                    worker.close()
                server.terminate()
                server.wait(timeout=5)
                target.shutdown()
                target.server_close()


if __name__ == "__main__":
    unittest.main()
