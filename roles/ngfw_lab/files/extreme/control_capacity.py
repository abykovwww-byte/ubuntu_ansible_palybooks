"""Opt-in endpoint installation and measured direct-path capacity.

Linux host CLI, no SSH, guest mutation, profile switching or native-input tests.
capture-dut -> Ansible direct switch -> run -> Ansible ngfw switch -> finalize.
A successful run is only a candidate until the actual DUT topology is restored.
"""
import argparse
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import runner
try:
    from . import controller, plan
except ImportError:
    import controller
    import plan
from native_analysis.normalize import extreme_observation, iperf_observation

Blocked = controller.Blocked
SERVICES = ('traffic-client', 'traffic-server')
CONFIG_KEYS = ('NanoCpus', 'Memory', 'MemorySwap', 'PidsLimit', 'Ulimits', 'Sysctls',
               'ReadonlyRootfs', 'CapDrop', 'CapAdd', 'SecurityOpt', 'Privileged', 'Tmpfs')

# Executed only inside the already selected endpoint; no guest process scan.
SNAPSHOT = r'''
import json, os, resource, time
from pathlib import Path
def read(path): return Path(path).read_text().strip()
def pairs(path): return {k:int(v) for k,v in (s.split() for s in read(path).splitlines())}
cg=Path('/sys/fs/cgroup')
processes=[]
for p in Path('/proc').iterdir():
    if p.name.isdigit() and p.stat().st_uid == os.getuid():
        try:
            stat=p.joinpath('stat').read_text().rsplit(')',1)[1].split()
            fds=len(list(p.joinpath('fd').iterdir()))
            limit=next(s.split()[3:5] for s in p.joinpath('limits').read_text().splitlines() if s.startswith('Max open files'))
            processes.append({'pid':int(p.name),'start_ticks':int(stat[19]),'fds':fds,'fd_soft':int(limit[0]),'fd_hard':int(limit[1])})
        except FileNotFoundError: pass
ports=[int(v) for v in read('/proc/sys/net/ipv4/ip_local_port_range').split()]
reserved=read('/proc/sys/net/ipv4/ip_local_reserved_ports')
sockets=[]
for family in ('tcp','tcp6','udp','udp6'):
    for row in read('/proc/net/'+family).splitlines()[1:]:
        cols=row.split()
        sockets.append({'protocol':family,'local':cols[1],'remote':cols[2],'state':cols[3]})
held=Path('/tmp/ngfw-held-server.json')
print(json.dumps({'time':time.time(),'processes':processes,'ports':ports,'reserved_ports':reserved,
 'sockets':sockets,'cpu':pairs(cg/'cpu.stat'),'cpu_max':read(cg/'cpu.max'),
 'memory_current':int(read(cg/'memory.current')),'memory_max':int(read(cg/'memory.max')),
 'memory_events':pairs(cg/'memory.events'),'pids_current':int(read(cg/'pids.current')),
 'pids_max':int(read(cg/'pids.max')),'pids_events':pairs(cg/'pids.events'),
 'held':json.loads(held.read_text()) if held.exists() else None}))
'''


