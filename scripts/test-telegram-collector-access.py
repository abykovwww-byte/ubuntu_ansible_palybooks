"""Render the actual access tasks through Ansible into disposable files only."""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def main():
    visudo = shutil.which('visudo') or '/usr/sbin/visudo'
    with tempfile.TemporaryDirectory(prefix='tgcollector-access-') as directory:
        temporary = Path(directory)
        tasks = []
        for role, names in {
            'telegram_archive': {'Install source-restricted SFTP public keys'},
            'telegram_collector': {
                'Install the exact-client scope public key',
                'Grant only the fixed forced-command launcher',
            },
        }.items():
            source = yaml.safe_load((ROOT / f'roles/{role}/tasks/main.yml').read_text(encoding='utf-8'))
            selected = [task for task in source if task['name'] in names]
            assert len(selected) == len(names), names
            for task in selected:
                copied = dict(task['ansible.builtin.copy'])
                copied.pop('owner')
                copied.pop('group')
                copied['dest'] = str(temporary / Path(copied['dest']).name)
                if 'validate' in copied:
                    copied['validate'] = visudo + ' -cf %s'
                tasks.append(dict(task, **{'ansible.builtin.copy': copied}))

        playbook = temporary / 'render.json'
        playbook.write_text(json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'become': False,
            'vars_files': [str(ROOT / 'roles/telegram_archive/defaults/main.yml')],
            'vars': {'telegram_archive_client_ip': '100.64.0.42',
                     'role_path': str(ROOT / 'roles/telegram_collector')},
            'tasks': tasks,
        }]), encoding='utf-8')
        result = subprocess.run([
            'ansible-playbook', '-i', 'localhost,', str(playbook),
        ], env=dict(os.environ, ANSIBLE_NOCOLOR='1'), cwd=ROOT,
            capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)

        for name in ('tgmedia-rw', 'tgmedia-ro', 'tgarchive-scope'):
            data = (temporary / name).read_bytes()
            assert data.endswith(b'\n') and data.count(b'\n') == 1, (name, repr(data))
            assert b'\\n' not in data, (name, repr(data))
            subprocess.run(['ssh-keygen', '-l', '-f', str(temporary / name)],
                           check=True, capture_output=True)
        sudoers = temporary / 'tg-archive-scope'
        expected = b'tgarchive-scope ALL=(root) NOPASSWD: /usr/local/libexec/tg-archive-scope ""\n'
        assert sudoers.read_bytes() == expected
        # Ensure this fixture would catch the original literal-backslash-n defect.
        invalid_sudoers = temporary / 'invalid-sudoers'
        invalid_sudoers.write_bytes(expected.rstrip(b'\n') + b'\\n\n')
        invalid = subprocess.run([visudo, '-cf', str(invalid_sudoers)], capture_output=True)
        assert invalid.returncode != 0, 'visudo accepted the regression fixture'

    print(json.dumps({'fixture': 'real-ansible-access-files', 'status': 'ok',
                      'public_keys': 3, 'sudoers_validated': True,
                      'literal_newline_rejected': True, 'become': False,
                      'service_configuration_changed': False}))


if __name__ == '__main__':
    main()
