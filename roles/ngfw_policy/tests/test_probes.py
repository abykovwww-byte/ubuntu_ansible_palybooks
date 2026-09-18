import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from test_policy import p

path = Path(__file__).resolve().parents[2] / "ngfw_lab/files/traffic/policy_probe.py"
spec = importlib.util.spec_from_file_location("policy_probe", path)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class Probes(unittest.TestCase):
    def test_port_bounds_and_overlap(self):
        self.assertEqual(probe.parse_ports([[10000, 10002]]), [10000, 10001, 10002])
        for ranges in [[[22, 23]], [[10002, 10000]], [[10000, 10002], [10002, 10005]], [[10000, 21000]]]:
            with self.assertRaises(ValueError):
                probe.parse_ports(ranges)

    def test_nonce_path_is_not_arbitrary_content(self):
        with self.assertRaises(ValueError):
            probe.reply("/policy-probe/not-a-nonce", ("1.1.1.1", 10), ("2.2.2.2", 10000))

    def test_snat_dnat_and_deny_are_distinguished(self):
        plan = p.build_plan(p.configure({}))
        by_tuple = {(c["target"], c["port"]): c for c in plan["probes"]}
        def fetch(target, port):
            if port == 8080:
                return {"local": "10.77.20.10", "peer": "10.77.10.10", "port": port}
            case = by_tuple[target, port]
            if case["expect"] == "drop":
                raise TimeoutError()
            return {"local": "10.77.20.10", "peer": case["peer"], "port": 8080 if case["kind"] == "dnat" else port}
        with patch.object(probe, "fetch", side_effect=fetch):
            result = probe.check(plan)
        self.assertTrue(result["ok"])
        self.assertFalse(result["rule_hits_verified"])
        self.assertTrue(any(r["deny_attribution_inconclusive"] for r in result["results"]))

    def test_no_listener_is_not_accepted_as_firewall_drop(self):
        plan = p.build_plan(p.configure({}))
        plan["probes"] = [c for c in plan["probes"] if c["expect"] == "drop"]
        def fetch(target, port):
            if port == 8080:
                return {"local": "10.77.20.10", "peer": "10.77.10.10", "port": port}
            raise ConnectionRefusedError()
        with patch.object(probe, "fetch", side_effect=fetch):
            self.assertFalse(probe.check(plan)["ok"])

    def test_broken_control_path_cannot_pass_deny(self):
        with patch.object(probe, "fetch", side_effect=TimeoutError()):
            with self.assertRaises(TimeoutError):
                probe.check(p.build_plan(p.configure({})))

    def test_wrong_translated_ip_is_failure(self):
        plan = p.build_plan(p.configure({}))
        plan["probes"] = [c for c in plan["probes"] if c["kind"] == "snat"]
        with patch.object(probe, "fetch", side_effect=lambda target, port: {
                "local": "10.77.20.10", "peer": "10.77.10.10", "port": port}):
            self.assertFalse(probe.check(plan)["ok"])


if __name__ == "__main__":
    unittest.main()
