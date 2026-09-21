"""Portable settings/render checks. Container behavior is tested separately in CI."""
import importlib.util
import json
from pathlib import Path
import unittest

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('collector', ROOT / 'files/collector/collector.py')
c = importlib.util.module_from_spec(spec)
spec.loader.exec_module(c)


def render_values(**overrides):
    values = yaml.safe_load((ROOT / 'defaults/main.yml').read_text(encoding='utf-8'))
    values.update(overrides)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters['to_nice_json'] = lambda value: json.dumps(value, indent=2)
    config = json.loads(env.from_string((ROOT / 'templates/collector.json.j2').read_text()).render(values))
    compose = yaml.safe_load(env.from_string((ROOT / 'templates/compose.yml.j2').read_text()).render(values))
    return values, config, compose


class Configuration(unittest.TestCase):
    def setUp(self):
        self.values, self.config, self.compose = render_values()

    def test_defaults_require_explicit_enable_and_start(self):
        self.assertFalse(self.values['ngfw_logs_enabled'])
        self.assertFalse(self.values['ngfw_logs_start'])
        self.assertEqual(c.validate(self.config), self.config)

    def test_management_only_no_docker_nat_or_privileges(self):
        service = self.compose['services']['collector']
        self.assertEqual(service['network_mode'], 'host')
        self.assertNotIn('ports', service)
        self.assertNotIn('privileged', service)
        self.assertEqual(service['user'], '10001:10001')
        self.assertEqual(service['cap_drop'], ['ALL'])
        self.assertEqual(service['security_opt'], ['no-new-privileges:true'])
        self.assertTrue(service['read_only'])
        self.assertEqual(service['mem_limit'], '512m')
        self.assertEqual(service['pids_limit'], 64)
        self.assertEqual(len(service['volumes']), 3)
        self.assertFalse(any('docker.sock' in mount for mount in service['volumes']))

    def test_two_streams_raw_json_and_source_filters(self):
        result = c.syslog_config(self.config)
        for stream in ('auditd', 'ngfw'):
            self.assertIn('/data/' + stream + '/events.jsonl', result)
            self.assertIn('filter(f_' + stream + ')', result)
        self.assertEqual(result.count('transport("tcp")'), 2)
        self.assertEqual(result.count('transport("udp")'), 2)
        self.assertEqual(result.count('flags(no-parse)'), 4)
        self.assertIn('raw=$MESSAGE', result)
        self.assertIn('source_ip=$SOURCEIP', result)
        self.assertNotIn('0.0.0.0', result)
        self.assertIn('netmask("10.77.0.20/32")', result)
        self.assertNotIn('system()', result)
        self.assertNotIn('internal()', result)

    def test_bounded_rotation_reopens_instead_of_copytruncate(self):
        result = c.rotation_config(self.config)
        self.assertIn('maxsize 64M', result)
        self.assertIn('rotate 7', result)
        self.assertIn('create 0600 collector collector', result)
        self.assertIn('syslog-ng-ctl reload', result)
        self.assertNotIn('copytruncate', result)
        self.assertNotIn('*', result)

    def test_unsafe_addresses_and_injection_rejected(self):
        for bad in ['0.0.0.0', '::', '8.8.8.8', '239.1.1.1', '10.77.0.1/24', '127.0.0.1"); system(); #']:
            for key in ('listen_address', 'audit_sources', 'ngfw_sources'):
                with self.subTest(value=bad, key=key):
                    changed = self.config | {key: [bad] if key.endswith('sources') else bad}
                    with self.assertRaises(ValueError):
                        c.validate(changed)

    def test_empty_or_broad_source_sets_rejected(self):
        for value in [[], '10.77.0.20', ['10.77.0.0/24'], ['10.77.0.20'] * 17]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                c.validate(self.config | {'audit_sources': value})

    def test_ports_and_retention_bounded(self):
        for key, value in [('audit_port', 514), ('ngfw_port', 65536), ('audit_port', True),
                           ('ngfw_port', 5514), ('rotate_mib', 0), ('rotate_count', 0),
                           ('rotate_count', 31), ('rotate_mib', '64M'), ('rotate_count', True)]:
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                c.validate(self.config | {key: value})

    def test_role_never_touches_guest_or_private_vars(self):
        tasks = (ROOT / 'tasks/main.yml').read_text(encoding='utf-8')
        for forbidden in ['local-overrides.yml', 'authorized_keys', 'auditctl', 'delegate_to:', 'virsh', 'shell:']:
            self.assertNotIn(forbidden, tasks)
        self.assertIn('ngfw_logs_start | bool', tasks)
        self.assertIn('not ansible_check_mode', tasks)
        self.assertIn('10.77.0.1', tasks)


if __name__ == '__main__':
    unittest.main()
