import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from collections import Counter
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(ROOT))
from extreme import held_evidence as h
from extreme import controller as c
sys.path.insert(0,str(ROOT/'traffic'))
from held_receiver import Receiver
from native_analysis.normalize import normalize_window


def fixture():
    offered={'kind':'held','sessions':2,'open_rate':2,'heartbeat_seconds':5}
    final={'record_type':'final','run_id':'held-A','kind':'held','measure_start':100,'time_end':400,
           'requested_sessions':2,'opened_sessions':2,'minimum_active_measurement_sessions':2,
           'active_sessions_at_measure_end':2,'confirmed_measurement_sessions':2,
           'stale_echo_sessions':0,'errors':0,'pending_requests':0,'censored_at_boundary':0}
    key=str(int.from_bytes(hashlib.sha256(b'held-A').digest()[:8],'big'))
    rows=[]
    for stamp in range(90,411,10):
        run={'connections':2,'active':2 if stamp<=400 else 0,'peak':2,'peak_reached_at':99,
             'first_disconnect_at':None if stamp<=400 else 400.1,
             'messages':max(0,(stamp-90)//5)*2,'minimum_echoes':max(1,(stamp-90)//5)}
        rows.append({'collected_at':stamp+.1,'receiver':{'wall_time':stamp,'kind':'held-session-receiver',
            'status':'running','receiver_instance':'a'*32,'runs':{} if stamp<100 else {key:run}}})
    return offered,final,rows,key


class HeldEvidenceTests(unittest.TestCase):
    def test_oversized_raw_proof_or_rows_rejected_before_decode(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'oversized.json'
            with path.open('wb') as stream:stream.truncate(h.MAX_EVIDENCE_BYTES+1)
            with self.assertRaisesRegex(ValueError,'16 MiB'):h.reference(path)
            with self.assertRaisesRegex(ValueError,'16 MiB'):
                h.referenced({'path':str(path),'sha256':'0'*64},directory)

    def test_receiver_minimum_advances_only_when_each_connection_echoes(self):
        receiver=Receiver.__new__(Receiver)
        receiver.state={'messages':0,'runs':{'r':{'messages':0,'minimum_echoes':0}}}
        receiver.echo_rounds={'r':Counter()}
        receiver._record_echo('r',-1,0)
        receiver._record_echo('r',-1,0)
        receiver._record_echo('r',0,1)
        self.assertEqual(receiver.state['runs']['r']['minimum_echoes'],1)
        receiver._record_echo('r',1,2)
        self.assertEqual(receiver.state['runs']['r']['minimum_echoes'],1)
        receiver._record_echo('r',0,1)
        self.assertEqual(receiver.state['runs']['r']['minimum_echoes'],2)
        self.assertEqual(sum(receiver.echo_rounds['r'].values()),2)
        self.assertEqual(receiver.state['messages'],5)

    def test_valid_independent_window_all_connections_and_teardown_after_end(self):
        offered,final,rows,_=fixture()
        proof=h.validate(rows,final,offered,'held-A',20)
        self.assertEqual(proof['status'],'VALID')
        self.assertEqual(proof['inside_samples'],31)

    def test_global_samples_cannot_hide_missing_target_run_between_boundaries(self):
        offered,final,rows,key=fixture()
        for row in rows:
            if 100<row['receiver']['wall_time']<400:row['receiver']['runs'].pop(key)
        with self.assertRaisesRegex(ValueError,'target run missing'):
            h.validate(rows,final,offered,'held-A',20)

    def test_failed_final_readback_preserves_primary_safety_abort(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=object.__new__(c.NativeRunner)
            monitor.directory=Path(directory);monitor.backend=SimpleNamespace(endpoint=Mock(side_effect=TimeoutError))
            monitor.native_health=Path(directory)/'health';monitor.boot_id='boot';monitor.context={};monitor.held_capture=True
            with patch.object(c,'validate_native',return_value={}),patch.object(c.legacy.Runner,'monitor',side_effect=c.legacy.Abort('safety original')):
                with self.assertRaisesRegex(c.legacy.Abort,'safety original'):monitor.monitor()
            row=json.loads((monitor.directory/'held-receiver.ndjson').read_text())
            self.assertEqual(row['error'],'TimeoutError')
            monitor.capture_held_receiver=Mock(side_effect=[None,c.Blocked('terminal failure')])
            with patch.object(c,'execute_components',side_effect=c.legacy.Abort('load safety original')),patch.object(c.time,'sleep'):
                with self.assertRaisesRegex(c.legacy.Abort,'load safety original'):
                    c.execute_measurement(monitor,{'offered':{'kind':'held'},'measure_seconds':300},{},{})
            self.assertFalse(monitor.held_capture)
            monitor.capture_held_receiver=Mock(side_effect=[None,c.Blocked('terminal failure')])
            with patch.object(c,'execute_components'),patch.object(c.time,'sleep'):
                with self.assertRaisesRegex(c.Blocked,'terminal failure'):
                    c.execute_measurement(monitor,{'offered':{'kind':'held'},'measure_seconds':300},{},{})

    def test_restart_stale_gap_wrong_run_early_disconnect_and_stalled_echo_reject(self):
        for fault in ('restart','stale','gap','run','disconnect','echo','population','unbracketed','failed'):
            with self.subTest(fault=fault):
                offered,final,rows,key=fixture()
                if fault=='restart':rows[10]['receiver']['receiver_instance']='b'*32
                if fault=='stale':rows[10]['collected_at']+=4
                if fault=='gap':del rows[8:11]
                if fault=='run':
                    for row in rows:row['receiver']['runs']={}
                if fault=='disconnect':rows[-1]['receiver']['runs'][key]['first_disconnect_at']=399
                if fault=='echo':
                    for row in rows[1:]:row['receiver']['runs'][key]['minimum_echoes']=2
                if fault=='population':rows[10]['receiver']['runs'][key]['active']=1
                if fault=='unbracketed':rows=rows[2:]
                if fault=='failed':rows[10]['error']='TimeoutError'
                with self.assertRaises(ValueError):h.validate(rows,final,offered,'held-A',20)

    def test_proof_rechecks_both_raw_sources_and_never_trusts_boolean(self):
        offered,final,rows,_=fixture()
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);raw=json.dumps(final).encode()
            workload=base/'held.jsonl';workload.write_bytes(raw)
            receiver=base/'receiver.ndjson';receiver.write_text('\n'.join(map(json.dumps,rows)))
            proof=h.validate(rows,final,offered,'held-A',20)
            proof.update(workload_evidence=h.reference(workload),receiver_evidence=h.reference(receiver))
            path=base/'proof.json';path.write_text(json.dumps(proof));ref=h.reference(path)
            self.assertEqual(h.verify(ref,base,raw,offered,'held-A')['status'],'VALID')
            receiver.write_text(receiver.read_text()+'\n{}')
            with self.assertRaisesRegex(ValueError,'hash differs'):h.verify(ref,base,raw,offered,'held-A')

    def test_normalizer_joins_real_proof_and_rejects_boolean_without_it(self):
        offered,final,rows,_=fixture()
        final.update(seconds=300,successful_requests=120,attempted_requests=120,success_rate=.4,
                     latency_histogram={'bounds_ms':[1],'counts':[120,0],'cumulative':False,
                                       'population':'successful_requests','overflow':'unbounded'})
        window={'run_id':'held-A','workload':'held','profile':'A','offered':offered,
                'block_id':'synthetic-block','position':0,'repeat':1}
        receipt={'run_id':'held-A','workload':'held','profile':'A','offered_load':offered,
                 'versions':{'ngfw':'SYNTHETIC'},'config_hashes':{k:'a'*64 for k in c.plan.HASH_FIELDS},
                 'profile_evidence':{'edr_installed':False,'audit_profile':'stock'},
                 'valid':True,'window_validated':True,'regime':'stable','outcome':'STABLE_LIMIT_NOT_ESTABLISHED',
                 'evidence':['SYNTHETIC'],'native_completeness':{'expected':1,'observed':1,'missing':0,'evidence':['SYNTHETIC']},
                 'held_receiver_window_validated':True}
        with tempfile.TemporaryDirectory() as directory:
            base=Path(directory);work=base/'work.jsonl';work.write_text(json.dumps(final))
            self.assertIn('independent_held_receiver_window_unverified',normalize_window(window,work,receipt)['normalization_errors'])
            rec=base/'receiver.ndjson';rec.write_text('\n'.join(map(json.dumps,rows)))
            proof=h.validate(rows,final,offered,'held-A',20)
            proof.update(workload_evidence=h.reference(work),receiver_evidence=h.reference(rec))
            path=base/'proof.json';path.write_text(json.dumps(proof))
            receipt['held_receiver_evidence']=h.reference(path)
            actual=normalize_window(window,work,receipt)
            self.assertTrue(actual['valid'],actual['normalization_errors'])

    def test_failed_probe_saved_before_raise_and_request_is_fixed(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor=SimpleNamespace(directory=Path(directory),backend=SimpleNamespace(endpoint=Mock(side_effect=TimeoutError)))
            with self.assertRaises(c.Blocked):c.NativeRunner.capture_held_receiver(monitor)
            row=json.loads((monitor.directory/'held-receiver.ndjson').read_text())
            self.assertEqual(row['error'],'TimeoutError')
            monitor.backend.endpoint.assert_called_once_with('traffic-server',['python3','-c',h.PROBE],timeout=5)


if __name__=='__main__':unittest.main()
