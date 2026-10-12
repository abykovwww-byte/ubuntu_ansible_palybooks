import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from types import SimpleNamespace
import hashlib

PATH = Path(__file__).resolve().parents[2] / 'ngfw_native_source/files/discover.py'
spec = importlib.util.spec_from_file_location('native_discover', PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DiscoveryTests(unittest.TestCase):
    def test_credentials_urls_commands_are_not_emitted(self):
        cfg = '''[agent]
interval="10s"
metric_buffer_limit=10000
[[inputs.exec]]
commands=["secret-command token-value"]
[[outputs.influxdb]]
urls=["http://secret-user:secret-pass@example.test"]
password="secret-password"
'''
        summary = module.config_summary(cfg)
        self.assertEqual(summary['agent']['interval'], '10s')
        self.assertEqual(summary['inputs'][0]['command_count'], 1)
        self.assertNotIn('secret', str(summary))
        self.assertIsNone(summary['native_metric_names'])

    def test_invalid_toml_does_not_echo_source(self):
        self.assertEqual(module.config_summary('password=secret'), {'unavailable': 'TOML_PARSE_FAILED'})

    def test_full_hash_and_count_survive_display_truncation(self):
        output = ('rule-with-long-parameters\n' * 3000).strip()
        with patch.object(module.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=output)):
            receipt = module.command(['auditctl', '-l'])
        self.assertTrue(receipt['stdout_truncated'])
        self.assertEqual(receipt['nonempty_lines'], 3000)
        self.assertEqual(receipt['stdout_sha256'], hashlib.sha256(output.encode()).hexdigest())
        self.assertNotEqual(receipt['stdout_sha256'], hashlib.sha256(receipt['stdout'].encode()).hexdigest())


if __name__ == '__main__':
    unittest.main()
