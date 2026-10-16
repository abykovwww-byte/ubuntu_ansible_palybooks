"""Exercise actual Ansible directories/templates and Compose mounts with dummy inputs."""
import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def checked(*args, **kwargs):
    result = subprocess.run(args, capture_output=True, text=True, timeout=90, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return result.stdout


def main():
    network = 'tg-config-' + uuid.uuid4().hex[:10]
    with tempfile.TemporaryDirectory(prefix='tgcollector-config-') as directory:
        base = Path(directory)
        source = yaml.safe_load((ROOT / 'roles/telegram_collector/tasks/main.yml').read_text(encoding='utf-8'))
        names = {
            'Create private collector data directories',
            'Create an empty mount target without exposing credentials to the scope helper',
            'Render the collector config and its writer-only credentials',
            'Render the isolated collector Compose project',
        }
        tasks = []
        for task in source:
            if task['name'] not in names:
                continue
            name = next(key for key in task if key.startswith('ansible.builtin.'))
            module = dict(task[name])
            module.pop('owner')
            module.pop('group')
            if name == 'ansible.builtin.template':
                module['src'] = str(ROOT / 'roles/telegram_collector/templates' / module['src'])
            tasks.append(dict(task, **{name: module}))
        assert len(tasks) == len(names)
        project = base / 'project'
        project.mkdir()
        variables = {
            'telegram_collector_project_dir': str(project),
            'telegram_collector_data_dir': str(base / 'data'),
            'context_postgres_data_dir': str(base / 'postgres'),
            'telegram_archive_base': str(base / 'archive'),
            'telegram_collector_user': {'ansible_facts': {'getent_passwd': {
                'tgmedia-rw': ['x', str(os.getuid()), str(os.getgid())],
            }}},
            'telegram_collector_api': {'api_id': 1, 'api_hash': '0' * 32},
            'telegram_archive_writer_password': 'SYNTHETIC-fixture-password',
            'telegram_collector_settings': {
                'default_since': '2026-01-01', 'refresh_days': 7, 'max_per_chat_run': 10,
                'media': {'kinds': ['document'], 'max_file_mb': 10, 'max_per_run_gb': 1,
                          'max_total_gb': 2, 'min_free_gb': 1},
            },
        }
        playbook = base / 'render.json'
        playbook.write_text(json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'become': False,
            'vars_files': [str(ROOT / f'roles/{role}/defaults/main.yml') for role in
                           ('context_postgres', 'telegram_archive', 'telegram_collector')],
            'tasks': tasks,
        }]), encoding='utf-8')
        values = base / 'vars.json'
        values.write_text(json.dumps(variables), encoding='utf-8')
        checked('ansible-playbook', '-i', 'localhost,', str(playbook),
                '--extra-vars', '@' + str(values), cwd=ROOT)
        data = base / 'data'
        (data / 'scope/chats.yaml').write_text('chats: []\n', encoding='utf-8')
        (data / 'secrets/telegram-session').write_text('SYNTHETIC-not-a-session', encoding='utf-8')
        (base / 'postgres/socket').mkdir(parents=True)
        (base / 'archive/media').mkdir(parents=True)
        assert (data / 'scope/sync.env').read_bytes() == b''
        assert (data / 'scope/reports').is_dir()
        # The fixture changes only its image tag and external network name.
        compose = project / 'compose.yml'
        config = yaml.safe_load(compose.read_text(encoding='utf-8'))
        for service in config['services'].values():
            service['image'] = os.environ.get('TG_COLLECTOR_TEST_IMAGE', 'telegram-collector-ci')
        config['networks']['vpn_clients']['name'] = network
        compose.write_text(yaml.safe_dump(config), encoding='utf-8')
        command = ['docker', 'compose', '--project-name', network, '--file', str(compose)]
        checked(*command, 'config', '--quiet')
        try:
            checked('docker', 'network', 'create', '--internal', network)
            result = checked(*command, 'run', '--rm', '--no-deps', '-T', 'collector',
                             'python', '-m', 'tg_archive.cli', '--config', '/data/tg-archive.toml', '--check')
            receipt = json.loads(result)
            assert receipt == {'configuration': 'ok', 'network_used': False,
                               'selected_chats': 0, 'credentials_present': True}, receipt
            checked(*command, '--profile', 'scope', 'run', '--rm', '--no-deps', '-T', 'scope',
                    'python', '-c', "from pathlib import Path; "
                    "assert Path('/data/sync.env').read_bytes() == b''; "
                    "assert not Path('/run/secrets/telegram-session').exists(); "
                    "assert not Path('/run/postgresql').exists()")
        finally:
            checked('docker', 'network', 'rm', network)
    print(json.dumps({'fixture': 'actual-ansible-compose-config', 'status': 'ok',
                      'real_volume_layout': True, 'credentials_visible_to_scope': False,
                      'network_used': False, 'service_configuration_changed': False}))


if __name__ == '__main__':
    main()
