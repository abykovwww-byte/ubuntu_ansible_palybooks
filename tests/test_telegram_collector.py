import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
ENV = Environment(undefined=StrictUndefined)


def render(role, name, **extra):
    values = {}
    for item in ('context_postgres', 'telegram_archive', 'telegram_collector'):
        values.update(yaml.safe_load((ROOT / f'roles/{item}/defaults/main.yml').read_text(encoding='utf-8')))
    values.update(telegram_archive_enabled=True, telegram_collector_enabled=True,
                  telegram_archive_client_ip='100.64.0.42', telegram_collector_user={
                      'ansible_facts': {'getent_passwd': {'tgmedia-rw': ['x', '991', '992']}}})
    values.update(extra)
    return ENV.from_string((ROOT / f'roles/{role}/templates/{name}.j2').read_text(encoding='utf-8')).render(values)


class ServerCollectorTests(unittest.TestCase):
    def test_collector_has_one_internal_network_and_no_listener_or_tcp_database(self):
        config = yaml.safe_load(render('telegram_collector', 'compose.yml'))
        service = config['services']['collector']
        self.assertEqual(service['networks'], ['vpn_clients'])
        self.assertEqual(config['networks']['vpn_clients'], {'external': True, 'name': 'vpn_clients'})
        self.assertNotIn('ports', service)
        self.assertNotIn('network_mode', service)
        self.assertTrue(service['read_only'])
        self.assertEqual(service['logging']['driver'], 'none')
        self.assertIn('/srv/app-data/context-postgres/socket:/run/postgresql:ro', service['volumes'])
        self.assertIn('/srv/data/tg-archive/media:/media', service['volumes'])
        self.assertEqual(service['user'], '991:992')

    def test_unix_writer_rule_does_not_allow_owner_reader_or_another_database(self):
        hba = render('context_postgres', 'pg_hba.conf')
        self.assertLess(hba.index('local tgarchive tg_writer scram-sha-256'), hba.index('local all all reject'))
        self.assertNotIn('local tgarchive tg_owner', hba)
        self.assertNotIn('local tgarchive tg_reader', hba)
        disabled = render('context_postgres', 'pg_hba.conf', telegram_collector_enabled=False)
        self.assertNotIn('local tgarchive', disabled)
        self.assertIn('100.64.0.42/32', hba)

    def test_existing_listener_stays_exact_and_only_local_socket_is_added(self):
        conf = render('context_postgres', 'postgresql.conf')
        self.assertIn("listen_addresses = '100.117.52.16,127.0.0.1'", conf)
        hba = render('context_postgres', 'pg_hba.conf')
        self.assertNotIn('127.0.0.1/32 scram-sha-256', hba)
        self.assertIn("unix_socket_directories = '/var/run/postgresql,/archive-socket'", conf)
        compose = yaml.safe_load(render('context_postgres', 'compose.yml'))
        self.assertIn('/srv/app-data/context-postgres/socket:/archive-socket', compose['services']['postgres']['volumes'])

    def test_single_persistent_schedule_waits_for_previous_collection(self):
        timer = render('telegram_collector', 'telegram-collector.timer')
        service = render('telegram_collector', 'telegram-collector.service')
        self.assertIn('OnCalendar=*-*-* *:00,30:00', timer)
        self.assertIn('Persistent=true', timer)
        self.assertIn('Type=oneshot', service)
        self.assertIn('--no-deps -T collector', service)
        self.assertIn('TimeoutStartSec=4h', service)
        self.assertIn('ExecStopPost=', service)

    def test_secret_tasks_do_not_expose_their_content(self):
        tasks = yaml.safe_load((ROOT / 'roles/telegram_collector/tasks/main.yml').read_text(encoding='utf-8'))
        for task in tasks:
            if any(name in task for name in ('ansible.builtin.slurp', 'ansible.builtin.set_fact')):
                self.assertTrue(task.get('no_log'), task['name'])
        self.assertFalse(yaml.safe_load((ROOT / 'roles/telegram_collector/defaults/main.yml').read_text(encoding='utf-8'))['telegram_collector_enabled'])

    def test_writer_password_is_encoded_as_a_single_dsn_value(self):
        from psycopg.conninfo import conninfo_to_dict

        password = "SYNTHETIC with spaces/'@\\?&"
        rendered = render('telegram_collector', 'sync.env',
                          telegram_collector_api={'api_id': '1', 'api_hash': '0' * 32},
                          telegram_archive_writer_password=password)
        dsn = rendered.split('TG_DB_WRITER_DSN=', 1)[1].strip()
        parsed = conninfo_to_dict(dsn)
        self.assertEqual(parsed['password'], password)
        self.assertEqual(parsed['host'], '/run/postgresql')
        self.assertEqual(parsed['user'], 'tg_writer')


if __name__ == '__main__':
    unittest.main()
