import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
ENV = Environment(undefined=StrictUndefined)


def values():
    result = yaml.safe_load((ROOT / 'roles/context_postgres/defaults/main.yml').read_text(encoding='utf-8'))
    result.update(yaml.safe_load((ROOT / 'roles/telegram_archive/defaults/main.yml').read_text(encoding='utf-8')))
    result.update(telegram_archive_enabled=True, telegram_archive_client_ip='100.64.0.42')
    return result


def render(role, filename, **overrides):
    data = values() | overrides
    return ENV.from_string((ROOT / f'roles/{role}/templates/{filename}.j2').read_text(encoding='utf-8')).render(data)


class ArchiveBoundaryTests(unittest.TestCase):
    def test_compose_has_one_instance_exact_bind_and_no_secret_values(self):
        config = yaml.safe_load(render('context_postgres', 'compose.yml'))
        self.assertEqual(list(config['services']), ['postgres'])
        pg = config['services']['postgres']
        self.assertEqual(pg['ports'], ['100.117.52.16:5432:5432'])
        self.assertEqual(pg['image'], 'postgres:17.11-bookworm')
        self.assertNotIn('POSTGRES_PASSWORD', pg['environment'])
        self.assertEqual(pg['environment']['POSTGRES_PASSWORD_FILE'], '/run/secrets/postgres_password')
        self.assertEqual(config['networks']['database']['driver'], 'bridge')
        self.assertNotIn('external', config['networks']['database'])
        self.assertNotIn('network_mode', pg)
        self.assertEqual(pg['logging']['driver'], 'none')

    def test_hba_exact_clients_and_other_databases_rejected(self):
        hba = render('context_postgres', 'pg_hba.conf')
        active = [line.split() for line in hba.splitlines() if line and not line.startswith('#')]
        allows = [line for line in active if line[-1] != 'reject']
        self.assertEqual(allows, [
            ['local', 'all', 'postgres', 'peer'],
            ['host', 'tgarchive', 'tg_owner,tg_writer,tg_reader', '100.64.0.42/32', 'scram-sha-256'],
        ])
        future = render('context_postgres', 'pg_hba.conf', context_postgres_client_rules=[
            {'database': 'context_registry', 'user': 'registry_reader', 'client_ip': '100.64.0.42'},
        ])
        self.assertIn('host context_registry registry_reader 100.64.0.42/32 scram-sha-256', future)
        disabled = render('context_postgres', 'pg_hba.conf', telegram_archive_enabled=False)
        self.assertNotIn('tgarchive', disabled)

    def test_ssh_accounts_have_distinct_forced_commands_and_no_forwarding(self):
        config = render('telegram_archive', 'sshd.conf')
        self.assertIn('ChrootDirectory /srv/data/tg-archive', config)
        self.assertIn('DisableForwarding yes', config)
        self.assertIn('AuthenticationMethods publickey', config)
        self.assertIn('ForceCommand internal-sftp -R -d /media', config)
        self.assertIn('ForceCommand internal-sftp -d /media -u 0027', config)
        self.assertIn('PermitUserRC no', config)

    def test_secrets_are_private_and_roles_are_disabled_by_default(self):
        for role in ('context_postgres', 'telegram_archive'):
            defaults = yaml.safe_load((ROOT / f'roles/{role}/defaults/main.yml').read_text(encoding='utf-8'))
            self.assertFalse(defaults[f'{role}_enabled'])
            tasks = yaml.safe_load((ROOT / f'roles/{role}/tasks/main.yml').read_text(encoding='utf-8'))
            for task in tasks:
                if 'password' in str(task.get('ansible.builtin.copy', {})):
                    self.assertTrue(task.get('no_log'))
                    self.assertEqual(task['ansible.builtin.copy']['mode'], '0400')
        tasks = (ROOT / 'roles/telegram_archive/tasks/main.yml').read_text(encoding='utf-8')
        self.assertIn('from="{{ telegram_archive_client_ip }}",restrict', tasks)


if __name__ == '__main__':
    unittest.main()
