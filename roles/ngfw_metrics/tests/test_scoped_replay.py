"""Temporary SQLite and synthetic LP only; no external endpoint or live data."""
import hashlib
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

FILES = Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0, str(FILES))
from metrics_spool import Spool, file_digest
import metrics_spool
from scoped_replay import prepare, replay, windows_from

URL = 'http://fixture.invalid/write?precision=ns'
LINE_A = b'fixture,host=synthetic\\ host value=1i,label="a b",ok=true 100\r\n'
LINE_B = b'fixture,host=synthetic value=1.25 300'
GAP = b'fixture,host=synthetic value=9i 200\n'


class CutoverTests(unittest.TestCase):
    def test_serve_applies_configured_tail_before_workers_start(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'live.sqlite'; s = Spool(path); s.accept(LINE_A)
            config = {'spool':str(path), 'sinks':[{'name':'local','write_url':URL},
                      {'name':'external','write_url':URL,'initial_cursor':'tail'}]}
            with mock.patch('metrics_spool.threading.Thread'), mock.patch('metrics_spool.BoundedHTTPServer'), mock.patch('builtins.print'):
                metrics_spool.serve(config)
            positions = {c['name']:c['batch_id'] for c in s.status()['sinks']}
            self.assertEqual(positions, {'local':0,'external':1})

    def test_accept_racing_with_cutover_is_delivered_after_the_boundary(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'live.sqlite'
            s = Spool(path); s.accept(LINE_A)
            reached = threading.Event()
            class PausedRegistration(Spool):
                @contextmanager
                def connect(self):
                    with super().connect() as db:
                        def trace(sql):
                            if 'SELECT COALESCE(MAX(id),0)' in sql:
                                reached.set(); time.sleep(.03)
                        db.set_trace_callback(trace)
                        yield db
            registering = PausedRegistration(path)
            errors = []
            def accept():
                try:
                    reached.wait(2); s.accept(LINE_B)
                except Exception as error: errors.append(error)
            thread = threading.Thread(target=accept); thread.start()
            birth = registering.register('external', URL, 'tail')
            thread.join(3)
            self.assertFalse(thread.is_alive()); self.assertEqual(errors, [])
            self.assertEqual(birth['batch_id'], 1)
            delivered = []; s.deliver_one('external', delivered.append)
            self.assertEqual(delivered, [LINE_B])

    def test_tail_is_atomic_birth_policy_not_a_restart_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            s = Spool(Path(temp)/'live.sqlite')
            s.register('local', URL)
            s.accept(LINE_A)
            local_before = s.status()['sinks'][0].copy()
            registration = s.register('external', URL, 'tail')
            self.assertTrue(registration['created'])
            self.assertEqual(registration['batch_id'], 1)
            s.accept(GAP); s.accept(LINE_B)
            got = []
            self.assertEqual(s.deliver_one('external', got.append), len(GAP))
            reopened = Spool(s.path)
            self.assertFalse(reopened.register('external', URL, 'tail')['created'])
            reopened.deliver_one('external', got.append)
            self.assertEqual(got, [GAP, LINE_B])
            local_after = next(x for x in reopened.status()['sinks'] if x['name']=='local')
            for key in ('batch_id','delivered_bytes','failures','last_success'):
                self.assertEqual(local_before[key], local_after[key])
            all_local = []
            while reopened.deliver_one('local', all_local.append):
                pass
            self.assertEqual(all_local, [LINE_A, GAP, LINE_B])

    def test_local_tail_and_destination_replacement_are_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            s = Spool(Path(temp)/'live.sqlite')
            with self.assertRaises(ValueError): s.register('local', URL, 'tail')
            s.register('external', URL, 'tail')
            with self.assertRaises(ValueError): s.register('external', URL+'&db=other', 'tail')
            self.assertEqual(s.status()['sinks'][0]['batch_id'], 0)


class ScopedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.live = Spool(self.root/'live.sqlite')
        self.live.register('local', URL)
        self.live.accept(LINE_A+GAP+LINE_B)
        self.live.accept(GAP)
        self.snapshot = self.root/'backup.sqlite'
        self.live.backup(self.snapshot)
        self.before = file_digest(self.snapshot)
        self.windows = [{'source_start_ns':100,'source_end_ns_exclusive':101},
                        {'source_start_ns':300,'source_end_ns_exclusive':301}]
        self.output = self.root/'selected'

    def test_prepare_preserves_exact_bytes_types_tags_and_only_two_windows(self):
        with mock.patch('scoped_replay.http_sender', side_effect=AssertionError('no network')):
            receipt = prepare(self.snapshot, self.output, self.windows)
        self.assertEqual(receipt['selected_lines'], 2)
        self.assertEqual(receipt['selected_batches'], 1)
        with closing(sqlite3.connect(self.output/'selected.sqlite')) as db:
            self.assertEqual(db.execute('SELECT payload FROM batches').fetchone()[0], LINE_A+LINE_B)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cursors').fetchone()[0], 0)
        self.assertEqual(receipt['ordered_payload_sha256'], hashlib.sha256(LINE_A+LINE_B).hexdigest())
        self.assertEqual(file_digest(self.snapshot), self.before)
        self.assertEqual(self.live.status()['sinks'][0]['batch_id'], 0)
        with self.assertRaises(ValueError): prepare(self.snapshot, self.output, self.windows)

    def test_missing_endpoint_never_registers_cursor_or_sends(self):
        prepare(self.snapshot, self.output, self.windows)
        before = file_digest(self.output/'selected.sqlite')
        with mock.patch('scoped_replay.http_sender') as sender:
            with self.assertRaises(ValueError): replay(self.output, {})
            sender.assert_not_called()
        self.assertEqual(file_digest(self.output/'selected.sqlite'), before)

    def test_cli_prepare_and_disabled_replay_do_not_expose_private_config(self):
        spec = self.root/'windows.json'; spec.write_text(json.dumps(self.windows),encoding='utf-8')
        program = [sys.executable,str(FILES/'scoped_replay.py')]
        result = subprocess.run(program+['prepare','--snapshot',str(self.snapshot),'--windows-file',str(spec),
                                '--directory',str(self.output)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'SELECTED_NOT_SENT')
        private = self.root/'private.json'
        private.write_text(json.dumps({'ingest_token':'synthetic-not-a-real-secret','sinks':[{'name':'local','write_url':URL}]}),encoding='utf-8')
        before = file_digest(self.output/'selected.sqlite')
        result = subprocess.run(program+['replay','--config',str(private),'--directory',str(self.output)],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['error_type'],'ValueError')
        self.assertNotIn('synthetic-not-a-real-secret',result.stdout+result.stderr)
        self.assertEqual(file_digest(self.output/'selected.sqlite'),before)

    def test_failed_ack_keeps_cursor_and_restart_retries_same_bytes(self):
        prepare(self.snapshot, self.output, self.windows)
        received = []
        def lost_ack(body):
            received.append(body)
            raise ConnectionError('private response must not be retained')
        sink = {'write_url':URL, 'retry_seconds':1}
        first = replay(self.output, sink, timeout=.01, send=lost_ack)
        self.assertEqual(first['status'], 'INCOMPLETE_RETRYABLE')
        self.assertEqual(first['sink']['batch_id'], 0)
        self.assertEqual(first['sink']['last_error'], 'ConnectionError')
        second = replay(self.output, sink, send=received.append)
        self.assertEqual(second['status'], 'WRITE_ACKNOWLEDGED_READBACK_REQUIRED')
        self.assertEqual(second['sink']['pending_batches'], 0)
        self.assertEqual(received, [LINE_A+LINE_B, LINE_A+LINE_B])
        self.assertEqual(file_digest(self.snapshot), self.before)
        self.assertEqual(self.live.status()['sinks'][0]['batch_id'], 0)

    def test_replay_recovers_selection_wal_after_process_exit(self):
        prepare(self.snapshot,self.output,self.windows)
        s=Spool(self.output/'selected.sqlite');s.register('scoped_external',URL)
        code="import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1]);d.execute('PRAGMA journal_mode=WAL');d.execute(\"UPDATE cursors SET failures=failures+1\");d.commit();os._exit(7)"
        result=subprocess.run([sys.executable,'-c',code,str(s.path)],capture_output=True,timeout=10)
        self.assertEqual(result.returncode,7)
        got=[];result=replay(self.output,{'write_url':URL},send=got.append)
        self.assertEqual(got,[LINE_A+LINE_B])
        self.assertEqual(result['sink']['pending_batches'],0)
        self.assertEqual(result['sink']['failures'],1)
        self.assertEqual(file_digest(self.snapshot),self.before)

    def test_corrupt_or_empty_or_live_source_is_not_a_selection(self):
        with self.assertRaises(ValueError): prepare(self.live.path, self.output, self.windows)
        with self.assertRaises(ValueError): prepare(self.snapshot, self.output, [{'source_start_ns':500,'source_end_ns_exclusive':600}])
        other = self.root/'other'
        self.snapshot.write_bytes(self.snapshot.read_bytes()+b'tamper')
        with self.assertRaises(ValueError): prepare(self.snapshot, other, self.windows)

    def test_window_validation_and_selected_tamper(self):
        for spec in ([], [{'source_start_ns':1.0,'source_end_ns_exclusive':2}],
                     [{'source_start_ns':1,'source_end_ns_exclusive':3},{'source_start_ns':2,'source_end_ns_exclusive':4}]):
            with self.assertRaises(ValueError): windows_from(spec)
        prepare(self.snapshot, self.output, self.windows)
        s = Spool(self.output/'selected.sqlite'); s.accept(GAP)
        with self.assertRaises(ValueError): replay(self.output, {'write_url':URL}, send=lambda _:None)
