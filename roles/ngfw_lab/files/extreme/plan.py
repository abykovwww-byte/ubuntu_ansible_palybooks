"""Materialize extreme A/B windows and commands; never mutate profiles or execute loads."""
import argparse
import hashlib
import ipaddress
import json
from pathlib import Path


HASH_FIELDS = ('build', 'policy', 'security_features', 'vm_resources', 'topology',
               'generator', 'receiver', 'observation', 'log_forwarding')
OUTCOMES = ('NGFW_LIMIT', 'GENERATOR_LIMITED', 'HOST_LIMITED',
            'INVALID_MEASUREMENT', 'SAFETY_STOP', 'STABLE_LIMIT_NOT_ESTABLISHED')


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                    allow_nan=False).encode()).hexdigest()


def validate_load(load):
    kind = load['kind']
    if kind not in ('tcp', 'udp', 'short-tcp', 'http', 'dns', 'held', 'mixed'):
        raise ValueError('unsupported workload kind')
    if kind in ('tcp', 'udp'):
        if not 0 < load['bps'] <= 100_000_000_000 or not 1 <= load.get('parallel', 1) <= 128:
            raise ValueError('invalid bitrate or stream count')
        if kind == 'udp' and not 16 <= load['payload_bytes'] <= 1400:
            raise ValueError('UDP payload must be 16..1400 bytes; not Ethernet frame size')
    elif kind in ('short-tcp', 'http', 'dns'):
        if not 0 < load['rate'] <= 1_000_000 or not 1 <= load['concurrency'] <= 20000:
            raise ValueError('invalid offered rate/concurrency')
    elif kind == 'held':
        if not 1 <= load['sessions'] <= 1_000_000 or not 1 <= load['open_rate'] <= 100000:
            raise ValueError('invalid session/opening limits')
        if not 1 <= load.get('heartbeat_seconds', 5) <= 10:
            raise ValueError('heartbeat must remain below receiver idle timeout')
    else:
        parts = load['components']
        if len(parts) < 2 or any(p['kind'] in ('mixed', 'held') for p in parts):
            raise ValueError('mixed needs independent non-held components')
        if sum(p['kind'] in ('tcp', 'udp') for p in parts) > 1:
            raise ValueError('one iperf3 server cannot run concurrent TCP and UDP jobs')
        if not {'tcp', 'udp'}.intersection(p['kind'] for p in parts) or not any(
                p['kind'] in ('short-tcp', 'http') for p in parts) or not any(
                p['kind'] == 'dns' for p in parts):
            raise ValueError('mixed needs bandwidth, new connections and DNS PPS components')
        for part in parts:
            validate_load(part)
    json.dumps(load, allow_nan=False)
    return load


def component_seconds(load, seconds, phase='measure'):
    if type(seconds) is not int or not 1 <= seconds <= 3600 or phase not in ('measure', 'stabilization'):
        raise ValueError('bounded nominal duration 1..3600 and known phase required')
    return seconds + (2 if load['kind'] == 'mixed' and phase == 'measure' else 0)


def commands(load, target, seconds, run_id, source_ips=(), tool_path='/opt/extreme/load.py', phase='measure'):
    """Return independent argv arrays; root controller owns concurrent mixed start/stop."""
    validate_load(load)
    address = ipaddress.ip_address(target)
    if address.version != 4 or not address.is_private:
        raise ValueError('explicit private IPv4 lab target required')
    actual_seconds = component_seconds(load, seconds, phase)
    if load['kind'] == 'mixed':
        return [_command(component, target, actual_seconds, run_id, source_ips, tool_path)
                for component in load['components']]
    return [_command(load, target, actual_seconds, run_id, source_ips, tool_path)]


def _command(load, target, seconds, run_id, source_ips, tool_path):
    if load['kind'] in ('tcp', 'udp'):
        parallel = load.get('parallel', 1)
        argv = ['iperf3', '-c', target, '-t', str(seconds), '-J', '-P', str(parallel),
                '-b', str(int(load['bps'] / parallel)), '--connect-timeout', '2000',
                '--get-server-output']
        if load['kind'] == 'udp':
            argv += ['-u', '-l', str(load['payload_bytes'])]
        return argv
    argv = ['python3', tool_path, 'client', '--kind', load['kind'], '--target', target,
            '--duration', str(seconds), '--run-id', run_id]
    if load['kind'] == 'held':
        argv += ['--sessions', str(load['sessions']), '--rate', str(load['open_rate']),
                 '--heartbeat', str(load.get('heartbeat_seconds', 5))]
        if not source_ips:
            raise ValueError('held sessions require explicitly provisioned source IPs')
    else:
        argv += ['--rate', str(load['rate']), '--concurrency', str(load['concurrency'])]
    for source in source_ips:
        if not ipaddress.ip_address(source).is_private:
            raise ValueError('source address must be private')
        argv += ['--source-ip', source]
    return argv


