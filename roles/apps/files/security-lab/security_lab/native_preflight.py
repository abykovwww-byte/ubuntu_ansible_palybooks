"""One stdlib binding checker, shared by the caller and the delivered preflight.

The program is host-selected implementation, never model-supplied code. It only
reads its sibling context/descriptor and emits a bounded receipt; no network.
"""
from __future__ import annotations

import hashlib
import shlex

MAX_PREFLIGHT_RECEIPT_CHARS = 1200
PREFLIGHT_GOAL_PREFIX = '\nHost-verified native preflight: '
PREFLIGHT_FILENAME = 'native-preflight.py'

BINDING_PROGRAM = r'''
import hashlib, json, math
def preflight_canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def native_services(scope):
    allowed = {str(t['scheme'])+'://'+str(t['host'])+
        ('' if t['port'] == (443 if t['scheme']=='https' else 80) else ':'+str(t['port']))+path.rstrip('/')
        for t in scope['targets'] for path in t['paths']}
    allowed.update('tcp://'+str(t['host'])+':'+str(port)
                   for t in scope.get('network_targets',[]) for port in t['tcp_ports'])
    return allowed
def validate_native_bindings(packet,run_id,*,allow_preview=False,now=None):
    if (type(packet.get('schema_version')) is not int or packet['schema_version'] != 2 or packet.get('stage') != 'checks'
            or packet.get('execution_mode') != 'existing_compose_runtime'):
        raise ValueError('native_envelope_mismatch')
    native = packet['native_execution']; binding = packet['binding']; scope = packet['scope_manifest']
    if native['admission'] != 'controller_reserved' and not (allow_preview and native['admission']=='preflight_only'):
        raise ValueError('native_admission_not_reserved')
    identity = {'case_id':packet['case_id'],'scope_id':binding['id'],'scope_version':binding['version'],
        'scope_hash':binding['hash'],'generation':packet['generation'],'run_id':run_id,
        'execution_targets':packet['execution_targets']}
    if any(native.get(key) != value for key,value in identity.items()):
        raise ValueError('native_binding_mismatch')
    if (hashlib.sha256(preflight_canonical(scope).encode('utf-8')).hexdigest() != binding['hash']
            or preflight_canonical(scope) != preflight_canonical(packet['effective_scope'])
            or scope['authorization'] != packet['rules_of_engagement']):
        raise ValueError('scope_payload_binding_mismatch')
    targets = native['execution_targets']
    if (not isinstance(targets,list) or not 1 <= len(targets) <= 16
            or any(type(t) is not str for t in targets) or sorted(set(targets)) != targets
            or not set(targets) <= native_services(scope)):
        raise ValueError('native_selection_outside_scope')
    if native['task_stage'] != 'checks' or packet['policy'].get('poc_allowed') is not False:
        raise ValueError('native_checks_stage_mismatch')
    if now is not None:
        times = [native['deadline'],native['snapshot_at'],scope['expires_at'],
                 scope['authorization']['valid_from'],scope['authorization']['valid_until'],now]
        if any(type(t) not in (int,float) or not math.isfinite(t) for t in times):
            raise ValueError('invalid_unix_deadline')
        if not scope['authorization']['valid_from'] <= now < min(native['deadline'],scope['expires_at'],scope['authorization']['valid_until']):
            raise ValueError('native_deadline_or_authorization_expired')
        if now < native['snapshot_at']:
            raise ValueError('native_snapshot_from_future')
    return native
def validate_native_descriptor(packet,descriptor,run_id):
    if (type(descriptor.get('schema_version')) is not int or descriptor['schema_version'] != 1
            or type(descriptor.get('packet_schema_version')) is not int or descriptor['packet_schema_version'] != 2
            or descriptor.get('worker_path') != '/workspace/handoffs/'+run_id+'/osint-context.json'):
        raise ValueError('native_descriptor_format_mismatch')
    native = validate_native_bindings(packet,run_id)
    for key in ('case_id','scope_id','scope_version','scope_hash','generation','run_id','execution_targets','job_id','deadline'):
        if descriptor.get(key) != native[key]:
            raise ValueError('native_descriptor_binding_mismatch')
    if descriptor.get('context_hash') != packet['context_hash']:
        raise ValueError('native_descriptor_snapshot_mismatch')
    return native
'''

