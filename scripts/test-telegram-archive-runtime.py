"""Exercise the actual PostgreSQL privilege boundary in an isolated fixture.

Linux + Docker only. No published ports, network, persistent volume or real secrets.
"""
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, check=True):
    result = subprocess.run(['docker', *args], text=True, capture_output=True, timeout=120)
    if check and result.returncode:
        raise RuntimeError('Fixture Docker command failed: ' + args[0])
    return result


def main():
    name = 'tgarchive-fixture-' + uuid.uuid4().hex[:12]
    checks = 0
    with tempfile.TemporaryDirectory(prefix='tgarchive-fixture-') as directory:
        base = Path(directory)
        base.chmod(0o755)
        config = base / 'config'
        secrets = base / 'secrets'
        config.mkdir(mode=0o755)
        secrets.mkdir(mode=0o755)
        for role in ('postgres', 'tg_owner', 'tg_writer', 'tg_reader'):
            (secrets / (role + '_password')).write_text('SYNTHETIC-' + role + '-fixture-password-12345678', encoding='utf-8')
        sql = (ROOT / 'roles/telegram_archive/templates/bootstrap.sql.j2').read_text(encoding='utf-8')
        (config / 'bootstrap.sql').write_text(sql, encoding='utf-8')
        (config / 'pg_hba.conf').write_text(
            'local all postgres peer\nlocal all all reject\n'
            'host tgarchive tg_owner,tg_writer,tg_reader 127.0.0.1/32 scram-sha-256\n'
            'host all all 0.0.0.0/0 reject\nhost all all ::/0 reject\n', encoding='utf-8')
        try:
            docker('run', '-d', '--name', name, '--network', 'none',
                   '--memory', '512m', '--cpus', '1',
                   '--tmpfs', '/var/lib/postgresql/data:rw,nosuid,size=128m',
                   '-v', f'{config}:/etc/fixture:ro', '-v', f'{secrets}:/run/secrets:ro',
                   '-e', 'POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password',
                   '-e', 'POSTGRES_INITDB_ARGS=--auth-local=peer --auth-host=scram-sha-256',
                   'postgres:17.11-bookworm', 'postgres',
                   '-c', 'hba_file=/etc/fixture/pg_hba.conf',
                   '-c', 'password_encryption=scram-sha-256',
                   '-c', 'log_min_error_statement=panic')
            for _ in range(60):
                if docker('exec', name, 'pg_isready', '-U', 'postgres', check=False).returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Fixture did not become ready')
            for _ in range(2):
                docker('exec', '-u', 'postgres', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                       '-d', 'postgres', '-f', '/etc/fixture/bootstrap.sql')

            def query(role, text, succeeds=True, expected=None):
                nonlocal checks
                result = docker('exec', '-e', 'PGPASSWORD=SYNTHETIC-' + role + '-fixture-password-12345678',
                                name, 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                                '-h', '127.0.0.1', '-U', role, '-d', 'tgarchive', '-c', text, check=False)
                if (result.returncode == 0) != succeeds:
                    raise AssertionError('Unexpected privilege result: ' + role + ' check ' + str(checks))
                if expected is not None and result.stdout.strip() != expected:
                    raise AssertionError('Unexpected read-back: ' + role + ' check ' + str(checks))
                checks += 1

            query('tg_owner', 'CREATE TABLE boundary_fixture (id bigserial PRIMARY KEY, value text NOT NULL)')
            query('tg_writer', "INSERT INTO boundary_fixture(value) VALUES ('synthetic')")
            query('tg_reader', 'SELECT count(*) FROM boundary_fixture', expected='1')
            query('tg_reader', 'SHOW default_transaction_read_only', expected='on')
            query('tg_reader', 'SHOW statement_timeout', expected='5s')
            query('tg_reader', "INSERT INTO boundary_fixture(value) VALUES ('blocked')", False)
            query('tg_reader', "SET default_transaction_read_only=off; BEGIN READ WRITE; INSERT INTO boundary_fixture(value) VALUES ('blocked')", False)
            query('tg_reader', 'DELETE FROM boundary_fixture', False)
            query('tg_writer', 'CREATE TABLE forbidden (id int)', False)
            query('tg_writer', 'CREATE SCHEMA forbidden', False)
            query('tg_writer', 'CREATE TEMP TABLE forbidden (id int)', False)
            query('tg_writer', 'ALTER TABLE boundary_fixture ADD COLUMN forbidden int', False)
            query('tg_writer', 'DROP TABLE boundary_fixture', False)
            query('tg_writer', 'SET ROLE tg_owner', False)
            query('tg_writer', 'CREATE DATABASE forbidden', False)
            query('tg_writer', "UPDATE boundary_fixture SET value='updated' WHERE id=1")
            query('tg_reader', 'SELECT value FROM boundary_fixture WHERE id=1', expected='updated')
            query('tg_owner', 'CREATE TABLE future_fixture (value int)')
            query('tg_writer', 'INSERT INTO future_fixture VALUES (7)')
            query('tg_reader', 'SELECT value FROM future_fixture', expected='7')
            query('tg_writer', 'DELETE FROM boundary_fixture')
            query('tg_reader', 'SELECT count(*) FROM boundary_fixture', expected='0')
            # Convergence must also remove accidental direct grants on existing data.
            docker('exec', '-u', 'postgres', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                   '-d', 'tgarchive', '-c',
                   'GRANT INSERT, DELETE ON future_fixture TO tg_reader; '
                   'GRANT CREATE ON SCHEMA public TO tg_writer; '
                   'GRANT TEMPORARY ON DATABASE tgarchive TO tg_writer; '
                   'GRANT tg_owner TO tg_reader;')
            docker('exec', '-u', 'postgres', name, 'psql', '-X', '-v', 'ON_ERROR_STOP=1',
                   '-d', 'postgres', '-f', '/etc/fixture/bootstrap.sql')
            query('tg_reader', 'SELECT value FROM future_fixture', expected='7')
            query('tg_reader', 'SET default_transaction_read_only=off; DELETE FROM future_fixture', False)
            query('tg_reader', 'SET default_transaction_read_only=off; INSERT INTO future_fixture VALUES (9)', False)
            query('tg_writer', 'CREATE TABLE forbidden (id int)', False)
            query('tg_writer', 'CREATE TEMP TABLE forbidden (id int)', False)
            query('tg_reader', 'SET ROLE tg_owner', False)
            print(json.dumps({'fixture': 'isolated-postgresql', 'checks': checks,
                              'bootstrap_runs': 3, 'status': 'ok', 'real_archive_data': False}))
        finally:
            docker('rm', '-f', name, check=False)
            if docker('inspect', name, check=False).returncode == 0:
                raise RuntimeError('Fixture container cleanup failed')


if __name__ == '__main__':
    main()
