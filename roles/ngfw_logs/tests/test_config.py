"""Portable settings/render checks. Container behavior is tested separately in CI."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import tempfile

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
    def test_metrics_projection_omits_peer_and_file_details(self):
        value = ('SourceName;SourceId;SourceInstance;State;Type;Number\n'
                 'dst.file;d_auditd#0;SECRET_PATH;a;processed;42\n'
                 'src.network;s_ngfw;SECRET_IP;a;dropped;2\n'
                 'internal;other;PRIVATE;a;processed;999\n')
        parsed = c.parse_stats(value)
        self.assertEqual(len(parsed), 2)
        self.assertEqual(parsed[0]['value'], 42)
        self.assertNotIn('SECRET', json.dumps(parsed))

    def setUp(self):
        self.values, self.config, self.compose = render_values()

    def test_defaults_require_explicit_enable_and_start(self):
        self.assertFalse(self.values['ngfw_logs_enabled'])
        self.assertFalse(self.values['ngfw_logs_start'])
        self.assertEqual(c.validate(self.config), self.config)

    def test_selected_host_enables_receiver_but_not_guest_apply(self):
        repo = ROOT.parents[1]
        selected = yaml.safe_load((repo / 'inventories/local/group_vars/server.yml').read_text(encoding='utf-8'))
        self.assertTrue(selected['ngfw_logs_enabled'])
        self.assertTrue(selected['ngfw_logs_start'])
        defaults = yaml.safe_load((repo / 'roles/ngfw_audit_forwarder/defaults/main.yml').read_text())
        self.assertEqual(defaults['ngfw_audit_forwarder_mode'], 'check')
        self.assertFalse(defaults['ngfw_audit_forwarder_install_packages'])
        site = (repo / 'playbooks/site.yml').read_text()
        self.assertNotIn('ngfw_audit_forwarder', site)

    def test_sender_is_file_only_bounded_and_separate(self):
        root = ROOT.parent / 'ngfw_audit_forwarder'
        config = (root / 'templates/rsyslog.conf.j2').read_text()
        unit = (root / 'templates/ngfw-audit-forwarder.service.j2').read_text()
        tasks = (root / 'tasks/main.yml').read_text()
        self.assertIn('File="/var/log/audit/audit.log"', config)
        self.assertIn('target="10.77.0.1" port="5514"', config)
        self.assertIn('PersistStateInterval="1"', config)
        self.assertIn('freshStartTail="off"', config)
        self.assertIn('queue.timeoutEnqueue="0"', config)
        for forbidden in ['imuxsock', 'imjournal', 'include(', 'queue.filename']:
            self.assertNotIn(forbidden, config)
        for setting in ['MemoryMax=256M', 'CPUQuota=50%', 'NoNewPrivileges=yes',
                        'ProtectSystem=strict', 'IPAddressDeny=any', 'StateDirectory=ngfw-audit-forwarder']:
            self.assertIn(setting, unit)
        for forbidden in ['ansible.builtin.apt:', 'ansible.builtin.shell:', 'authorized_keys', 'local-overrides.yml', 'auditctl, -R']:
            self.assertNotIn(forbidden, tasks)
        self.assertIn('validate:', tasks)
        self.assertIn('rescue:', tasks)
        bootstrap = yaml.safe_load((root / 'tasks/packages.yml').read_text())
        install = next(task['ansible.builtin.apt'] for task in bootstrap if 'ansible.builtin.apt' in task)
        for key in ['update_cache', 'install_recommends', 'auto_install_module_deps',
                    'allow_downgrade', 'allow_unauthenticated']:
            self.assertFalse(install[key])
        self.assertTrue(install['fail_on_autoremove'])
        self.assertEqual(install['policy_rc_d'], 101)
        self.assertIn('when: ngfw_audit_forwarder_install_packages | bool', tasks)

    def test_audit_receipt_requires_source_record_and_exact_marker(self):
        marker = 'NGFW_AUDIT_FORWARDER_1789977600'
        with tempfile.TemporaryDirectory() as temp, patch.object(c, 'DATA', Path(temp)):
            path = Path(temp) / 'auditd'
            path.mkdir()
            row = dict(stream='auditd', source_ip='10.77.0.20',
                       raw='type=USER msg=audit(1789977600.123:42): msg=' + marker)
            for wrong in [row | {'source_ip': '10.77.0.10'}, row | {'stream': 'ngfw'},
                          row | {'raw': marker}, row | {'raw': row['raw'] + '0'}]:
                (path / 'events.jsonl').write_text(json.dumps(wrong) + '\n')
                self.assertIsNone(c.find_audit_receipt(marker))
            (path / 'events.jsonl').write_text(json.dumps(row) + '\n')
            result = c.find_audit_receipt(marker)
            self.assertEqual(result['audit_id'], '1789977600.123:42')
            self.assertNotIn('raw', result)
            (path / 'events.jsonl').rename(path / 'events.jsonl.1')
            self.assertEqual(c.find_audit_receipt(marker), result)
            with self.assertRaises(ValueError):
                c.find_audit_receipt('')

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
