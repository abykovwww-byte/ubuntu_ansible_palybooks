"""Exercise rendered host-network templates on a temporary loopback-only fixture."""
import json
import socket
import struct
import subprocess
import tempfile
import uuid
from pathlib import Path

import psycopg
import yaml
from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]


def password(role):
    return 'SYNTHETIC-' + role + '-network-fixture-password-123456789'


def run(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError('Network fixture command failed: ' + args[0])
    return result


def receive(sock, length):
    result = b''
    while len(result) < length:
        piece = sock.recv(length - len(result))
        if not piece:
            raise AssertionError('Unexpected PostgreSQL protocol EOF')
        result += piece
    return result


def main():
    name = 'context-postgres-fixture-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='context-postgres-net-') as directory:
        base = Path(directory)
        base.chmod(0o755)
        for folder in ('config', 'secrets'):
            (base / folder).mkdir(mode=0o755)
        (base / 'socket').mkdir()
        # Isolated synthetic fixture only; production uses postgres-owned 0755.
        (base / 'socket').chmod(0o777)
        with socket.socket() as candidate:
            candidate.bind(('127.0.0.1', 0))
            port = candidate.getsockname()[1]
        values = yaml.safe_load((ROOT / 'roles/context_postgres/defaults/main.yml').read_text())
        values.update(context_postgres_bind_host='127.0.0.1', context_postgres_port=port,
                      context_postgres_project_dir=str(base), context_postgres_data_dir=str(base),
                      telegram_archive_enabled=True, telegram_archive_client_ip='127.0.0.1',
                      telegram_collector_enabled=True)
        env = Environment(undefined=StrictUndefined)
        def render(filename):
            return env.from_string((ROOT / 'roles/context_postgres/templates' / filename).read_text()).render(values)
        config = yaml.safe_load(render('compose.yml.j2'))
        service = config['services']['postgres']
        service['volumes'] = [item for item in service['volumes'] if not item.endswith(':/var/lib/postgresql/data')]
        service['tmpfs'] = ['/var/lib/postgresql/data:rw,nosuid,size=128m']
        service['restart'] = 'no'
        (base / 'compose.yml').write_text(yaml.safe_dump(config))
        for filename in ('postgresql.conf', 'pg_hba.conf'):
            (base / 'config' / filename).write_text(render(filename + '.j2'))
        (base / 'config/bootstrap.sql').write_text(
            (ROOT / 'roles/telegram_archive/templates/bootstrap.sql.j2').read_text())
        for role in ('postgres', 'tg_owner', 'tg_writer', 'tg_reader'):
            (base / 'secrets' / (role + '_password')).write_text(password(role))
        compose = ['docker', 'compose', '-p', name, '-f', str(base / 'compose.yml')]
        try:
            run(compose + ['up', '-d', '--wait', '--wait-timeout', '90'])
            run(compose + ['exec', '-T', '--user', 'postgres', 'postgres', 'psql', '-X',
                           '-v', 'ON_ERROR_STOP=1', '-p', str(port), '-d', 'postgres',
                           '-f', '/etc/postgresql/bootstrap.sql'])
            with psycopg.connect(host='127.0.0.1', port=port, user='tg_writer',
                                 password=password('tg_writer'), dbname='tgarchive',
                                 autocommit=True, connect_timeout=10) as db:
                assert db.execute('SELECT host(inet_client_addr())').fetchone()[0] == '127.0.0.1'
                assert db.execute('SHOW listen_addresses').fetchone()[0] == '127.0.0.1'
            with psycopg.connect(host=str(base / 'socket'), port=port, user='tg_writer',
                                 password=password('tg_writer'), dbname='tgarchive',
                                 connect_timeout=5) as db:
                assert db.execute('SELECT inet_client_addr()').fetchone()[0] is None
                assert db.execute('SELECT current_user').fetchone()[0] == 'tg_writer'
            for denied_role in ('tg_owner', 'tg_reader'):
                try:
                    with psycopg.connect(host=str(base / 'socket'), port=port, user=denied_role,
                                         password=password(denied_role), dbname='tgarchive',
                                         connect_timeout=5):
                        raise AssertionError('Unexpected local role access')
                except psycopg.OperationalError:
                    pass
            with socket.socket() as denied:
                denied.settimeout(5)
                denied.bind(('127.0.0.2', 0))
                denied.connect(('127.0.0.1', port))
                startup = struct.pack('!I', 196608) + b'user\0tg_writer\0database\0tgarchive\0\0'
                denied.sendall(struct.pack('!I', len(startup) + 4) + startup)
                assert receive(denied, 1) == b'E'
                length = struct.unpack('!I', receive(denied, 4))[0]
                assert b'pg_hba.conf rejects connection' in receive(denied, length - 4)
            try:
                with socket.create_connection(('127.0.0.2', port), timeout=2):
                    raise AssertionError('Wildcard listener unexpectedly reachable')
            except OSError:
                pass
            print(json.dumps({'fixture': 'rendered-postgres-host-network', 'status': 'ok',
                              'source_preserved': True, 'other_source_denied': True,
                              'local_writer_socket': True, 'local_owner_reader_denied': True,
                              'other_listener_address_unavailable': True, 'persistent_data': False}))
        finally:
            run(compose + ['down', '--remove-orphans'])


if __name__ == '__main__':
    main()