_namespace: dict = {}
exec(compile(BINDING_PROGRAM, 'native-binding-checker', 'exec'), _namespace)
native_services = _namespace['native_services']
validate_native_bindings = _namespace['validate_native_bindings']
validate_native_descriptor = _namespace['validate_native_descriptor']

NATIVE_PREFLIGHT_PROGRAM = BINDING_PROGRAM + r'''
def native_preflight_main():
    import argparse, pathlib, sys, time
    parser = argparse.ArgumentParser()
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--expected-bytes',required=True,type=int)
    parser.add_argument('--run-id',required=True)
    args = parser.parse_args()
    try:
        directory = pathlib.Path(__file__).parent
        context_path = directory / 'osint-context.json'; descriptor_path = directory / 'descriptor.json'
        if context_path.is_symlink() or descriptor_path.is_symlink():
            raise ValueError('linked_preflight_input')
        if not 0 < args.expected_bytes <= 1048576 or context_path.stat().st_size != args.expected_bytes:
            raise ValueError('native_context_size_mismatch')
        raw = context_path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != args.expected_sha256:
            raise ValueError('native_context_digest_mismatch')
        if descriptor_path.stat().st_size > 16384:
            raise ValueError('native_descriptor_size_mismatch')
        packet = json.loads(raw.decode('utf-8')); descriptor = json.loads(descriptor_path.read_text('utf-8'))
        if descriptor['content_sha256'] != args.expected_sha256 or descriptor['size_bytes'] != args.expected_bytes:
            raise ValueError('native_descriptor_content_mismatch')
        native = validate_native_descriptor(packet,descriptor,args.run_id)
        now = time.time(); validate_native_bindings(packet,args.run_id,now=now)
        allowance = native['case_allowance']; data = packet.get('untrusted_osint_data',{})
        receipt = {'status':'ready_for_scoped_checks','preflight_revision':1,'binding_match':True,
            'admission':native['admission'],'run_id':args.run_id,'job_id':native['job_id'],
            'scope_version':native['scope_version'],'generation':native['generation'],
            'remaining_run_seconds':math.floor(native['deadline']-now),'reserved_seconds':native['reserved_seconds'],
            'unallocated_case_seconds':allowance['unallocated_seconds'],'unallocated_case_jobs':allowance['unallocated_jobs'],
            'execution_target_count':len(native['execution_targets']),
            'graph_count':len(data.get('surface_nodes',[])),'coverage_count':len(data.get('coverage',[])),
            'gap_count':len(data.get('gaps',[])),'target_requests':0}
        rendered = preflight_canonical(receipt)
        if len(rendered)>PREFLIGHT_RECEIPT_LIMIT: raise ValueError('native_preflight_receipt_too_large')
        print(rendered)
        return 0
    except (KeyError,TypeError,ValueError,OSError):
        print('{"status":"blocked","error_code":"native_preflight_validation_failed","target_requests":0}')
        return 2
if __name__ == '__main__':
    raise SystemExit(native_preflight_main())
'''.replace('PREFLIGHT_RECEIPT_LIMIT',str(MAX_PREFLIGHT_RECEIPT_CHARS))
NATIVE_PREFLIGHT_SHA256 = hashlib.sha256(NATIVE_PREFLIGHT_PROGRAM.encode('utf-8')).hexdigest()


def preflight_command(descriptor: dict) -> str:
    """A pinned command, rather than a request for the LLM to invent a validator."""
    path = descriptor['worker_path'].rsplit('/', 1)[0] + '/' + PREFLIGHT_FILENAME
    wrapper = ("import hashlib,pathlib,runpy,sys;p=pathlib.Path(sys.argv[1]);"
               "assert hashlib.sha256(p.read_bytes()).hexdigest()==sys.argv[2],'preflight_code_hash_mismatch';"
               "sys.argv=[str(p),'--expected-sha256',sys.argv[3],'--expected-bytes',sys.argv[4],'--run-id',sys.argv[5]];"
               "runpy.run_path(str(p),run_name='__main__')")
    return shlex.join(['python3', '-c', wrapper, path, descriptor['preflight_sha256'],
                       descriptor['content_sha256'], str(descriptor['size_bytes']), descriptor['run_id']])
