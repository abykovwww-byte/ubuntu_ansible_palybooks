import copy
import contextlib
import hashlib
import json
import io
from pathlib import Path
import sys
import tempfile
import unittest
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import native_health as health

BOOT = "11111111-2222-4333-8444-555555555555"


class FakeClock:
    def __init__(self):
        self.elapsed = 0
        self.wall_base = 1791811000_000000000
        self.mono_base = 1000_000000000

    def time_ns(self):
        self.elapsed += 1000000
        return self.wall_base+self.elapsed

    def monotonic_ns(self):
        self.elapsed += 1000000
        return self.mono_base+self.elapsed


class NativeHealthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = FakeClock()
        self.clock_calls = 0
        self.queries = []
        self.source_age_ns = 15_000000000
        self.offset_ns = 22_000000000
        observation = {"results": [{"series": [{"name": "native_measurement", "tags": {"host": "verified-lab-host"},
            "columns": ["time", "packets"], "values": [[self.clock.wall_base-self.offset_ns, 7]]}]}]}
        self.observation = self.write_ref("observed.json", observation)
        interval_ref = self.write_ref("source-interval.json", {"observed_interval": "10s"})
        interval_ref["json_pointer"] = "/observed_interval"
        self.catalog = {"schema_version": 1, "database": "actual_db", "source_interval_seconds": 10,
            "host_selector": {"tag": "host", "value": "verified-lab-host"},
            "measurements": [{"name": "native_measurement", "field": "packets"}],
            "observation_evidence": self.observation, "interval_evidence": interval_ref}
        self.catalog_path = self.root / "catalog.json"
        self.catalog_path.write_bytes(health.encode(self.catalog))
        self.config = {"influx_url": "http://127.0.0.1:18087"}
        self.output = self.root / "health.json"

    def tearDown(self):
        self.temp.cleanup()

    def write_ref(self, name, value):
        payload = health.encode(value)
        path = self.root / name
        path.write_bytes(payload)
        return {"path": str(path), "sha256": hashlib.sha256(payload).hexdigest()}

    def clock_provider(self):
        self.clock_calls += 1
        return health.encode({"guest_epoch_ns": self.clock.time_ns()-self.offset_ns,
                              "guest_monotonic_ns": self.clock.monotonic_ns()-100_000000000,
                              "guest_boot_id": BOOT})

    def get(self, url, timeout, cap):
        self.queries.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query))
        timestamp = self.clock.time_ns()-self.offset_ns-self.source_age_ns+1
        self.last_source_timestamp = timestamp
        return 200, health.encode({"results": [{"series": [{"name": "native_measurement",
            "columns": ["time", "value"], "values": [[timestamp, 0]]}]}]})

    def collect(self, get=None, provider=None):
        return health.collect(self.config, self.catalog_path, self.output, self.root/"evidence",
                              provider or self.clock_provider, get or self.get, self.clock)

    def read_ref(self, ref):
        payload = Path(ref["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(payload).hexdigest(), ref["sha256"])
        return json.loads(payload)

    def test_skew_alignment_preserves_original_nanosecond_timestamp(self):
        receipt = self.collect()
        self.assertEqual(receipt["status"], "HEALTHY", receipt)
        raw = self.read_ref(receipt["query_evidence"])
        alignment = self.read_ref(receipt["clock_alignment"])
        point = raw["points"][0]
        self.assertEqual(point["source_timestamp_ns"], self.last_source_timestamp)
        self.assertEqual(point["time"], health.rfc3339_ns(self.last_source_timestamp))
        self.assertEqual(point["value"], 0)
        self.assertGreater(raw["evaluated_at_epoch_ns"]-point["source_timestamp_ns"], 30_000000000)
        self.assertLess(receipt["aligned_age_intervals"][0]["age_max_ns"], 16_000000000)
        self.assertEqual(alignment["query_evidence_sha256"], receipt["query_evidence"]["sha256"])
        self.assertEqual(alignment["query_bounds"], raw["query_bounds"])
        self.assertTrue(raw["api_requests"][0]["complete"])
        actual_response = self.read_ref(raw["api_requests"][0]["response_evidence"])
        self.assertEqual(actual_response["results"][0]["series"][0]["values"][0][0], self.last_source_timestamp)
        self.assertEqual(self.queries[0]["epoch"], ["ns"])
        self.assertNotIn("SELECT *", self.queries[0]["q"][0])
        self.assertIn('LAST("packets")', self.queries[0]["q"][0])
        self.assertIn('"host" = \'verified-lab-host\'', self.queries[0]["q"][0])

    def test_missing_partial_and_lossy_api_responses_fail_closed(self):
        cases = [
            {"results": [{}]},
            {"results": [{"partial": True}]},
            {"partial": True, "results": [{}]},
            {"results": [{"series": [{"name": "native_measurement", "columns": ["time", "value"], "values": [[1.791e18, 1]]}]}]},
        ]
        for document in cases:
            with self.subTest(document=document):
                result = self.collect(get=lambda *_: (200, health.encode(document)))
                self.assertEqual(result["status"], "BLOCKED")
                self.assertTrue(self.read_ref(result["query_evidence"])["partial"])

    def test_truncated_body_preserved_and_rejected(self):
        body = b"x"*65537
        result = self.collect(get=lambda *_: (200, body))
        self.assertEqual(result["status"], "BLOCKED")
        request = self.read_ref(result["query_evidence"])["api_requests"][0]
        self.assertTrue(request["truncated"])
        self.assertEqual(Path(request["response_evidence"]["path"]).read_bytes(), body)

    def test_changed_boot_retains_failed_alignment(self):
        def provider():
            sample = json.loads(self.clock_provider())
            if self.clock_calls == 2:
                sample["guest_boot_id"] = "22222222-2222-4333-8444-555555555555"
            return health.encode(sample)
        result = self.collect(provider=provider)
        self.assertEqual(result["reason"], "guest_boot_changed")
        self.assertNotEqual(self.read_ref(result["clock_alignment"])["before"]["guest_boot_id"], self.read_ref(result["clock_alignment"])["after"]["guest_boot_id"])

    def test_guest_clock_step_rejected(self):
        def provider():
            sample = json.loads(self.clock_provider())
            if self.clock_calls == 2:
                sample["guest_epoch_ns"] += 1_000000000
            return health.encode(sample)
        result = self.collect(provider=provider)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "clock_step_across_query")

    def test_stale_and_future_measurements_rejected_with_original_evidence(self):
        for age in (40_000000000, -5_000000000):
            self.source_age_ns = age
            result = self.collect()
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(result["reason"], "required_measurement_stale_or_future_after_alignment")
            self.assertEqual(self.read_ref(result["query_evidence"])["points"][0]["source_timestamp_ns"], self.last_source_timestamp)

    def test_catalog_cannot_claim_unobserved_host_field_or_interval(self):
        variants = [copy.deepcopy(self.catalog) for _ in range(3)]
        variants[0]["host_selector"]["value"] = "unobserved-host"
        variants[1]["measurements"][0]["field"] = "unobserved_field"
        variants[2]["source_interval_seconds"] = 5
        for variant in variants:
            self.catalog_path.write_bytes(health.encode(variant))
            result = self.collect()
            self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(self.queries, [])

    def test_catalog_evidence_tampering_rejected(self):
        Path(self.observation["path"]).write_bytes(b"changed")
        result = self.collect()
        self.assertEqual(result["reason"], "source_evidence_digest_mismatch")

    def test_failed_poll_atomically_replaces_latest_but_retains_prior_artifacts(self):
        success = self.collect()
        old_path = Path(success["query_evidence"]["path"])
        old_bytes = old_path.read_bytes()
        failure = self.collect(get=lambda *_: (503, b"unavailable"))
        self.assertEqual(json.loads(self.output.read_bytes())["status"], "BLOCKED")
        self.assertEqual(old_path.read_bytes(), old_bytes)
        self.assertNotEqual(failure["query_evidence"], success["query_evidence"])

    def test_wider_clock_limits_are_not_permitted(self):
        self.config["max_offset_spread_seconds"] = 1
        result = self.collect()
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "invalid_max_offset_spread")

    def test_offset_envelope_rejects_large_rtt_even_without_clock_step(self):
        def provider():
            sample = self.clock_provider()
            self.clock.elapsed += 600_000000
            return sample
        result = self.collect(provider=provider)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "offset_envelope_exceeds_limit")

    def test_recent_boot_cannot_reuse_preboot_native_points(self):
        def provider():
            sample = json.loads(self.clock_provider())
            sample["guest_monotonic_ns"] = 20_000000000
            return health.encode(sample)
        result = self.collect(provider=provider)
        self.assertEqual(result["reason"], "guest_boot_too_recent_for_native_freshness")
        self.assertEqual(self.queries, [])

    def test_invalid_cli_config_publishes_blocked_receipt(self):
        self.collect()
        config = self.root / "invalid-config.json"
        config.write_text("invalid JSON", encoding="utf-8")
        original = sys.argv
        try:
            sys.argv = ["native_health", "--config", str(config), "--catalog", str(self.catalog_path),
                        "--clock-argv-file", str(self.root/"absent-argv.json"), "--output", str(self.output),
                        "--evidence-dir", str(self.root/"evidence")]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(health.main(), 1)
        finally:
            sys.argv = original
        result = json.loads(self.output.read_bytes())
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "configuration_or_clock_argv_unavailable")


if __name__ == "__main__":
    unittest.main()
