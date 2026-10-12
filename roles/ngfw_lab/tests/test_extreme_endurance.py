import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]/'files'
sys.path.insert(0,str(ROOT))
from extreme import controller as c, held_evidence as h


class EnduranceTests(unittest.TestCase):
    def test_endurance_gate_invoked_after_full_duration_control_and_comparison_unaffected(self):
        from test_extreme_controller import IdentityTests, file_ref
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            campaign,window,backend,control=IdentityTests().prerequisites(base)
            with patch.object(c,'validate_stable_window',side_effect=c.Blocked('stable_missing')) as check:
                c.validate_campaign_artifacts(campaign,window,base,backend,{'ngfw_vm':'lab-vm'},1000)
                check.assert_not_called()
                window.update(phase='endurance',measure_seconds=3600)
                with self.assertRaisesRegex(c.Blocked,'direct-path'):
                    c.validate_campaign_artifacts(campaign,window,base,backend,{'ngfw_vm':'lab-vm'},1000)
                check.assert_not_called()
                control['duration_seconds']=3600
                campaign['workloads'][0]['control_capacity']['receipt']=file_ref(base,'control.json',control)
                with self.assertRaisesRegex(c.Blocked,'stable_missing'):
                    c.validate_campaign_artifacts(campaign,window,base,backend,{'ngfw_vm':'lab-vm'},1000)
                check.assert_called_once()

    def fixture(self,base,fault=None,kind='tcp'):
        offered={'kind':'tcp','bps':900000000,'parallel':4}
        campaign={'target':'10.77.20.10','invariants':{k:'a'*64 for k in c.plan.HASH_FIELDS},
                  'profiles':{'A':{'edr_installed':False,'audit_profile':'stock'}}}
        raw={'start':{'timestamp':{'timesecs':100},'test_start':{'protocol':'TCP','num_streams':4,'target_bitrate':225000000,'duration':300}},
             'end':{'sum_received':{'seconds':300,'start':0,'end':300,'bytes':33750000000}}}
        if kind=='http':
            offered={'kind':'http','rate':100,'concurrency':10}
            raw={'record_type':'final','run_id':'prior-A','kind':'http','measure_start':100,'time_end':400,
                 'seconds':300,'successful_requests':30000,'attempted_requests':30000,'errors':0,
                 'requested_rate':100,'success_rate':100,'pending_requests':0,'censored_at_boundary':0,
                 'latency_histogram':{'bounds_ms':[1],'counts':[30000,0],'cumulative':False,
                                      'population':'successful_requests','overflow':'unbounded'}}
            if fault=='errors':raw.update(errors=300,attempted_requests=30300)
        if fault=='underachieved':raw['end']['sum_received']['bytes']=100
        if fault=='duration':raw['end']['sum_received'].update(seconds=299,end=299)
        if fault=='actual_load':raw['start']['test_start']['target_bitrate']=1
        # Real finalizers preserve relocated bytes; component paths still name the original window.
        evidence_dir=base/'finalization'/'evidence';evidence_dir.mkdir(parents=True)
        work=evidence_dir/'preserved-work.json';work.write_text(json.dumps(raw))
        original={'run_id':'prior-A','profile':'A','offered_load':offered,'config_hashes':campaign['invariants'],
                  'process_start':'1970-01-01T00:01:30+00:00',
                  'profile_evidence':campaign['profiles']['A'],'outcome':'COMPLETED','recovery':{'status':'RECOVERED'},
                  'components':[{'kind':kind,'phase':'measure','path':'measure-00-'+kind+'.jsonl','exit_code':0,
                                 'argv':c.plan.commands(offered,campaign['target'],300,'prior-A')[0]}]}
        if fault=='source_profile':original['profile']='B'
        if fault=='recovery':original['recovery']['status']='SAFETY_STOP'
        if fault=='invocation':original['components'][0]['argv'][-2:]=['-b','1']
        source=evidence_dir/'original-controller-source.json';source.write_text(json.dumps(original))
        criterion={'offered':offered,'duration_seconds':300,'declared_at':'1970-01-01T00:00:50+00:00',
                   'thresholds':{'success_ratio':.95,'max_error_ratio':.005,'max_udp_loss_pct':.5,
                       'min_fd_reserve':4096,'min_tuple_reserve':4096,'max_memory_ratio':.85,
                       'max_pids_ratio':.85,'max_throttled_ratio':.01,'host_memory_stop_pct':90,
                       'host_disk_stop_pct':80,'sample_seconds':10,'temperature_stop_c':95}}
        if fault=='criterion_load':criterion['offered']={'kind':'tcp','bps':1,'parallel':4}
        if fault=='criterion_duration':criterion['duration_seconds']=301
        if fault=='criterion_late':criterion['declared_at']='1970-01-01T00:02:00+00:00'
        if fault=='criterion_relaxed':criterion['thresholds']['success_ratio']=.1
        criterion_path=evidence_dir/'prior-criterion.json';criterion_path.write_text(json.dumps(criterion))
        stable=copy.deepcopy(original);stable.update(profile='A',valid=True,window_validated=True,regime='stable',
            outcome='STABLE_LIMIT_NOT_ESTABLISHED',original_controller=h.reference(source),
            stable_criterion=h.reference(criterion_path),
            stable_workload_evidence=[{'component_path':original['components'][0]['path'],'kind':kind,'reference':h.reference(work)}],
            native_completeness={'status':'COMPLETE_WITHIN_DECLARED_CATALOG_AND_CADENCE','scope':'native_telemetry','expected':2,'observed':2,'missing':0,
                                 'evidence':[h.reference(work)],'gaps':[]})
        if fault=='mapping_missing':stable.pop('stable_workload_evidence')
        if fault=='mapping_path':stable['stable_workload_evidence'][0]['component_path']='other.json'
        if fault=='mapping_kind':stable['stable_workload_evidence'][0]['kind']='udp'
        if fault=='mapping_hash':stable['stable_workload_evidence'][0]['reference']['sha256']='0'*64
        if fault=='criterion_missing':stable.pop('stable_criterion')
        if fault=='criterion_hash':stable['stable_criterion']['sha256']='0'*64
        if fault=='native_status':stable['native_completeness']['status']='INCOMPLETE'
        if fault=='profile':stable['profile']='B'
        if fault=='load':stable['offered_load']=dict(offered,bps=800000000)
        if fault=='outcome':stable['outcome']='COMPLETED'
        if fault=='validation':stable['window_validated']=False
        if fault=='source_hash':stable['original_controller']['sha256']='0'*64
        if fault=='workload_hash':stable['native_completeness']['evidence'][0]['sha256']='0'*64
        if fault=='native':stable['native_completeness']['missing']=1
        path=base/'stable.json';path.write_text(json.dumps(stable))
        window={'run_id':'endurance-A','profile':'A','offered':offered,'phase':'endurance','stable_receipt':h.reference(path)}
        if fault=='stable_hash':window['stable_receipt']['sha256']='0'*64
        return campaign,window

    def test_hashed_prior_stable_actual_window_accepted_without_maximum_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);campaign,window=self.fixture(base)
            proof=c.validate_stable_window(campaign,window,base,500)
            self.assertEqual(proof['actual_component_seconds'],[300])
            self.assertEqual(proof['scope'],'validated_stable_load_not_maximum_capacity')

    def test_source_hash_profile_load_outcome_duration_and_native_fail_closed(self):
        for fault in ('profile','load','outcome','validation','source_profile','source_hash','workload_hash',
                      'stable_hash','duration','actual_load','native','recovery','invocation','underachieved',
                      'mapping_missing','mapping_path','mapping_kind','mapping_hash','criterion_missing',
                      'criterion_hash','criterion_load','criterion_duration','criterion_late','criterion_relaxed','native_status'):
            with self.subTest(fault=fault),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);campaign,window=self.fixture(base,fault)
                with self.assertRaises((c.Blocked,ValueError)):c.validate_stable_window(campaign,window,base,500)

    def test_request_errors_recomputed_even_when_successful_rate_meets_target(self):
        for fault in (None,'errors'):
            with tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);campaign,window=self.fixture(base,fault,'http')
                if fault:
                    with self.assertRaisesRegex(c.Blocked,'application_error_ratio'):
                        c.validate_stable_window(campaign,window,base,500)
                else:self.assertEqual(c.validate_stable_window(campaign,window,base,500)['actual_component_seconds'],[300])

    def mixed_fixture(self,base,shift):
            campaign,window=self.fixture(base)
            stable_path=Path(window['stable_receipt']['path']);stable=json.loads(stable_path.read_bytes())
            source_path=Path(stable['original_controller']['path']);source=json.loads(source_path.read_bytes())
            criterion_path=Path(stable['stable_criterion']['path']);criterion=json.loads(criterion_path.read_bytes())
            offered={'kind':'mixed','components':[window['offered'],
                {'kind':'http','rate':100,'concurrency':10},{'kind':'dns','rate':100,'concurrency':10}]}
            argv=c.plan.commands(offered,campaign['target'],300,'prior-A')
            source['components'][0].update(argv=argv[0],requested_duration_seconds=302)
            tcp_path=Path(stable['stable_workload_evidence'][0]['reference']['path'])
            tcp=json.loads(tcp_path.read_bytes());tcp['start']['test_start']['duration']=302
            tcp['end']['sum_received'].update(seconds=302,end=302,bytes=33975000000)
            tcp_path.write_text(json.dumps(tcp))
            stable['stable_workload_evidence'][0]['reference']=h.reference(tcp_path)
            stable['native_completeness']['evidence'][0]=h.reference(tcp_path)
            for index,kind in ((1,'http'),(2,'dns')):
                raw={'record_type':'final','run_id':'prior-A','kind':kind,'measure_start':100+shift,'time_end':402+shift,
                     'seconds':302,'successful_requests':30200,'attempted_requests':30200,'errors':0,
                     'requested_rate':100,'success_rate':100,'pending_requests':0,'censored_at_boundary':0,
                     'latency_histogram':{'bounds_ms':[1],'counts':[30200,0],'cumulative':False,
                                         'population':'successful_requests','overflow':'unbounded'}}
                path=source_path.parent/(kind+'.jsonl');path.write_text(json.dumps(raw))
                component={'kind':kind,'phase':'measure','path':f'measure-{index:02d}-{kind}.jsonl','exit_code':0,
                           'argv':argv[index],'requested_duration_seconds':302}
                source['components'].append(component)
                stable['stable_workload_evidence'].append({'component_path':component['path'],'kind':kind,'reference':h.reference(path)})
                stable['native_completeness']['evidence'].append(h.reference(path))
            source.update(offered_load=offered,measure_seconds=300,component_measure_seconds=302);source_path.write_text(json.dumps(source))
            stable.update(components=source['components'],offered_load=offered,original_controller=h.reference(source_path),
                          measure_seconds=300,component_measure_seconds=302)
            criterion.update(offered=offered,component_measure_seconds=302);criterion_path.write_text(json.dumps(criterion))
            stable['stable_criterion']=h.reference(criterion_path);stable_path.write_text(json.dumps(stable))
            window.update(offered=offered,stable_receipt=h.reference(stable_path))
            return campaign,window,source,stable

    def test_mixed_padded_actual_work_and_common_interval_both_required(self):
        for shift in (.1,1.1):
            with self.subTest(shift=shift),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);campaign,window,source,stable=self.mixed_fixture(base,shift)
                if shift==.1:
                    self.assertEqual(c.validate_stable_window(campaign,window,base,500)['actual_component_seconds'],[302]*3)
                else:
                    with self.assertRaisesRegex(c.Blocked,'mixed full concurrent duration'):
                        c.validate_stable_window(campaign,window,base,500)

    def test_direct_and_controller_use_actual_mixed_union_and_common_interval(self):
        from extreme import control_capacity as cap
        for shift in (.1,1.1):
            with self.subTest(shift=shift),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);campaign,window,source,stable=self.mixed_fixture(base,shift)
                for comp,evidence in zip(source['components'],stable['stable_workload_evidence']):
                    (base/comp['path']).write_bytes(Path(evidence['reference']['path']).read_bytes())
                criterion=json.loads(Path(stable['stable_criterion']['path']).read_bytes())
                actual,errors=cap.measure_results(source,base,window['offered'],300,criterion['thresholds'],[])
                selected=dict(window,run_id=source['run_id'],measure_seconds=300,component_measure_seconds=302)
                monitor=SimpleNamespace(directory=base)
                with patch.object(c,'execute_components'):
                    if shift==.1:c.execute_measurement(monitor,selected,campaign,source)
                    else:
                        with self.assertRaisesRegex(c.Blocked,'mixed full concurrent duration'):
                            c.execute_measurement(monitor,selected,campaign,source)
                from native_analysis.normalize import normalize_campaign
                receipt_path=base/'mixed-receipt.json';receipt_path.write_text(json.dumps(source))
                normalized=normalize_campaign({'windows':[selected]},[receipt_path.name],base)['runs']
                if shift==.1:
                    self.assertEqual(errors,[])
                    self.assertAlmostEqual(source['mixed_measurement_window']['conservative_common_seconds'],300.9)
                    self.assertAlmostEqual(source['mixed_measurement_window']['export_union_end_epoch_seconds'],402.1)
                    self.assertTrue(all('mixed_measurement_window' in row for row in normalized))
                else:
                    self.assertTrue(any('mixed_timing_invalid' in e for e in errors))
                    self.assertTrue(all('mixed_full_concurrent_window_unverified' in row['normalization_errors'] for row in normalized))
if __name__=='__main__':unittest.main()
