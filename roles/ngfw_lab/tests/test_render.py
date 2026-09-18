"""Render role defaults with strict Jinja and validate Compose with Docker."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import jinja2
import yaml
from test_runner import r, ROOT


class Rendering(unittest.TestCase):
    def test_default_config_and_compose(self):
        values = yaml.safe_load((ROOT / "defaults/main.yml").read_text())
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        env.filters["to_nice_json"] = lambda v: json.dumps(v, indent=2)

        def resolve(value):
            if isinstance(value, str):
                return env.from_string(value).render(values)
            if isinstance(value, list):
                return [resolve(v) for v in value]
            if isinstance(value, dict):
                return {k: resolve(v) for k, v in value.items()}
            return value

        for _ in range(5):
            values = resolve(values)
        rendered = env.from_string((ROOT / "templates/runner.json.j2").read_text()).render(values)
        config = r.validate(json.loads(rendered))
        self.assertEqual(len(config["scenarios"]), 9)
        self.assertEqual(config["repetitions"], 3)
        compose = env.from_string((ROOT / "templates/compose.yml.j2").read_text()).render(values)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "compose.yml"
            path.write_text(compose)
            result = subprocess.run(["docker", "compose", "-f", str(path), "config", "--format", "json"],
                                    check=True, capture_output=True, text=True)
        data = json.loads(result.stdout)
        for name in ("traffic-client", "traffic-server"):
            self.assertTrue(data["services"][name]["read_only"])
            self.assertEqual(data["services"][name]["restart"], "no")
            self.assertEqual(int(data["services"][name]["mem_limit"]), 536870912)
        self.assertEqual(json.loads(data["services"]["traffic-server"]["environment"]["NGFW_POLICY_LISTEN_RANGES"]), [])
        values["ngfw_lab_policy_listener_ranges"] = [[10000, 10991], [20000, 20099]]
        enabled = yaml.safe_load(env.from_string((ROOT / "templates/compose.yml.j2").read_text()).render(values))
        self.assertEqual(json.loads(enabled["services"]["traffic-server"]["environment"]["NGFW_POLICY_LISTEN_RANGES"]),
                         values["ngfw_lab_policy_listener_ranges"])
        self.assertEqual(enabled["services"]["traffic-server"]["ulimits"]["nofile"]["soft"], 16384)


if __name__ == "__main__":
    unittest.main()