def readiness(config):
    """Receipts are evidence references, never an inferred claim of live readiness."""
    errors = []
    for name in HASH_FIELDS:
        digest = config.get('invariants', {}).get(name)
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            errors.append('missing sha256 invariant: ' + name)
    for name in ('isolation', 'snapshot_A', 'snapshot_B', 'rollback', 'profile_A', 'profile_B',
                 'native_collection', 'observer_impact', 'resource_reservations'):
        if not config.get('receipts', {}).get(name):
            errors.append('missing receipt: ' + name)
    profiles = config.get('profiles', {})
    if profiles.get('A', {}).get('edr_installed') is not False or profiles.get('A', {}).get('audit_profile') != 'stock':
        errors.append('A must have EDR absent and administrative audit')
    if (profiles.get('B', {}).get('edr_installed') is not True or profiles.get('B', {}).get('audit_profile') != 'full'
            or profiles.get('B', {}).get('audit_rule_count') != 116):
        errors.append('B must have EDR installed and full 116-rule audit read-back')
    for workload in config['workloads']:
        if workload.get('offered') is None:
            errors.append('offered load not selected from direct capacity: ' + workload['id'])
            continue
        validate_load(workload['offered'])
        capacity = workload.get('control_capacity', {})
        if (not capacity.get('receipt') or capacity.get('load_hash') != fingerprint(workload['offered'])
                or capacity.get('topology_hash') != config.get('invariants', {}).get('topology')
                or capacity.get('status') != 'VALID' or capacity.get('offered_achieved') is not True
                or capacity.get('resources_unlimited') is not True):
            errors.append('missing valid direct-path capacity at identical load: ' + workload['id'])
    return errors


def materialize(config):
    if not config.get('campaign_id') or not config.get('workloads'):
        raise ValueError('campaign_id and workloads required')
    if len({w['id'] for w in config['workloads']}) != len(config['workloads']):
        raise ValueError('duplicate workload ids')
    windows = []
    warmup = config.get('stabilization_seconds', 30)
    recovery = config.get('recovery_seconds', 120)
    if not 0 <= warmup <= 60 or recovery < 120:
        raise ValueError('stabilization <=60 and recovery >=120 required')
    for workload in config['workloads']:
        if workload.get('offered') is None:
            continue
        for block, order in enumerate(('ABA', 'BAB', 'ABA'), 1):
            for position, profile in enumerate(order, 1):
                ident = f"{config['campaign_id']}-{workload['id']}-b{block}-{position}-{profile}"
                windows.append(dict(run_id=ident, profile=profile, workload=workload['id'], repeat=block,
                                    pair_id=f"{config['campaign_id']}-{workload['id']}-b{block}",
                                    block_id=f"{config['campaign_id']}-{workload['id']}-b{block}", position=position - 1,
                                    role='return_control' if position == 3 else 'paired',
                                    offered=workload['offered'], offered_hash=fingerprint(workload['offered']),
                                    phase='comparison', buildup_separate=workload['offered']['kind'] == 'held',
                                    stabilization_seconds=warmup, measure_seconds=300,
                                    recovery_seconds=recovery, wait_until_recovered=True,
                                    argv=commands(workload['offered'], config['target'], 300, ident,
                                                  config.get('source_ips', []))))
    # Endurance must use each profile's independently evidenced stable limit.
    endurance = []
    for profile in ('A', 'B'):
        chosen = config.get('endurance_selection', {}).get(profile)
        if chosen:
            if not chosen.get('stable_receipt'):
                raise ValueError('endurance requires stable-limit receipt for ' + profile)
            ident = f"{config['campaign_id']}-endurance-{profile}"
            endurance.append(dict(run_id=ident, profile=profile, phase='endurance',
                                  offered=chosen['offered'], offered_hash=fingerprint(chosen['offered']),
                                  stable_receipt=chosen['stable_receipt'], measure_seconds=3600,
                                  stabilization_seconds=warmup, recovery_seconds=recovery,
                                  wait_until_recovered=True, argv=commands(chosen['offered'], config['target'],
                                  3600, ident, config.get('source_ips', []))))
    for window in windows + endurance:
        if window['offered']['kind'] == 'mixed':
            window['component_measure_seconds'] = component_seconds(window['offered'], window['measure_seconds'])
    return dict(schema_version=1, campaign_id=config['campaign_id'], config_hash=fingerprint(config),
                invariants=config.get('invariants', {}), blockers=readiness(config), windows=windows,
                endurance=endurance, endurance_pending=[p for p in ('A', 'B') if not config.get('endurance_selection', {}).get(p)],
                unselected_workloads=[w for w in config['workloads'] if w.get('offered') is None],
                outcomes=list(OUTCOMES), cpu_100_is_stop=False,
                classification_note='A/B measures full EDR plus AuditD profile, not binary-only overhead')


def classify(evidence):
    """Attribution needs affirmative evidence; high OS CPU alone cannot select a limit."""
    if evidence.get('safety_stop'):
        return 'SAFETY_STOP'
    if evidence.get('complete') is not True or evidence.get('identical_invariants') is not True:
        return 'INVALID_MEASUREMENT'
    if evidence.get('host_limited'):
        return 'HOST_LIMITED'
    if evidence.get('generator_limited') is True:
        return 'GENERATOR_LIMITED'
    if evidence.get('degraded'):
        if evidence.get('direct_control_valid') is True and evidence.get('ngfw_limit_evidence'):
            return 'NGFW_LIMIT'
        return 'INVALID_MEASUREMENT'
    if evidence.get('offered_achieved') is not True:
        return 'INVALID_MEASUREMENT'
    return 'STABLE_LIMIT_NOT_ESTABLISHED'


def next_load(low_stable, high_failed, prepared_max):
    """Coarse doubling or bisection after the initial upper target, never a slow ladder."""
    if not 0 <= low_stable <= prepared_max or high_failed is not None and high_failed <= low_stable:
        raise ValueError('invalid observed range')
    if high_failed is None:
        return min(prepared_max, max(1, low_stable * 2)) if low_stable < prepared_max else None
    return (low_stable + high_failed) / 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--require-ready', action='store_true')
    args = parser.parse_args()
    result = materialize(json.loads(args.config.read_text(encoding='utf-8')))
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + '\n', encoding='utf-8')
    if args.require_ready and result['blockers']:
        raise SystemExit(2)


if __name__ == '__main__':
    main()
