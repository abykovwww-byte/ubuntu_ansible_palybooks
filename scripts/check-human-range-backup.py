"""Run the shipped backup/restore scripts against an isolated PostgreSQL container."""
import fcntl
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

from jinja2 import Environment, StrictUndefined


ROOT = Path(__file__).resolve().parents[1]


def main():
    with tempfile.TemporaryDirectory(prefix="human-range-backup-") as directory:
        project = Path(directory)
        backups = project / "backups"
        backups.mkdir()
        (project / "docker-compose.yml").write_text("""services:
  db:
    image: postgres:16-alpine
    environment:
      POSTGRES_USER: human_range
      POSTGRES_DB: human_range
      POSTGRES_HOST_AUTH_METHOD: trust
    healthcheck:
      test: [CMD-SHELL, pg_isready -U human_range -d human_range]
      interval: 1s
      timeout: 3s
      retries: 40
""", encoding="utf-8")
        jinja = Environment(undefined=StrictUndefined)
        jinja.filters["quote"] = shlex.quote
        for command in ("backup", "restore-check"):
            template = ROOT / f"roles/apps/templates/human-range-{command}.sh.j2"
            rendered = jinja.from_string(template.read_text()).render(
                human_range_project_dir=str(project), human_range_backup_dir=str(backups))
            (project / f"{command}.sh").write_text(rendered, encoding="utf-8")
        compose = ["docker", "compose", "--file", str(project / "docker-compose.yml")]
        processes = []
        try:
            subprocess.run(compose + ["up", "-d", "--wait", "db"], check=True)
            sql = "CREATE TABLE alembic_version(version_num text); INSERT INTO alembic_version VALUES ('backup-proof');"
            sql += "".join(f"CREATE TABLE {name}(id int); INSERT INTO {name} VALUES (1);" for name in
                           ("run", "event", "run_result", "run_result_revision"))
            subprocess.run(compose + ["exec", "-T", "db", "psql", "-U", "human_range", "-d", "human_range", "-v", "ON_ERROR_STOP=1"], input=sql, text=True, check=True)
            with (backups / ".backup.lock").open("w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                processes = [subprocess.Popen(["sh", str(project / "backup.sh")], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(2)]
                time.sleep(0.5)
                assert all(process.poll() is None for process in processes), "Backups must wait for the shared lock"
                assert not list(backups.glob("*.part")), "Dump must not begin before obtaining the lock"
                fcntl.flock(lock, fcntl.LOCK_UN)
            archives = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=90)
                assert process.returncode == 0, stderr
                archives.append(Path(stdout.strip()))
            assert len(set(archives)) == 2 and all(path.stat().st_size > 0 for path in archives)
            assert not list(backups.glob("*.part"))
            for archive in archives:
                result = subprocess.run(["sh", str(project / "restore-check.sh"), str(archive)], capture_output=True, text=True, check=True)
                assert "backup-proof" in result.stdout and "Restore into a separate empty database succeeded" in result.stdout
            result = subprocess.run(compose + ["exec", "-T", "db", "psql", "-U", "human_range", "-d", "human_range", "-Atc", "SELECT count(*) FROM event; SELECT count(*) FROM pg_database WHERE datname LIKE 'human_range_restore_probe_%';"], capture_output=True, text=True, check=True)
            assert result.stdout.splitlines() == ["1", "0"], "Source data preserved and restore databases removed"
            subprocess.run(compose + ["stop", "db"], check=True)
            failed = subprocess.run(["sh", str(project / "backup.sh")], capture_output=True, text=True, timeout=30)
            assert failed.returncode != 0
            assert not list(backups.glob("*.part")) and len(list(backups.glob("*.dump"))) == 2
            print("Two locked concurrent backups restored successfully; source preserved; failed dump published no archive.")
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)
            subprocess.run(compose + ["down", "--volumes"], check=True)


if __name__ == "__main__":
    main()
