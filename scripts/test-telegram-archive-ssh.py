"""Validate OpenSSH parsing/effective Match rules without starting a daemon."""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    daemon = shutil.which('sshd') or '/usr/sbin/sshd'
    with tempfile.TemporaryDirectory(prefix='tgarchive-ssh-') as directory:
        base = Path(directory)
        key = base / 'hostkey'
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(key)], check=True)
        snippet = base / '99-tg-archive.conf'
        snippet.write_text((ROOT / 'roles/telegram_archive/templates/sshd.conf.j2').read_text(encoding='utf-8')
                           .replace('{{ telegram_archive_base }}', '/srv/data/tg-archive')
                           .replace('{{ telegram_archive_client_ip }}', '100.64.0.42'), encoding='utf-8')
        config = base / 'sshd_config'
        scope_snippet = base / '98-tg-archive-scope.conf'
        scope_snippet.write_text((ROOT / 'roles/telegram_collector/templates/scope-sshd.conf.j2').read_text(encoding='utf-8')
                                 .replace('{{ telegram_archive_client_ip }}', '100.64.0.42'), encoding='utf-8')
        config.write_text(f'Include {scope_snippet}\nInclude {snippet}\nUsePAM yes\nPasswordAuthentication yes\n', encoding='utf-8')
        subprocess.run([daemon, '-t', '-f', str(config), '-h', str(key)], check=True,
                       capture_output=True, text=True)
        for user, command in [('tgmedia-rw', 'internal-sftp -d /media -u 0027'),
                              ('tgmedia-ro', 'internal-sftp -R -d /media'),
                              ('tgarchive-scope', 'sudo -n /usr/local/libexec/tg-archive-scope'),
                              ('abykov', 'none')]:
            result = subprocess.run([daemon, '-T', '-f', str(config), '-h', str(key),
                                     '-C', f'user={user},host=fixture,addr=100.64.0.42'],
                                    capture_output=True, text=True, check=True)
            values = dict(line.split(' ', 1) for line in result.stdout.splitlines())
            assert values['forcecommand'] == command, user
            if user.startswith('tgmedia-') or user == 'tgarchive-scope':
                expected_chroot = '/srv/data/tg-archive' if user.startswith('tgmedia-') else 'none'
                assert values['chrootdirectory'] == expected_chroot
                assert values['passwordauthentication'] == 'no'
                assert values['disableforwarding'] == 'yes'
                assert values['permittty'] == 'no'
                assert values['authorizedkeysfile'] == '/etc/ssh/tg-archive-keys/%u'
            else:
                assert values['chrootdirectory'] == 'none'
                assert values['passwordauthentication'] == 'yes'
            outside = subprocess.run([daemon, '-T', '-f', str(config), '-h', str(key),
                                      '-C', f'user={user},host=fixture,addr=100.64.0.43'],
                                     capture_output=True, text=True, check=True)
            outside_values = dict(line.split(' ', 1) for line in outside.stdout.splitlines())
            if user.startswith('tgmedia-') or user == 'tgarchive-scope':
                denied = {name for line in outside.stdout.splitlines()
                          if line.startswith('denyusers ') for name in line.split()[1:]}
                expected = {'tgmedia-rw', 'tgmedia-ro'} if user.startswith('tgmedia-') else {'tgarchive-scope'}
                assert denied == expected, denied
                assert 'denyusers' not in values
            else:
                assert 'denyusers' not in outside_values
        print(json.dumps({'fixture': 'sshd-effective-configuration', 'users_checked': 4,
                          'source_addresses_checked': 2, 'status': 'ok', 'daemon_started': False}))


if __name__ == '__main__':
    main()
