"""Execute the role's actual preflight with synthetic inputs; no become or mutations."""
import json
import os
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    valid = {
        'telegram_archive_client_ip': '100.64.0.42',
        'telegram_archive_owner_password': 'SYNTHETIC-owner-fixture-password-123456789',
        'telegram_archive_writer_password': 'SYNTHETIC-writer-fixture-password-123456789',
        'telegram_archive_reader_password': 'SYNTHETIC-reader-fixture-password-123456789',
    }
    rw_public = (ROOT / 'roles/telegram_archive/files/tgmedia-rw.pub').read_text(encoding='ascii').strip()
    cases = [
        ('valid-default-key-lookups', {}, True),
        ('invalid-key', {'telegram_archive_rw_public_key': 'ssh-rsa SYNTHETIC'}, False),
        ('duplicate-key', {'telegram_archive_ro_public_key': rw_public}, False),
        ('non-tailscale-client', {'telegram_archive_client_ip': '192.0.2.42'}, False),
        ('short-password', {'telegram_archive_reader_password': 'SYNTHETIC-short'}, False),
    ]
    with tempfile.TemporaryDirectory(prefix='tgarchive-preflight-') as directory:
        playbook = Path(directory) / 'preflight.json'
        variables = Path(directory) / 'vars.json'
        playbook.write_text(json.dumps([{
            'hosts': 'localhost', 'connection': 'local', 'gather_facts': False, 'become': False,
            'tasks': [{'ansible.builtin.include_role': {
                'name': 'telegram_archive', 'tasks_from': 'preflight',
            }}],
        }]), encoding='utf-8')
        env = dict(os.environ, ANSIBLE_ROLES_PATH=str(ROOT / 'roles'))
        for label, overrides, expected_success in cases:
            variables.write_text(json.dumps(valid | overrides), encoding='utf-8')
            result = subprocess.run([
                'ansible-playbook', '-i', 'localhost,', str(playbook),
                '--extra-vars', '@' + str(variables),
            ], env=env, cwd=ROOT, capture_output=True, text=True, timeout=60)
            if (result.returncode == 0) != expected_success:
                # Fixtures contain no real credentials; output can diagnose parser errors.
                raise RuntimeError(label + '\n' + result.stdout + result.stderr)
        print(json.dumps({'fixture': 'real-ansible-preflight', 'cases': len(cases),
                          'status': 'ok', 'become': False, 'configuration_changed': False}))


if __name__ == '__main__':
    main()
