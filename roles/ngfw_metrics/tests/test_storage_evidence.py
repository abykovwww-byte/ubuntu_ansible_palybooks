"""Real local SQLite/CLI tests; no service, SSH, destination or live data."""
import hashlib
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import yaml

FILES = Path(__file__).resolve().parents[1] / 'files'
sys.path.insert(0, str(FILES))
from metrics_spool import Spool
import storage_evidence as evidence

PAYLOAD = b'test,host=fixture value=1i 1791792000000000001\n'


class StorageEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / 'spool' / 'metrics.sqlite'
        self.spool = Spool(self.source)
        self.minute = int(time.time() // 60) - 5
        for minute, count in enumerate((1, 3, 2)):
            for _ in range(count):
                self.spool.accept(PAYLOAD, now=(self.minute + minute) * 60 + 10)
        self.spool.register('local', 'http://fixture.invalid/write?precision=ns')
        self.spool.deliver_one('local', lambda payload: None)
        self.spool.register('offline', 'http://offline.invalid/write?precision=ns')
        self.output = self.root / 'evidence' / 'request-one'
        self.output.parent.mkdir()

    def collect(self, **kwargs):
        return evidence.collect(self.source, FILES / 'metrics_spool.py', self.output,
                                self.minute * 60, (self.minute + 3) * 60,
                                max_bytes=4 * 1024**2, reserve_bytes=0, **kwargs)

    def read_tables(self, path):
        with closing(sqlite3.connect(path)) as db:
            return {name: db.execute('SELECT * FROM ' + name).fetchall()
                    for name in ('batches', 'cursors', 'ingest_minutes', 'totals')}

    def test_real_backup_exact_payload_and_cursors_with_complete_peak(self):
        before = self.read_tables(self.source)
        result = self.collect()
        self.assertEqual(self.read_tables(self.source), before)
        self.assertEqual(self.read_tables(self.output / 'backup.sqlite'), before)
        self.assertEqual(self.read_tables(self.output / 'working.sqlite'), before)
        self.assertFalse(result['storage_acceptance'])
        self.assertTrue(result['ingest_window_complete'])
        minutes = json.loads((self.output / 'ingest-minutes.json').read_bytes())
        self.assertEqual([row['bytes'] for row in minutes['rows']], [len(PAYLOAD), 3 * len(PAYLOAD), 2 * len(PAYLOAD)])
        self.assertEqual(minutes['queue_24h_payload_bytes_30pct_margin'], 3 * len(PAYLOAD) * 1872)
        self.assertEqual(set(result['files']), set(evidence.EXPORT_FILES) - {'manifest.json'})
        for name, ref in result['files'].items():
            self.assertEqual(hashlib.sha256((self.output / name).read_bytes()).hexdigest(), ref['sha256'])
        # Idempotent readback neither invokes another snapshot nor resumes work.
        self.assertEqual(self.collect(run=mock.Mock(side_effect=AssertionError('must not run'))), result)

    def test_missing_minute_preserved_without_peak_capacity_number(self):
        with self.spool.connect() as db:
            db.execute('DELETE FROM ingest_minutes WHERE minute=?', (self.minute + 1,))
        result = self.collect()
        self.assertFalse(result['ingest_window_complete'])
        minutes = json.loads((self.output / 'ingest-minutes.json').read_bytes())
        self.assertEqual(minutes['missing_minutes'], [self.minute + 1])
        self.assertIsNone(minutes['queue_24h_payload_bytes_30pct_margin'])

    def test_backup_includes_committed_wal_without_repointing_or_stopping_source(self):
        with closing(sqlite3.connect(self.source)) as keeper:
            keeper.execute('SELECT COUNT(*) FROM batches').fetchone()
            extra = b'test,host=fixture value=99i 1791792000000000002\n'
            self.spool.accept(extra, now=(self.minute + 1) * 60 + 20)
            self.assertGreater(Path(str(self.source) + '-wal').stat().st_size, 0)
            self.collect()
            self.assertEqual(self.read_tables(self.output / 'backup.sqlite')['batches'][-1][2], extra)
            self.spool.accept(PAYLOAD)
            self.assertEqual(len(self.read_tables(self.source)['batches']), 8)
            self.assertEqual(len(self.read_tables(self.output / 'backup.sqlite')['batches']), 7)

    def test_excludes_partial_start_and_snapshot_minutes(self):
        self.collect()
        value = evidence.export_minutes(self.output / 'backup.sqlite', self.minute * 60 + 1,
                                        (self.minute + 3) * 60, (self.minute + 2) * 60 + 20,
                                        time.monotonic() + 5)
        self.assertEqual([row['minute'] for row in value['rows']], [self.minute + 1])

    def test_timeout_preserves_partial_and_never_publishes_manifest(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            self.collect(run=mock.Mock(side_effect=subprocess.TimeoutExpired('fixture', 1)))
        self.assertEqual(json.loads((self.output / 'failure.json').read_bytes())['status'], 'INCOMPLETE')
        self.assertFalse((self.output / 'manifest.json').exists())
        with self.assertRaises(FileNotFoundError):
            self.collect()

    def test_changed_completed_artifact_or_request_refused(self):
        self.collect()
        (self.output / 'status.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'existing_artifact_changed'):
            self.collect()

    def test_space_admission_before_capture(self):
        with mock.patch('storage_evidence.shutil.disk_usage', return_value=type('Disk', (), {'free': 0})()):
            with self.assertRaisesRegex(ValueError, 'snapshot_space_reserve'):
                self.collect()
        self.assertFalse(self.output.exists())

    def test_empty_missing_or_future_source_is_not_initialized_or_captured(self):
        empty = self.source.parent / 'empty.sqlite'
        empty.write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'existing_spool_schema_required'):
            evidence.collect(empty, FILES / 'metrics_spool.py', self.output, 1, 120)
        self.assertEqual(empty.read_bytes(), b'')
        self.assertFalse(self.output.exists())
        with self.assertRaisesRegex(ValueError, 'existing_source_required'):
            evidence.collect(self.root / 'missing', FILES / 'metrics_spool.py', self.output, 1, 120)
        with self.assertRaisesRegex(ValueError, 'past_host_interval_required'):
            evidence.collect(self.source, FILES / 'metrics_spool.py', self.output, 1, time.time() + 300)
        self.assertFalse(self.output.exists())


class EvidencePlaybookTests(unittest.TestCase):
    def test_focused_owner_boundary_and_no_runtime_provisioning(self):
        repo = FILES.parents[2]
        play = yaml.safe_load((repo / 'playbooks/ngfw-metrics-evidence.yml').read_text())[0]
        self.assertTrue(play['become'])
        self.assertEqual(play['tasks'][0]['ansible.builtin.import_role']['tasks_from'], 'storage_evidence')
        tasks = yaml.safe_load((FILES.parent / 'tasks/storage_evidence.yml').read_text())
        forbidden = {'ansible.builtin.systemd', 'ansible.builtin.uri', 'ansible.builtin.user',
                     'community.docker.docker_compose_v2', 'community.general.ufw', 'notify'}
        self.assertFalse(any(forbidden & set(task) for task in tasks))
        commands = [task for task in tasks if 'ansible.builtin.command' in task]
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0]['become_user'], 'ngfw-metrics')
        self.assertIn('/usr/bin/timeout', commands[0]['ansible.builtin.command']['argv'])
        self.assertIn('/usr/bin/flock', commands[0]['ansible.builtin.command']['argv'])
        for task in tasks:
            if 'ansible.builtin.file' in task:
                self.assertNotIn('/spool', task['ansible.builtin.file']['path'])
        defaults = yaml.safe_load((FILES.parent / 'defaults/main.yml').read_text())
        self.assertIs(defaults['ngfw_metrics_evidence_enabled'], False)
        self.assertNotIn('storage_evidence', (FILES.parent / 'tasks/main.yml').read_text())


if __name__ == '__main__':
    unittest.main()
