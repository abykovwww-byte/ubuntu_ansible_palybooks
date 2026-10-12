"""Synthetic native latest generations; no live workload or collector calls."""
import base64,copy,hashlib,json,os,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from test_extreme_controller import c, file_ref, timestamp
class Clock:
 def __init__(self):self.now=0;self.on_sleep=lambda:None
 def wall(self):return 1010.1+self.now
 def monotonic(self):return self.now
 def sleep(self,seconds):self.now+=seconds;self.on_sleep()
class NativeRefreshTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.path=self.root/'latest.json';self.clock=Clock();self.events=[]
  self.catalog=self.ref(self.root,'catalog.json',dict(schema_version=1,database='lab',source_interval_seconds=10,
    host_selector={'tag':'host','value':'synthetic'},measurements=[{'name':'native_dp','field':'value'},{'name':'threadstat','field':'cpu'}],
    interval_evidence=self.ref(self.root,'interval.json',{'synthetic':True,'interval':10}),
    observation_evidence=self.ref(self.root,'observation.json',{'synthetic':True})))
  self.old=self.generation('old',0);self.new=self.generation('new',10)
  self.publish(self.old)
 def ref(self,root,name,value):
  ref=file_ref(root,name,value);ref['path']=str(root/name);return ref
 def generation(self,name,shift,points=None,boot='boot'):
  root=self.root/name;root.mkdir();ns=int(shift*1e9)
  catalog=json.loads(Path(self.catalog['path']).read_bytes())
  bounds=dict(started_epoch_ns=998000000000+ns,completed_epoch_ns=998200000000+ns,started_monotonic_ns=98000000000+ns,completed_monotonic_ns=98200000000+ns)
  points=points or [980+shift,985+shift]
  raw=dict(schema_version=2,database='lab',guest_boot_id=boot,partial=False,query_bounds=bounds,evaluated_at_epoch_ns=999000000000+ns,catalog_evidence=self.catalog,
    points=[dict(measurement=name,selected_field=field,time=timestamp(stamp),source_timestamp_ns=int(stamp*1e9)) for name,field,stamp in zip(('native_dp','threadstat'),('value','cpu'),points)])
  query=self.ref(root,'query.json',raw)
  alignment=dict(schema_version=1,scope='bounded_clock_alignment_not_ntp_synchronization',guest_boot_id=boot,query_bounds=bounds,query_evidence_sha256=query['sha256'],offset_min_ns=-50000000,offset_max_ns=50000000,limits=dict(max_uncertainty_seconds=1,max_offset_spread_seconds=.5,max_clock_step_seconds=.05))
  for side,send in (('before',997800000000+ns),('after',998300000000+ns)):
   guest=dict(guest_epoch_ns=send+50000000,guest_monotonic_ns=send-900000000000+50000000,guest_boot_id=boot)
   alignment[side]=dict(host_send_epoch_ns=send,host_receive_epoch_ns=send+100000000,host_send_monotonic_ns=send-900000000000,host_receive_monotonic_ns=send-900000000000+100000000,**guest,guest_evidence=self.ref(root,side+'.json',guest))
  return dict(schema_version=2,status='HEALTHY',database='lab',guest_boot_id=boot,collected_at=timestamp(999+shift),source_interval_seconds=10,required_measurements=['native_dp','threadstat'],query_evidence=query,clock_alignment=self.ref(root,'alignment.json',alignment),catalog_evidence=self.catalog,catalog_source_evidence={'interval':catalog['interval_evidence'],'observation':catalog['observation_evidence']})
 def publish(self,receipt):
  temporary=self.root/'atomic.tmp';temporary.write_bytes(receipt if isinstance(receipt,bytes) else json.dumps(receipt).encode());os.replace(temporary,self.path)
 def run_current(self):return c.read_native_current(self.path,'boot',self.events.append,wall=self.clock.wall,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
 def replace_after(self,value,seconds=.069):
  def update():
   if self.clock.now>=seconds:self.publish(value)
  self.clock.on_sleep=update
 def test_stale_old_then_atomic_new_generation_accepts_actual_time(self):
  original=self.path.read_bytes();self.replace_after(self.new)
  result=self.run_current()
  self.assertAlmostEqual(result['refresh_wait']['waited_seconds'],.1)
  self.assertEqual(result['refresh_wait']['status'],'RESOLVED')
  self.assertEqual([r['status'] for r in self.events],['PENDING','CANDIDATE_VALIDATED'])
  self.assertEqual(base64.b64decode(self.events[0]['receipt_bytes_base64']),original)
  self.assertEqual(result['receipt_sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
  self.assertTrue(all(r['max_seconds']<=30 for r in result['age_ranges'].values()))
 def test_no_update_exhausts_fixed_deadline_without_accepting_old_health(self):
  with self.assertRaisesRegex(c.Blocked,'deadline'):self.run_current()
  self.assertAlmostEqual(self.clock.now,1);self.assertEqual(self.events[-1]['status'],'FAILED')
  self.assertEqual(self.events[0]['receipt_sha256'],self.events[-1]['receipt_sha256'])
 def test_new_query_with_same_stale_source_does_not_cure_real_gap(self):
  stale=self.generation('source-gap',10,points=[980,985]);self.replace_after(stale)
  with self.assertRaises(c.Blocked):self.run_current()
  self.assertLess(self.clock.now,1);self.assertEqual(self.events[-1]['status'],'FAILED')
 def test_new_blocked_wrong_boot_bad_hash_catalog_and_malformed_fail_immediately(self):
  for fault in ('BLOCKED','boot','hash','catalog','malformed'):
   self.clock=Clock();self.events=[];self.publish(self.old);new=copy.deepcopy(self.new)
   if fault=='BLOCKED':new['status']='BLOCKED'
   elif fault=='boot':new['guest_boot_id']='other'
   elif fault=='hash':new['query_evidence']['sha256']='0'*64
   elif fault=='catalog':new['catalog_source_evidence']['interval']=self.ref(self.root,'changed-source.json',{'changed':True})
   else:new=b'{SYNTHETIC MALFORMED'
   self.replace_after(new)
   with self.subTest(fault=fault),self.assertRaises((c.Blocked,ValueError)):self.run_current()
   self.assertLess(self.clock.now,1);self.assertEqual(self.events[-1]['status'],'FAILED')
   if fault=='malformed':self.assertEqual(base64.b64decode(self.events[-1]['receipt_bytes_base64']),new)
 def test_missing_future_and_previously_stale_sources_never_wait(self):
  for points in ([1020,995],[950,985]):
   value=self.generation('bad-'+str(points[0]),0,points=points);self.publish(value);self.events=[]
   with self.assertRaises(c.Blocked):self.run_current()
   self.assertFalse(self.events);self.assertEqual(self.clock.now,0)
  value=copy.deepcopy(self.old);value['required_measurements'].append('absent');self.publish(value)
  with self.assertRaises(c.Blocked):self.run_current()
  self.assertFalse(self.events)
 def test_pure_validator_hash_is_same_single_snapshot_during_atomic_replace(self):
  self.publish(self.new);payload=self.path.read_bytes();original=c.artifact;changed=[]
  def artifact(*args):
   if not changed:self.publish(b'new invalid generation');changed.append(True)
   return original(*args)
  with patch.object(c,'artifact',side_effect=artifact):result=c.validate_native(self.path,'boot',self.clock.wall())
  self.assertEqual(result['receipt_sha256'],hashlib.sha256(payload).hexdigest())
  self.assertNotEqual(result['receipt_sha256'],hashlib.sha256(self.path.read_bytes()).hexdigest())
 def test_healthy_current_snapshot_has_no_wait_and_one_latest_read(self):
  self.publish(self.new)
  with patch.object(c,'native_snapshot',wraps=c.native_snapshot) as reader:
   self.run_current();self.assertEqual(reader.call_count,1)
  self.assertEqual(self.clock.now,0);self.assertFalse(self.events)
 def test_replacement_validation_past_deadline_rejected(self):
  self.replace_after(self.new);original=c.native_generation_identity
  def identity(snapshot,health):
   result=original(snapshot,health)
   if snapshot['receipt']['query_evidence']==self.new['query_evidence']:self.clock.now+=1
   return result
  with patch.object(c,'native_generation_identity',side_effect=identity),self.assertRaisesRegex(c.Blocked,'deadline'):self.run_current()
  self.assertEqual(self.events[-1]['status'],'FAILED')
 def test_evidence_fsync_delay_cannot_outlive_deadline_or_refresh_age(self):
  self.replace_after(self.new)
  def record(event):
   self.events.append(event)
   if event['status']=='CANDIDATE_VALIDATED':self.clock.now+=1
  with self.assertRaisesRegex(c.Blocked,'recording exceeded'):
   c.read_native_current(self.path,'boot',record,wall=self.clock.wall,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
  self.assertEqual([r['status'] for r in self.events],['PENDING','CANDIDATE_VALIDATED','FAILED'])
 def test_pending_record_error_retains_initial_exact_snapshot(self):
  payload=self.path.read_bytes()
  with self.assertRaises(OSError) as caught:
   c.read_native_current(self.path,'boot',lambda event:(_ for _ in ()).throw(OSError('synthetic disk full')),wall=self.clock.wall,monotonic=self.clock.monotonic,sleep=self.clock.sleep)
  self.assertEqual(caught.exception.native_snapshot['payload'],payload)
 def test_regressing_query_chronology_or_any_source_point_rejected(self):
  # A mixed-age old receipt: first source is stale, second is fresher. Only the
  # latter regresses in a replacement whose every source age is still <=30s.
  self.old=self.generation('old-mixed',0,points=[980,998]);self.publish(self.old)
  regressed=self.generation('regressed-source',10,points=[990,995]);self.replace_after(regressed)
  with self.assertRaisesRegex(c.Blocked,'timestamp regressed'):self.run_current()
  self.clock=Clock();self.events=[];self.publish(self.old)
  # Rewriting point bytes with the original query chronology is not a newer generation.
  same=self.generation('same-generation',0,points=[990,998]);self.replace_after(same)
  with self.assertRaisesRegex(c.Blocked,'generation did not advance'):self.run_current()
if __name__=='__main__':unittest.main(verbosity=2)
