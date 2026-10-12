"""Independent bounded receiver observations; no guest or source discovery."""
import hashlib
import json
import math
from pathlib import Path

PROBE = """import json,time
from pathlib import Path
with Path('/tmp/ngfw-held-server.json').open('rb') as f: raw=f.read(2097153)
assert len(raw)<=2097152
print(json.dumps({'collected_at':time.time(),'receiver':json.loads(raw)}))
"""


def require(condition, reason):
    if not condition: raise ValueError(reason)


def number(value):
    return type(value) in (int,float) and math.isfinite(value)


MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


def read_bounded(path):
    with Path(path).open('rb') as stream:
        raw = stream.read(MAX_EVIDENCE_BYTES + 1)
    require(len(raw) <= MAX_EVIDENCE_BYTES, 'evidence exceeds 16 MiB bound')
    return raw


def reference(path):
    path=Path(path)
    return {'path':str(path.resolve()),'sha256':hashlib.sha256(read_bounded(path)).hexdigest()}


def referenced(ref, base):
    require(isinstance(ref,dict) and isinstance(ref.get('path'),str),'held evidence reference absent')
    path=Path(ref['path']);path=path if path.is_absolute() else Path(base)/path
    raw=read_bounded(path)
    require(hashlib.sha256(raw).hexdigest()==ref.get('sha256'),'held evidence hash differs')
    return raw


def validate(rows, final, offered, run_id, max_gap):
    """Combine independent receiver history with already checked client work.

Receiver counters record every disconnect and each connection's minimum echo
round. Periodic snapshots are bounded-cadence evidence, not packet capture.
"""
    require(number(max_gap) and 0<max_gap<=60,'held cadence bound invalid')
    start,end=final.get('measure_start'),final.get('time_end')
    require(number(start) and number(end) and end>start,'held actual window absent')
    expected=offered['sessions'];heartbeat=offered.get('heartbeat_seconds',5)
    require(final.get('requested_sessions')==expected,'held requested count differs')
    for key in ('minimum_active_measurement_sessions','active_sessions_at_measure_end','confirmed_measurement_sessions'):
        require(final.get(key)==expected,'held client population differs')
    require(final.get('stale_echo_sessions')==0 and not final.get('stop_reason'),'held client stale or stopped')
    require(final.get('opened_sessions')==expected and all(final.get(k)==0 for k in
            ('errors','pending_requests','censored_at_boundary')),'held client exchange incomplete')
    key=str(int.from_bytes(hashlib.sha256(run_id.encode()).digest()[:8],'big'))
    require(len(rows)>=3,'held receiver samples absent')
    instances=set();observed=[];previous=None;previous_stamp=None
    for row in rows:
        require(not row.get('error'),'held receiver read failed')
        r=row['receiver'];collected=row['collected_at'];stamp=r['wall_time']
        require(number(stamp) and number(collected) and 0<=collected-stamp<=3,'held receiver snapshot stale')
        require(r.get('kind')=='held-session-receiver' and r.get('status')=='running','held receiver stopped')
        instance=r.get('receiver_instance')
        require(isinstance(instance,str) and len(instance)==32,'held receiver instance absent')
        instances.add(instance)
        if previous is not None:
            require(collected>previous and collected-previous<=max_gap,'held receiver sample gap')
            require(stamp>=previous_stamp,'held receiver publication time regressed')
        previous=collected
        previous_stamp=stamp
        run=r.get('runs',{}).get(key)
        if stamp>=start:require(run is not None,'held target run missing during receiver coverage')
        if run is not None: observed.append((stamp,run))
    require(len(instances)==1,'held receiver restarted')
    require(rows[0]['collected_at']<=start and rows[-1]['receiver']['wall_time']>=end,
            'held receiver samples do not bracket measurement')
    relevant=[(t,r) for t,r in observed if t>=start]
    inside=[(t,r) for t,r in relevant if t<=end]
    require(len(inside)>=2 and inside[0][0]-start<=max_gap and end-inside[-1][0]<=max_gap,
            'held receiver measurement coverage absent')
    for t,r in relevant:
        require(all(type(r.get(k)) is int and r[k]>=0 for k in
                    ('connections','peak','active','messages','minimum_echoes')),'held receiver counters invalid')
        require(r.get('connections')==expected and r.get('peak')==expected,'held receiver population differs')
        require(number(r.get('peak_reached_at')) and r['peak_reached_at']<=start,'held buildup overlaps measurement')
        closed=r.get('first_disconnect_at')
        require(closed is None or number(closed) and closed>=end,'held receiver disconnected during measurement')
        if t<=end:require(r.get('active')==expected,'held receiver active population differs')
        require(type(r.get('minimum_echoes')) is int and r['minimum_echoes']>0,'held receiver echoes absent')
    for (a,ra),(b,rb) in zip(inside,inside[1:]):
        require(rb['messages']>=ra['messages'] and rb['minimum_echoes']>=ra['minimum_echoes'],
                'held receiver echo counters regressed')
    # Every connection must advance, even when sampling is faster than heartbeat.
    for i,(a,ra) in enumerate(inside):
        later=next(((b,rb) for b,rb in inside[i+1:] if b-a>=2*heartbeat),None)
        if later:
            require(later[1]['minimum_echoes']>ra['minimum_echoes'],'held receiver echo progression stalled')
    return {'status':'VALID','scope':'independent_receiver_population_and_sampled_echo_progression',
            'run_id':run_id,'offered':offered,'measure_start':start,'measure_end':end,
            'receiver_instance':next(iter(instances)),'samples':len(rows),'inside_samples':len(inside),
            'max_sample_gap_seconds':max_gap}


def verify(ref, base, raw_workload, offered, run_id):
    proof=json.loads(referenced(ref,base))
    receiver_raw=referenced(proof['receiver_evidence'],base)
    workload=referenced(proof['workload_evidence'],base)
    require(workload==raw_workload,'held proof workload differs')
    finals=[r for r in map(json.loads,workload.splitlines()) if r.get('record_type')=='final']
    require(len(finals)==1 and finals[0].get('run_id')==run_id,'held proof final identity differs')
    rows=[json.loads(line) for line in receiver_raw.splitlines()]
    actual=validate(rows,finals[0],offered,run_id,proof['max_sample_gap_seconds'])
    require(all(proof.get(k)==v for k,v in actual.items()),'held proof content differs')
    return actual