def save(path, value):
    path = Path(path)
    with path.with_suffix(path.suffix + '.tmp').open('w', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    path.with_suffix(path.suffix + '.tmp').replace(path)


def reference(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}


@contextmanager
def locked(config):
    import fcntl
    directory = Path(config['reports_dir'])
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Blocked('another runner or endpoint switch owns the lab lock') from None
        yield


def endpoint_config(entry, project):
    cfg, host = entry['Config'], entry['HostConfig']
    if (not host.get('ReadonlyRootfs') or host.get('Privileged') or host.get('CapAdd')
            or 'ALL' not in host.get('CapDrop', []) or host.get('PortBindings')):
        raise Blocked('endpoint isolation/resources do not match prepared mode')
    mounts = entry.get('Mounts', [])
    mounted = [m for m in mounts if m.get('Destination') == '/opt/extreme']
    if len(mounted) != 1 or mounted[0].get('RW') or Path(mounted[0]['Source']).resolve() != (Path(project) / 'extreme').resolve():
        raise Blocked('extreme source mount missing, writable or unexpected')
    files = {str(path.relative_to(Path(project) / 'extreme')): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted((Path(project) / 'extreme').rglob('*.py'))}
    if not files:
        raise Blocked('mounted workload sources unavailable')
    return {'image_id': entry['Image'], 'entrypoint': cfg['Entrypoint'], 'command': cfg['Cmd'],
            'user': cfg['User'], 'environment_sha256': plan.fingerprint(cfg.get('Env', [])),
            'resources': {key: host.get(key) for key in CONFIG_KEYS}, 'extreme_sources': files}


def inspect_endpoints(backend, mode):
    entries, configs, routes = {}, {}, {}
    for service in SERVICES:
        cid = backend.call(backend.compose + ['ps', '-q', service]).strip()
        if not cid:
            raise Blocked('endpoint absent: ' + service)
        entry = json.loads(backend.call(['docker', 'inspect', cid]))[0]
        if not entry['State']['Running'] or entry['State'].get('Restarting'):
            raise Blocked('endpoint not continuously running')
        if len(entry['NetworkSettings']['Networks']) != 1:
            raise Blocked('exactly one endpoint network required')
        entries[service] = entry
        configs[service] = endpoint_config(entry, backend.config['project_dir'])
        routes[service] = json.loads(backend.endpoint(service, ['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
    network_ids = {next(iter(e['NetworkSettings']['Networks'].values()))['NetworkID'] for e in entries.values()}
    networks = json.loads(backend.call(['docker', 'network', 'inspect'] + sorted(network_ids)))
    if mode == 'direct':
        if len(networks) != 1:
            raise Blocked('direct endpoints do not share exactly one network')
        net = networks[0]
        if (net['Driver'] != 'bridge' or net.get('Internal') is not True or net.get('EnableIPv6')
                or net.get('Labels', {}).get('ngfw.lab.scope') != 'direct-control-only'
                or set(net.get('Containers', {})) != {e['Id'] for e in entries.values()}):
            raise Blocked('direct network not exclusive internal IPv4 bridge')
        for service, rows in routes.items():
            if any(row.get('dst') == 'default' or row.get('gateway') for row in rows):
                raise Blocked('direct endpoint has an external/default route: ' + service)
        # Docker allocates the subnet. Refuse overlap with any other Docker network.
        allocated = [ipaddress.ip_network(row['Subnet']) for row in net['IPAM']['Config'] if 'Subnet' in row]
        others = backend.call(['docker', 'network', 'ls', '-q']).split()
        for other in json.loads(backend.call(['docker', 'network', 'inspect'] + others)):
            if other['Id'] == net['Id']:
                continue
            for row in other.get('IPAM', {}).get('Config') or []:
                if row.get('Subnet') and any(ipaddress.ip_network(row['Subnet']).version == n.version and
                        ipaddress.ip_network(row['Subnet']).overlaps(n) for n in allocated):
                    raise Blocked('allocated control subnet overlaps another Docker network')
        bridge = net.get('Options', {}).get('com.docker.network.bridge.name', 'br-' + net['Id'][:12])
        host_routes = json.loads(backend.call(['ip', '-j', '-4', 'route', 'show', 'table', 'all']))
        for row in host_routes:
            if row.get('dst') not in (None, 'default') and row.get('dev') != bridge:
                destination = ipaddress.ip_network(row['dst'], strict=False)
                if any(destination.version == n.version and destination.overlaps(n) for n in allocated):
                    raise Blocked('control subnet overlaps another host route')
    elif len(networks) != 2 or any(n['Driver'] != 'ipvlan' or n.get('Options', {}).get('ipvlan_mode') != 'l2' for n in networks):
        raise Blocked('DUT endpoints must use separate ipvlan l2 networks')
    server = entries['traffic-server']
    if configs['traffic-server']['entrypoint'] != ['python3', '/opt/extreme/load.py'] or '--with-iperf' not in server['Config']['Cmd']:
        raise Blocked('extreme receiver with supervised iperf required')
    addresses = {s: next(iter(e['NetworkSettings']['Networks'].values()))['IPAddress'] for s, e in entries.items()}
    return {'mode': mode, 'observed_at': controller.utc(), 'entries': entries, 'configs': configs,
            'networks': networks, 'routes': routes, 'addresses': addresses}


def capacity_limits(value):
    required = {'success_ratio', 'max_error_ratio', 'max_udp_loss_pct', 'min_fd_reserve',
                'min_tuple_reserve', 'max_memory_ratio', 'max_pids_ratio', 'max_throttled_ratio',
                'host_memory_stop_pct', 'host_disk_stop_pct', 'sample_seconds', 'temperature_stop_c'}
    if set(value) != required or any(type(v) not in (int, float) or not math.isfinite(v) for v in value.values()):
        raise Blocked('explicit finite capacity thresholds required')
    if (not .95 <= value['success_ratio'] <= 1 or not 0 <= value['max_error_ratio'] <= .01
            or not 0 <= value['max_udp_loss_pct'] <= 1 or value['min_fd_reserve'] < 1024
            or value['min_tuple_reserve'] < 1024 or not .1 <= value['max_memory_ratio'] <= .9
            or not .1 <= value['max_pids_ratio'] <= .9 or not 0 <= value['max_throttled_ratio'] <= .05
            or not 50 <= value['host_memory_stop_pct'] <= 95 or not 50 <= value['host_disk_stop_pct'] <= 90
            or not 5 <= value['sample_seconds'] <= 15 or not 50 <= value['temperature_stop_c'] <= 100):
        raise Blocked('capacity thresholds outside prepared bounds')
    return value


def tuple_capacity(snapshot):
    low, high = snapshot['ports']
    reserved = set()
    for item in snapshot['reserved_ports'].split(','):
        if item:
            ends = [int(v) for v in item.split('-')]
            reserved.update(range(ends[0], ends[-1] + 1))
    total = high - low + 1 - sum(low <= port <= high for port in reserved)
    used = {int(row['local'].rsplit(':', 1)[1], 16) for row in snapshot['sockets']
            if low <= int(row['local'].rsplit(':', 1)[1], 16) <= high}
    return total, total - len(used)


def resources_proven(samples, limits):
    """No observed bottleneck within declared thresholds; never physical unlimited."""
    issues = []
    if len(samples) < 2 or any(sample.get('error') for sample in samples):
        return ['resource_samples_missing']
    if any(b['time'] - a['time'] > limits['sample_seconds'] * 2 + 2 for a, b in zip(samples, samples[1:])):
        issues.append('resource_sampling_gap')
    for service in SERVICES:
        previous = None
        for sample in samples:
            row = sample['endpoints'][service]
            if not row['processes'] or min(p['fd_soft'] - p['fds'] for p in row['processes']) < limits['min_fd_reserve']:
                issues.append(service + ':fd_headroom')
            if tuple_capacity(row)[1] < limits['min_tuple_reserve']:
                issues.append(service + ':tuple_headroom')
            if row['memory_current'] / row['memory_max'] >= limits['max_memory_ratio']:
                issues.append(service + ':memory_headroom')
            if row['pids_current'] / row['pids_max'] >= limits['max_pids_ratio']:
                issues.append(service + ':pids_headroom')
            if previous:
                for group, counters in (('memory_events', ('high', 'max', 'oom', 'oom_kill')), ('pids_events', ('max',))):
                    if any(row[group][key] != previous[group][key] for key in counters):
                        issues.append(service + ':' + group)
                periods = row['cpu']['nr_periods'] - previous['cpu']['nr_periods']
                throttled = row['cpu']['nr_throttled'] - previous['cpu']['nr_throttled']
                if periods < 0 or throttled < 0 or (periods and throttled / periods > limits['max_throttled_ratio']):
                    issues.append(service + ':cgroup_throttling')
            previous = row
    return sorted(set(issues))


class Monitor:
    def __init__(self, backend, directory, report, limits):
        self.backend, self.directory, self.report, self.limits = backend, directory, report, limits
        self.config = {'sample_interval': limits['sample_seconds']}
        self.context, self.samples, self.cancelled = {}, [], False

    def monitor(self):
        if self.cancelled:
            raise Blocked('operator interrupted direct load')
        sample = {'time': time.time(), 'monotonic': time.monotonic(), 'phase': self.context.get('phase'), 'endpoints': {}}
        try:
            sample['host'] = self.backend.host()
            sample['temperatures_c'] = [int(p.read_text()) / 1000 for p in Path('/sys/class/hwmon').glob('hwmon*/temp*_input')]
            if not sample['temperatures_c']:
                raise Blocked('host temperature observations unavailable')
            for service in SERVICES:
                sample['endpoints'][service] = json.loads(self.backend.endpoint(service, ['python3', '-c', SNAPSHOT], timeout=10))
            sample['management_ok'] = self.backend.tcp_ok(self.backend.config['management_address'], self.backend.config['management_port'])
            sample['mngt_ok'] = self.backend.tcp_ok(self.backend.config['mngt_address'], self.backend.config['mngt_port'])
            sample['direct_service_ok'] = self.backend.probe_endpoint('traffic-client', self.backend.config['target'])
            if not all(sample[key] is True for key in ('management_ok', 'mngt_ok', 'direct_service_ok')):
                raise Blocked('management or direct receiver health failed')
            if (sample['host']['memory_used_pct'] >= self.limits['host_memory_stop_pct']
                    or sample['host']['disk_used_pct'] >= self.limits['host_disk_stop_pct']
                    or max(sample['temperatures_c']) >= self.limits['temperature_stop_c']):
                raise Blocked('host safety threshold crossed')
            for service, row in sample['endpoints'].items():
                if row['memory_current'] / row['memory_max'] >= .95 or row['pids_current'] / row['pids_max'] >= .95:
                    raise Blocked('endpoint safety headroom exhausted: ' + service)
        except Exception as error:
            sample['error'] = type(error).__name__
            raise
        finally:
            self.samples.append(sample)
            with (self.directory / 'resource-samples.ndjson').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(sample) + '\n')
                stream.flush()
                os.fsync(stream.fileno())

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.monitor()
            time.sleep(min(self.limits['sample_seconds'], max(0, end - time.monotonic())))


def measure_results(report, directory, offered, duration, limits, samples):
    parts = offered.get('components', [offered])
    issues, measured = [], []
    components = [c for c in report['components'] if c['phase'] == 'measure']
    if len(components) != len(parts):
        return [], ['missing_workload_components']
    for part, component in zip(parts, components):
        raw = (directory / component['path']).read_bytes()
        kind = part['kind']
        if kind in ('tcp', 'udp'):
            value, observation, errors = iperf_observation(raw, kind, {})
            receiver = observation.get('receiver_summary', {})
            if receiver.get('seconds', 0) < duration - .1:
                errors.append('measurement_shorter_than_requested')
            if value.get('achieved', {}).get('value', 0) < part['bps'] * limits['success_ratio']:
                errors.append('receiver_offered_rate_not_achieved')
            if kind == 'udp' and (type(receiver.get('lost_percent')) not in (int, float)
                                 or receiver['lost_percent'] > limits['max_udp_loss_pct']):
                errors.append('receiver_loss_exceeds_threshold_or_missing')
        else:
            value, observation, errors = extreme_observation(raw, report['run_id'], kind)
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
            final = next((row for row in reversed(rows) if row.get('record_type') == 'final'), {})
            if final.get('seconds', 0) < duration - .1:
                errors.append('measurement_shorter_than_requested')
            if final.get('errors', 1) / max(1, final.get('attempted_requests', 0)) > limits['max_error_ratio']:
                errors.append('application_error_ratio')
            if kind == 'held':
                key = str(int.from_bytes(hashlib.sha256(report['run_id'].encode()).digest()[:8], 'big'))
                start, end = final.get('measure_start'), final.get('time_end')
                held = [s['endpoints']['traffic-server']['held'] for s in samples
                        if start is not None and start <= s['time'] <= end]
                if (len(held) < max(2, int(duration / limits['sample_seconds']) - 2)
                        or any(h is None or h['status'] != 'running' or h['runs'].get(key, {}).get('active') != part['sessions'] for h in held)
                        or any(b['runs'][key]['messages'] <= a['runs'][key]['messages'] for a, b in zip(held, held[1:]))):
                    errors.append('receiver_sustained_held_population_unproven')
            elif value.get('achieved', {}).get('value', 0) < part['rate'] * limits['success_ratio']:
                errors.append('successful_offered_rate_not_achieved')
        if component['exit_code'] != 0:
            errors.append('workload_nonzero_exit')
        measured.append({'kind': kind, 'actual': value, 'observation': observation, 'errors': errors})
        issues.extend(kind + ':' + item for item in errors)
    return measured, issues


def capture_dut(backend, directory, isolation):
    topology = backend.topology()
    evidence = controller.read_json(isolation)
    runner.validate_isolation(evidence, topology, time.time(), 86400, backend.config.get('campaign_id'))
    if (evidence.get('ngfw_state') != 'shut off' or evidence.get('client_to_receiver') != 'unreachable'
            or evidence.get('receiver_local_health') is not True):
        raise Blocked('powered-off negative dataplane isolation proof required')
    state = inspect_endpoints(backend, 'ngfw')
    if state['addresses']['traffic-server'] != backend.config['target']:
        raise Blocked('DUT target differs from live receiver')
    save(directory / 'dut-topology.json', topology)
    references = {}
    for service, key in zip(SERVICES, ('generator', 'receiver')):
        save(directory / (key + '.json'), state['configs'][service])
        references[key] = reference(directory / (key + '.json'))
    anchor = {'observed_at': controller.utc(), 'target': backend.config['target'], 'topology_hash': plan.fingerprint(topology),
              'topology': reference(directory / 'dut-topology.json'), 'endpoint_artifacts': references,
              'configs': state['configs'], 'isolation': reference(isolation), 'state': state}
    save(directory / 'anchor.json', anchor)
    return anchor


def validate_anchor(anchor, campaign):
    if not 0 <= time.time() - controller.epoch(anchor['observed_at']) <= 86400:
        raise Blocked('DUT anchor older than one day')
    if anchor['target'] != campaign['target'] or anchor['topology_hash'] != campaign['invariants']['topology']:
        raise Blocked('campaign differs from observed DUT anchor')
    controller.artifact(anchor['topology'], Path('.'))
    for key in ('generator', 'receiver'):
        payload = controller.artifact(anchor['endpoint_artifacts'][key], Path('.'))
        if hashlib.sha256(payload).hexdigest() != campaign['invariants'][key]:
            raise Blocked('campaign endpoint hash not bound to observed configuration')


def run_capacity(backend, campaign, anchor_path, workload, directory, seconds, limits):
    if seconds not in (300, 3600):
        raise Blocked('capacity must cover a complete 300s or 3600s window')
    anchor = controller.read_json(anchor_path)
    validate_anchor(anchor, campaign)
    offered = next(w['offered'] for w in campaign['workloads'] if w['id'] == workload)
    plan.validate_load(offered)
    report = {'run_id': 'control-' + uuid.uuid4().hex, 'status': 'INVALID_MEASUREMENT', 'components': [],
              'gaps': [], 'offered': offered, 'duration_seconds': seconds, 'thresholds': limits,
              'campaign': campaign, 'anchor': reference(anchor_path), 'started_at': controller.utc(),
              'scope': 'direct_generator_receiver_capacity_not_guest_AB_qualification'}
    monitor = Monitor(backend, directory, report, limits)
    previous = {}
    try:
        before = inspect_endpoints(backend, 'direct')
        if before['configs'] != anchor['configs']:
            raise Blocked('direct endpoint configuration/resources differ from DUT anchor')
        save(directory / 'direct-before.json', before)
        backend.config['target'] = before['addresses']['traffic-server']
        direct_campaign = {'target': backend.config['target'], 'source_ips': [before['addresses']['traffic-client']]}
        monitor.context = {'phase': 'baseline'}
        monitor.monitor()
        baseline = monitor.samples[-1]
        if resources_proven([baseline, baseline], limits):
            raise Blocked('endpoint baseline resource headroom not established')
        if offered['kind'] == 'held':
            for service in SERVICES:
                row = baseline['endpoints'][service]
                if (min(p['fd_soft'] - p['fds'] for p in row['processes']) < offered['sessions'] + limits['min_fd_reserve']
                        or (service == 'traffic-client' and tuple_capacity(row)[1] < offered['sessions'] + limits['min_tuple_reserve'])):
                    raise Blocked('actual FD/tuple space does not support held target plus reserve')
        for sig in (signal.SIGINT, signal.SIGTERM):
            previous[sig] = signal.signal(sig, lambda *_: setattr(monitor, 'cancelled', True))
        window = {'run_id': report['run_id'], 'offered': offered, 'profile': 'DIRECT'}
        controller.execute_components(monitor, window, direct_campaign, 'measure', seconds, report)
        if report['gaps']:
            raise Blocked('remote job cleanup not confirmed; recovery cannot be claimed')
        monitor.context = {'phase': 'recovery'}
        monitor.pause(120)
        recovery_end = time.monotonic() + 480
        healthy = 0
        while True:
            monitor.monitor()
            last = monitor.samples[-1]
            recovered = all(last['endpoints'][s]['memory_current'] <= max(baseline['endpoints'][s]['memory_current'] * 1.2,
                            baseline['endpoints'][s]['memory_current'] + 64 * 1024**2)
                            and sum(p['fds'] for p in last['endpoints'][s]['processes']) <=
                            sum(p['fds'] for p in baseline['endpoints'][s]['processes']) + 32 for s in SERVICES)
            healthy = healthy + 1 if recovered else 0
            if healthy >= 2:
                break
            if time.monotonic() >= recovery_end:
                raise Blocked('recovery not normalized before bounded deadline')
            monitor.pause(limits['sample_seconds'])
        report['recovery'] = {'status': 'RECOVERED', 'minimum_seconds': 120}
        after = inspect_endpoints(backend, 'direct')
        save(directory / 'direct-after.json', after)
        if any(before['entries'][s]['Id'] != after['entries'][s]['Id'] or
               before['entries'][s]['State']['StartedAt'] != after['entries'][s]['State']['StartedAt'] for s in SERVICES):
            raise Blocked('endpoint restarted during direct measurement')
        if before['configs'] != after['configs'] or before['networks'] != after['networks'] or before['routes'] != after['routes']:
            raise Blocked('direct topology or configuration changed during measurement')
        measured, issues = measure_results(report, directory, offered, seconds, limits, monitor.samples)
        report['measurements'] = measured
        report['resource_issues'] = resources_proven(monitor.samples, limits)
        report['offered_achieved'] = not issues
        report['resources_unlimited'] = not report['resource_issues']
        report['resource_scope'] = 'No observed FD/tuple/memory/pids/cgroup bottleneck at declared sampled thresholds; not physical unlimited capacity'
        report['gaps'].extend(issues)
        report['status'] = 'MEASURED_PENDING_DUT_RETURN' if not (issues or report['resource_issues'] or report['gaps']) else 'CAPACITY_NOT_PROVEN'
    except Exception as error:
        report['gaps'].append(str(error) if isinstance(error, Blocked) else type(error).__name__)
        report['status'] = 'SAFETY_STOP' if report['components'] else 'BLOCKED'
        report['recovery'] = {'status': 'NOT_CONFIRMED', 'reason': 'load stopped; subsequent run requires independent health verification'}
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        report['finished_at'] = controller.utc()
        try:
            logs = backend.call(backend.compose + ['logs', '--no-color', '--since', report['started_at'], 'traffic-server'], timeout=15)
            (directory / 'receiver.log').write_text(logs, encoding='utf-8')
        except Exception:
            report['gaps'].append('receiver_log_unavailable')
            report['status'] = 'INVALID_MEASUREMENT'
        report['evidence'] = [reference(p) for p in directory.iterdir() if p.is_file() and p.name not in ('receipt.json', 'receipt.json.tmp')]
        save(directory / 'receipt.json', report)
    return report


def finalize(backend, path, isolation=None):
    directory = path.parent
    report = controller.read_json(path)
    if report['status'] != 'MEASURED_PENDING_DUT_RETURN':
        raise Blocked('only a complete measured candidate can finalize')
    for ref in report['evidence']:
        controller.artifact(ref, directory)
    anchor = json.loads(controller.artifact(report['anchor'], directory))
    validate_anchor(anchor, report['campaign'])
    returned = inspect_endpoints(backend, 'ngfw')
    topology = backend.topology()
    save(directory / 'dut-return.json', {'state': returned, 'topology': topology})
    if (returned['configs'] != anchor['configs'] or plan.fingerprint(topology) != anchor['topology_hash']
            or returned['routes'] != anchor['state']['routes'] or returned['addresses'] != anchor['state']['addresses']):
        raise Blocked('DUT return configuration/topology differs from actual pre-control anchor')
    if isolation is None:
        raise Blocked('fresh powered-off negative isolation after direct run required')
    negative = controller.read_json(isolation)
    runner.validate_isolation(negative, topology, time.time(), 3600, backend.config.get('campaign_id'))
    if (negative['checked_at'] <= controller.epoch(report['finished_at'])
            or negative.get('ngfw_state') != 'shut off' or negative.get('client_to_receiver') != 'unreachable'
            or negative.get('receiver_local_health') is not True):
        raise Blocked('post-control negative isolation is absent or predates direct run completion')
    if backend.probe_endpoint('traffic-client', backend.config['target']) is not True:
        raise Blocked('restored DUT dataplane health failed')
    direct = controller.read_json(directory / 'direct-before.json')
    receipt = {'kind': 'control_capacity', 'scope': 'isolated-ngfw-lab', 'campaign_id': report['campaign']['campaign_id'],
               'target': anchor['target'], 'config_hashes': report['campaign']['invariants'], 'observed_at': controller.utc(),
               'status': 'VALID', 'load_hash': plan.fingerprint(report['offered']), 'duration_seconds': report['duration_seconds'],
               'topology_hash': anchor['topology_hash'], 'control_topology_hash': plan.fingerprint(direct),
               'control_topology_evidence': reference(directory / 'direct-before.json'),
               'path_kind': 'direct_control', 'traverses_ngfw': False, 'offered_achieved': report['offered_achieved'],
               'resources_unlimited': report['resources_unlimited'], 'resource_scope': report['resource_scope'],
               'endpoint_config_hashes': {k: report['campaign']['invariants'][k] for k in ('generator', 'receiver')},
               'evidence': report['evidence'] + [reference(path), reference(directory / 'dut-return.json'), reference(isolation)]}
    save(directory / 'control-capacity.json', receipt)
    return receipt


def validate_install_config(config, project, legacy=False):
    services = config['services']
    if set(services) != set(SERVICES):
        raise Blocked('only existing two endpoint services may be installed')
    for service in services.values():
        if (service.get('ports') or service.get('privileged') or service.get('cap_add') or not service.get('read_only')
                or service.get('cap_drop') != ['ALL'] or len(service.get('networks', {})) != 1
                or service.get('network_mode') or service.get('pid') == 'host' or service.get('devices')):
            raise Blocked('candidate introduces privileges, exposure or multiple endpoint networks')
        nofile = service.get('ulimits', {}).get('nofile', {})
        if legacy:
            if not 0 < float(service.get('cpus', 0)) <= 4 or compose_integer(service.get('mem_limit', 0)) <= 0:
                raise Blocked('legacy endpoint resource bounds missing')
        else:
            validate_extreme_resources(service, nofile)
        mounts = service.get('volumes', [])
        if (len(mounts) != 1 or mounts[0].get('type') != 'bind' or mounts[0].get('target') != '/opt/extreme'
                or mounts[0].get('read_only') is not True
                or Path(mounts[0]['source']).resolve() != (Path(project) / 'extreme').resolve()):
            raise Blocked('candidate source mount differs from readonly public workload directory')
    if services['traffic-client'].get('entrypoint') != ['sleep', 'infinity'] or services['traffic-client'].get('command'):
        raise Blocked('client must start idle')
    expected_entry = ['python3', '/opt/traffic/endpoint.py' if legacy else '/opt/extreme/load.py']
    expected_command = ['server'] if legacy else ['serve', '--bind', '0.0.0.0', '--max-sessions', '60000', '--with-iperf']
    if services['traffic-server'].get('entrypoint') != expected_entry or services['traffic-server'].get('command') != expected_command:
        raise Blocked('one bounded selected receiver required')
    networks = config['networks']
    direct = 'control' in networks
    if direct and (legacy or len(networks) != 1 or networks['control'].get('internal') is not True or networks['control'].get('driver') != 'bridge'):
        raise Blocked('candidate direct network is not exclusively internal')
    if not direct and (set(networks) != {'left', 'right'} or any(not n.get('external') for n in networks.values())):
        raise Blocked('candidate DUT topology differs from existing external left/right networks')
    return direct


def compose_integer(value):
    # Compose emits normalized byte counts as decimal strings in some versions.
    if type(value) is int or (isinstance(value, str) and re.fullmatch(r'[0-9]+', value)):
        return int(value)
    raise Blocked('candidate integer resource is not a normalized decimal value')


def validate_extreme_resources(service, nofile):
    if (not 2 <= float(service.get('cpus', 0)) <= 4
            or not 2147483648 <= compose_integer(service.get('mem_limit', 0)) <= 4294967296
            or not 131072 <= compose_integer(nofile.get('soft', 0)) <= compose_integer(nofile.get('hard', 0)) <= 262144
            or not 96 <= compose_integer(service.get('pids_limit', 0)) <= 512):
        raise Blocked('candidate lacks bounded approved endpoint resources')
    sysctls = service.get('sysctls', {})
    if (sysctls.get('net.ipv4.ip_local_port_range') != '1024 65535'
            or sysctls.get('net.ipv4.ip_local_reserved_ports') != '5201,5353,8080,9000'
            or str(sysctls.get('net.ipv4.ip_forward')) != '0'):
        raise Blocked('candidate namespace tuple/forwarding bounds differ')


def reject_legacy_downgrade(config):
    reports = Path(config['reports_dir'])
    receipt = reports / 'endpoint-switch.json'
    prior = controller.read_json(receipt) if receipt.exists() else {}
    if ((reports / '.extreme-managed').exists() or prior.get('managed_extreme') is True
            or prior.get('state', {}).get('mode') in ('direct', 'ngfw')):
        raise Blocked('lab is extreme-managed; normal legacy apply cannot downgrade runtime')


def image_build_inputs(config, project):
    """Hash the supported local Dockerfile and every file its COPY reads.

    Unsupported build features fail closed rather than omitting an input from
    the reuse decision. Network attachment and bind-mounted code are not image
    inputs; the latter still requires endpoint recreation on every install.
    """
    specs = []
    for name in SERVICES:
        service = config['services'][name]
        build, tag = service.get('build'), service.get('image')
        if (not isinstance(build, dict) or set(build) - {'context', 'dockerfile'}
                or not isinstance(tag, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/:@-]*', tag)
                or service.get('platform') or service.get('pull_policy')):
            raise Blocked('unsupported endpoint image build settings')
        context = Path(build.get('context', ''))
        context = context if context.is_absolute() else project / context
        if context.is_symlink() or context.resolve() != (project / 'traffic').resolve():
            raise Blocked('endpoint build context must be local traffic directory')
        if build.get('dockerfile', 'Dockerfile') != 'Dockerfile':
            raise Blocked('unsupported endpoint Dockerfile path')
        specs.append({'context': str(context.resolve()), 'tag': tag})
    if specs[0] != specs[1]:
        raise Blocked('endpoints must share the same image build inputs and tag')
    context = Path(specs[0]['context'])
    dockerfile = context / 'Dockerfile'
    if dockerfile.is_symlink() or not dockerfile.is_file():
        raise Blocked('endpoint Dockerfile missing or symlinked')
    raw = dockerfile.read_bytes()
    logical = re.sub(r'\\\r?\n', ' ', raw.decode('utf-8'))
    copied = set()
    for line in logical.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        instruction, _, args = line.partition(' ')
        instruction = instruction.upper()
        if instruction not in {'FROM', 'ENV', 'RUN', 'WORKDIR', 'COPY', 'USER', 'ENTRYPOINT', 'CMD', 'LABEL'}:
            raise Blocked('unsupported Dockerfile instruction for image reuse')
        if instruction == 'RUN' and '--mount' in args:
            raise Blocked('external Dockerfile RUN mount inputs unsupported')
        if instruction == 'COPY':
            parts = args.split()
            if (len(parts) < 2 or parts[-1] != './'
                    or any(not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', part) for part in parts[:-1])):
                raise Blocked('unsupported Dockerfile COPY inputs for image reuse')
            copied.update(parts[:-1])
    if not copied:
        raise Blocked('endpoint Dockerfile has no supported COPY inputs')
    hashes = {'Dockerfile': hashlib.sha256(raw).hexdigest()}
    for name in sorted(copied | {'.dockerignore', 'Dockerfile.dockerignore'}):
        path = context / name
        if path.is_symlink() or (name in copied and not path.is_file()):
            raise Blocked('endpoint image input missing or symlinked')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return dict(specs[0], files=hashes)


def image_identity(backend, tag):
    value = json.loads(backend.call(['docker', 'image', 'inspect', '--', tag], timeout=30))
    if (not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict)
            or not re.fullmatch(r'sha256:[0-9a-f]{64}', str(value[0].get('Id', '')))):
        raise Blocked('Docker image identity readback invalid')
    return value[0]['Id']


def ensure_endpoint_image(backend, candidate, config, project):
    """Reuse only a locally observed successful build with an unchanged tag ID."""
    inputs = image_build_inputs(config, project)
    fingerprint = plan.fingerprint(inputs)
    path = Path(backend.config['reports_dir']) / 'endpoint-image-build.json'
    try:
        pointer = controller.read_json(path)
        prior_ref = pointer.get('build_receipt') if isinstance(pointer, dict) else None
        prior = json.loads(controller.artifact(prior_ref, path.parent))
    except (OSError, ValueError, TypeError, Blocked):
        prior = {}
    if (isinstance(prior, dict) and prior.get('schema') == 1
            and prior.get('inputs') == inputs and prior.get('input_sha256') == fingerprint
            and re.fullmatch(r'sha256:[0-9a-f]{64}', str(prior.get('image_id', '')))):
        try:
            current = image_identity(backend, inputs['tag'])
        except runner.Abort:
            current = None
        if current == prior['image_id']:
            return dict(action='REUSED', image_id=current, input_sha256=fingerprint, build_receipt=prior_ref)
    # Both services have identical image inputs. Build once; separate builds of
    # the same tag can regenerate OCI index/attestation IDs despite equal layers.
    backend.call(backend.compose + ['--file', str(candidate), 'build', 'traffic-client'], timeout=300)
    image_id = image_identity(backend, inputs['tag'])
    if image_build_inputs(config, project) != inputs:
        raise Blocked('endpoint build inputs changed during build')
    receipt = dict(schema=1, built_at=controller.utc(), inputs=inputs,
                   input_sha256=fingerprint, image_id=image_id)
    archive = path.parent / 'endpoint-image-builds'
    archive.mkdir(exist_ok=True)
    immutable = archive / (plan.fingerprint(receipt) + '.json')
    if immutable.exists():
        if controller.read_json(immutable) != receipt:
            raise Blocked('immutable image build receipt already differs')
    else:
        save(immutable, receipt)
    ref = reference(immutable)
    # Only this cache pointer changes; archived endpoint-switch references keep
    # their original build evidence after later source changes and rebuilds.
    save(path, dict(schema=1, build_receipt=ref))
    return dict(action='BUILT', image_id=image_id, input_sha256=fingerprint, build_receipt=ref)


def verify_endpoint_image(backend, image_id):
    for service in SERVICES:
        cid = backend.call(backend.compose + ['ps', '-q', service], timeout=15).strip()
        if not re.fullmatch(r'[0-9a-f]{12,64}', cid):
            raise Blocked('endpoint container identity readback invalid')
        entries = json.loads(backend.call(['docker', 'inspect', cid], timeout=15))
        if (not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict)
                or entries[0].get('Image') != image_id):
            raise Blocked('running endpoint image differs from selected build identity')


def install_endpoints(backend, candidate, source_tree=None, legacy=False, start=True):
    """Called by Ansible while holding the same lock as every traffic executor."""
    project = Path(backend.config['project_dir']).resolve()
    runner.validate(backend.config)
    config = json.loads(backend.call(backend.compose + ['--file', str(candidate), 'config', '--format', 'json'], timeout=30))
    direct = validate_install_config(config, project, legacy)
    if legacy:
        reject_legacy_downgrade(backend.config)
    else:
        (Path(backend.config['reports_dir']) / '.extreme-managed').write_text(controller.utc() + '\n', encoding='utf-8')
        if not start and any(backend.call(backend.compose + ['ps', '-q', service]).strip() for service in SERVICES):
            raise Blocked('cannot promote mounted extreme sources without recreating existing endpoints')
    if source_tree:
        source_tree = Path(source_tree).resolve()
        paths = list(source_tree.glob('*.py'))
        for name in ('traffic', 'extreme', 'native_analysis', 'plans'):
            paths.extend(p for p in (source_tree / name).rglob('*') if p.is_file() and p.suffix in ('.py', '.json', '.md'))
        paths.extend(p for p in (source_tree / 'monitor').rglob('*') if p.is_file() and p.suffix in ('.html', '.css', '.js'))
        dockerfile = source_tree / 'traffic' / 'Dockerfile'
        if dockerfile.exists():
            paths.append(dockerfile)
        if (source_tree / 'ngfw_probe.sh').exists():
            paths.append(source_tree / 'ngfw_probe.sh')
        for path in paths:
            if path.is_symlink():
                raise Blocked('staged source symlinks unsupported')
            dest = project / path.relative_to(source_tree)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(dest.parent, 0o755)
            shutil.copyfile(path, dest)
            os.chmod(dest, 0o644)
        if (source_tree / 'runner.json').exists():
            shutil.copyfile(source_tree / 'runner.json', project / 'runner.json')
    # Build before replacing active Compose. A failed up is retained as a failed
    # switch, never reported as restored or a capacity pass.
    if start:
        image = ensure_endpoint_image(backend, candidate, config, project)
    dest = project / 'compose.yml'
    temporary = project / 'compose.promote.yml'
    shutil.copyfile(candidate, temporary)
    os.chmod(temporary, 0o644)
    temporary.replace(dest)
    if not start:
        state = {'mode': 'legacy' if legacy else 'not_started', 'planned_path': 'direct' if direct else 'ngfw'}
        save(Path(backend.config['reports_dir']) / 'endpoint-switch.json',
             {'status': 'STAGED_NOT_STARTED', 'managed_extreme': not legacy, 'state': state})
        return state
    # Bind-mounted Python can change without a Compose config/image change.
    # Recreate even in same-path installs so source hashes describe loaded code.
    backend.call(backend.compose + ['up', '--detach', '--no-build', '--pull', 'never', '--force-recreate'], timeout=120)
    verify_endpoint_image(backend, image['image_id'])
    state = {'mode': 'legacy', 'topology': backend.topology()} if legacy else inspect_endpoints(backend, 'direct' if direct else 'ngfw')
    deadline = time.monotonic() + 30
    while True:
        try:
            if backend.probe_endpoint('traffic-server', '127.0.0.1') is not True:
                raise Blocked('receiver local health returned false')
            break
        except runner.Abort:
            if time.monotonic() >= deadline:
                raise Blocked('receiver readiness deadline exceeded')
            time.sleep(1)
    save(Path(backend.config['reports_dir']) / 'endpoint-switch.json',
         {'status': 'INSTALLED', 'managed_extreme': not legacy, 'state': state, 'image': image})
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install-endpoints', 'capture-dut', 'run', 'finalize'])
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--source-tree', type=Path)
    parser.add_argument('--legacy', action='store_true', help='main-role bootstrap only; refuses an extreme-managed lab')
    parser.add_argument('--no-start', action='store_true', help='promote configuration/code only, never start endpoints')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--isolation', type=Path)
    parser.add_argument('--campaign', type=Path)
    parser.add_argument('--anchor', type=Path)
    parser.add_argument('--workload')
    parser.add_argument('--seconds', type=int, default=300)
    parser.add_argument('--limits', type=Path)
    parser.add_argument('--receipt', type=Path)
    args = parser.parse_args()
    if os.name != 'posix':
        parser.error('Linux Docker host required; local tests use fake backends')
    os.umask(0o077)
    config = controller.read_json(args.config)
    with locked(config):
        backend = runner.Backend(config)
        try:
            if args.action == 'install-endpoints':
                if not args.candidate:
                    parser.error('--candidate required')
                if args.legacy:
                    reject_legacy_downgrade(config)
                else:
                    (Path(config['reports_dir']) / '.extreme-managed').write_text(controller.utc() + '\n', encoding='utf-8')
                transition = {'status': 'IN_PROGRESS', 'managed_extreme': not args.legacy,
                              'started_at': controller.utc(), 'candidate': reference(args.candidate)}
                switch_path = Path(config['reports_dir']) / 'endpoint-switch.json'
                save(switch_path, transition)
                try:
                    state = install_endpoints(backend, args.candidate, args.source_tree, args.legacy, not args.no_start)
                    print(json.dumps({'status': 'STAGED_NOT_STARTED' if args.no_start else 'INSTALLED',
                                      'mode': state['mode'], 'receipt': str(switch_path)}))
                except Exception as error:
                    transition.update(status='FAILED', error=type(error).__name__, finished_at=controller.utc())
                    save(switch_path, transition)
                    raise
            elif args.action == 'finalize':
                if not args.receipt or not args.isolation:
                    parser.error('--receipt and fresh post-return --isolation required')
                switch = controller.read_json(Path(config['reports_dir']) / 'endpoint-switch.json')
                if switch.get('status') != 'INSTALLED' or switch.get('state', {}).get('mode') != 'ngfw':
                    raise Blocked('DUT restoration is not confirmed by last endpoint transition')
                finalization = {'status': 'BLOCKED', 'attempted_at': controller.utc()}
                try:
                    finalize(backend, args.receipt, args.isolation)
                    finalization['status'] = 'VALID'
                finally:
                    save(args.receipt.parent / 'finalization.json', finalization)
            else:
                switch = controller.read_json(Path(config['reports_dir']) / 'endpoint-switch.json')
                expected_mode = 'ngfw' if args.action == 'capture-dut' else 'direct'
                if switch.get('status') != 'INSTALLED' or switch.get('state', {}).get('mode') != expected_mode:
                    raise Blocked('last endpoint switch failed or mode differs; resolve through Ansible')
                if not args.output or (args.action == 'capture-dut' and not args.isolation):
                    parser.error('new --output directory and capture --isolation required')
                args.output.mkdir(parents=True, exist_ok=False)
                if args.action == 'capture-dut':
                    capture_dut(backend, args.output, args.isolation)
                else:
                    if not all((args.campaign, args.anchor, args.workload, args.limits)):
                        parser.error('run requires --campaign --anchor --workload --limits')
                    report = run_capacity(backend, controller.read_json(args.campaign), args.anchor, args.workload,
                                          args.output, args.seconds, capacity_limits(controller.read_json(args.limits)))
                    print(json.dumps({'status': report['status'], 'receipt': str(args.output / 'receipt.json')}))
                    return 0 if report['status'] == 'MEASURED_PENDING_DUT_RETURN' else 2
        finally:
            backend.pool.shutdown(wait=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (runner.Abort, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'BLOCKED', 'error': str(error) if isinstance(error, Blocked) else type(error).__name__}))
        raise SystemExit(2)
