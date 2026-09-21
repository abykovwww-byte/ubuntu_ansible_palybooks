"""Measurement derivation and opt-in safety gates; standard library only."""
import hashlib
import json
import math
from pathlib import Path
import re

import measure_probe

AUDIT_FIXED_CONTROLS = ('enabled', 'failure', 'pid', 'rate_limit', 'backlog_limit', 'backlog_wait_time')


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate_busy_poll_baseline(value):
    if (not isinstance(value, dict)
            or set(value) != {'boot_id', 'observed_seconds', 'evidence_sha256', 'cores'}):
        raise ValueError('busy-poll baseline requires exact evidence and core identity fields')
    if not isinstance(value['boot_id'], str) or not re.fullmatch(
            r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['boot_id']):
        raise ValueError('busy-poll baseline requires the actual guest boot UUID')
    if not number(value['observed_seconds']) or not 60 <= value['observed_seconds'] <= 86400:
        raise ValueError('busy-poll baseline requires 60..86400 seconds of observation')
    if not isinstance(value['evidence_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', value['evidence_sha256']):
        raise ValueError('busy-poll baseline requires observation evidence SHA256')
    cores = value['cores']
    if not isinstance(cores, list) or not 1 <= len(cores) <= 16:
        raise ValueError('busy-poll baseline requires 1..16 explicit core identities')
    seen_cores, seen_threads = set(), set()
    for row in cores:
        fields = {'cpu', 'pid', 'process_start_ticks', 'process_comm', 'tid',
                  'thread_start_ticks', 'thread_comm', 'affinity'}
        if not isinstance(row, dict) or set(row) != fields:
            raise ValueError('busy-poll core identity requires exact fields')
        if (not isinstance(row['cpu'], str) or not re.fullmatch(r'cpu(?:0|[1-9][0-9]{0,2})', row['cpu'])
                or int(row['cpu'][3:]) > 511):
            raise ValueError('invalid busy-poll CPU identity')
        for key in ('pid', 'tid', 'process_start_ticks', 'thread_start_ticks'):
            if type(row[key]) is not int or not 0 < row[key] <= 2**63 - 1:
                raise ValueError('invalid busy-poll task identity: ' + key)
        if row['pid'] > 2147483647 or row['tid'] > 2147483647:
            raise ValueError('busy-poll PID/TID outside Linux task identifier range')
        for key in ('process_comm', 'thread_comm'):
            if not isinstance(row[key], str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', row[key]):
                raise ValueError('busy-poll comm must be an exact bounded Linux task name')
        if (not isinstance(row['affinity'], list) or len(row['affinity']) != 1
                or type(row['affinity'][0]) is not int or row['affinity'][0] != int(row['cpu'][3:])):
            raise ValueError('busy-poll affinity must be exactly its one selected CPU')
        thread = (row['pid'], row['tid'])
        if row['cpu'] in seen_cores or thread in seen_threads:
            raise ValueError('duplicate busy-poll CPU or PID/TID identity')
        seen_cores.add(row['cpu'])
        seen_threads.add(thread)
    return value


def validate(config):
    if config.get('schema_version') != 1:
        raise ValueError('measurement schema_version must be 1')
    for key in ('process_names', 'host_temperature_keys', 'guest_disk_devices', 'host_disk_devices'):
        values = config.get(key)
        if not isinstance(values, list) or len(values) > 64 or not all(
                isinstance(v, str) and 0 < len(v) <= 160 and '\n' not in v for v in values):
            raise ValueError('invalid measurement list: ' + key)
    if not config['host_temperature_keys'] or not config['guest_disk_devices']:
        raise ValueError('select actual host sensors and guest audit disk before load')
    for name in config['process_names']:
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name):
            raise ValueError('process_names must be exact Linux comm names (max 15)')
    for key in ('temperature_start_c', 'temperature_stop_c', 'single_core_pct',
                'single_core_seconds', 'disk_await_ms', 'disk_await_seconds',
                'service_probe_ms', 'service_probe_seconds', 'max_sample_seconds'):
        value = config.get(key)
        if not number(value) or value <= 0:
            raise ValueError('operator must set positive measurement threshold: ' + key)
    if not config['temperature_start_c'] < config['temperature_stop_c'] < 120:
        raise ValueError('temperature_start_c < temperature_stop_c < 120 required')
    if config['single_core_pct'] >= 100 or config['max_sample_seconds'] > 30:
        raise ValueError('invalid core threshold or collection deadline')
    for key in ('single_core_seconds', 'disk_await_seconds', 'service_probe_seconds'):
        if config[key] > 300:
            raise ValueError('hold time outside 0..300 seconds')
    allowed = {'schema_version', 'process_names', 'host_temperature_keys', 'guest_disk_devices',
               'host_disk_devices', 'temperature_start_c', 'temperature_stop_c', 'single_core_pct',
               'single_core_seconds', 'disk_await_ms', 'disk_await_seconds', 'service_probe_ms',
               'service_probe_seconds', 'max_sample_seconds', 'guest_busy_poll_baseline'}
    if set(config) - allowed:
        raise ValueError('unknown measurement setting')
    if 'guest_busy_poll_baseline' in config:
        validate_busy_poll_baseline(config['guest_busy_poll_baseline'])
    return config


def source(process_names=None, inventory=False, guest_busy_poll_baseline=None):
    options = {'process_names': process_names or ['auditd', 'rsyslogd'], 'inventory': inventory}
    if guest_busy_poll_baseline is not None:
        baseline = validate_busy_poll_baseline(guest_busy_poll_baseline)
        options['thread_targets'] = [{'pid': row['pid'], 'tid': row['tid']} for row in baseline['cores']]
    return Path(measure_probe.__file__).read_text(encoding='utf-8').replace(
        'OPTIONS = {}', 'OPTIONS = ' + repr(options), 1)


def control_changes(before, after):
    """Compare fixed kernel AuditD controls in two full probe snapshots."""
    first, last = (before or {}).get('audit') or {}, (after or {}).get('audit') or {}
    reasons = []
    for field in AUDIT_FIXED_CONTROLS:
        old, current = first.get(field), last.get(field)
        if type(old) is not int or old < 0 or type(current) is not int or current < 0:
            reasons.append('fixed AuditD control unavailable: ' + field)
        elif old != current:
            reasons.append('fixed AuditD control changed: ' + field)
    return reasons


def inventory_risks(snapshot):
    problems = control_changes(snapshot, snapshot)
    audit = snapshot.get('audit') or {}
    inv = snapshot.get('inventory') or {}
    # The operator selected the existing EDR runtime profile as baseline.
    # Both silent (0) and printk (1) are availability-preserving; panic (2) is not.
    if audit.get('enabled') != 1 or audit.get('failure') not in (0, 1) or audit.get('lost') != 0:
        problems.append('audit controls missing/unsafe: require enabled=1, failure=0 or 1, lost=0')
    if not inv.get('rules_sha256') or not inv.get('auditd_conf_sha256'):
        problems.append('effective rules/config identity unavailable')
    if not inv.get('auditd_active') or not inv.get('dataplane_active'):
        problems.append('auditd/dataplane active state not confirmed')
    cfg = inv.get('auditd_conf') or {}
    if cfg.get('standard_log_path') is False or cfg.get('write_logs') == 'no':
        problems.append('probe requires local standard audit.log recording')
    known_defaults = measure_probe.effective_defaults(cfg, inv.get('auditd_package') or {})
    for key in ('space_left_action', 'admin_space_left_action', 'max_log_file_action',
                'disk_full_action', 'disk_error_action', 'overflow_action'):
        value = cfg.get(key)
        expected_default = known_defaults.get(key)
        reported_default = (inv.get('effective_defaults') or {}).get(key)
        if key not in cfg and expected_default and reported_default == expected_default:
            value = expected_default['value']
        if value not in {'ignore', 'syslog', 'rotate', 'suspend', 'email', 'keep_logs'}:
            problems.append(key + ' missing or potentially fail-closed; review installed-version defaults')
    return problems


def legacy_guest(row):
    """Keep the existing Guard active when the JSON probe replaces its shell input."""
    cpu, audit, log = (row.get('cpu') or {}).get('cpu', {}), row.get('audit') or {}, row.get('audit_log') or {}
    result = {'boot_id': row.get('boot_id')}
    if cpu:
        result.update(cpu_total=sum(cpu.values()), cpu_idle=cpu['idle'] + cpu['iowait'])
    for key in ('enabled', 'lost', 'backlog', 'backlog_limit'):
        if key in audit:
            result['audit_' + key] = audit[key]
    for key, prefix in [('root_fs', 'root'), ('audit_fs', 'audit_disk')]:
        fs = row.get(key)
        if fs and fs['total_bytes']:
            result[prefix + '_used_pct'] = 100 * (1 - fs['free_bytes'] / fs['total_bytes'])
    if row.get('audit_fs'):
        result['audit_free_bytes'] = row['audit_fs']['free_bytes']
    if row.get('memory'):
        result['memory_used_pct'] = row['memory']['used_pct']
    if log:
        result.update(log_inode=log['inode'], log_bytes=log['bytes'])
    return result


def derive(previous, current):
    out = {'cpu': {}, 'disks': {}, 'processes': {}, 'audit_wait_delta': None,
           'audit_lost_delta': None, 'log_bytes_per_second': None, 'rotation': False, 'gaps': []}
    if not previous or current.get('boot_id') != previous.get('boot_id'):
        out['gaps'].append('first sample or boot boundary')
        return out
    dt = current['monotonic'] - previous['monotonic']
    if dt <= 0:
        out['gaps'].append('nonpositive interval')
        return out
    out['interval_seconds'] = dt
    for name, row in (current.get('cpu') or {}).items():
        old = (previous.get('cpu') or {}).get(name)
        if not old:
            continue
        delta = {k: row[k] - old[k] for k in measure_probe.CPU_FIELDS}
        total = sum(delta.values())
        if total <= 0 or min(delta.values()) < 0:
            out['gaps'].append('CPU counter reset: ' + name)
            continue
        pct = {k + '_pct': 100 * v / total for k, v in delta.items()}
        pct['busy_pct'] = 100 - pct['idle_pct'] - pct['iowait_pct']
        out['cpu'][name] = pct
    for name, row in (current.get('disks') or {}).items():
        old = (previous.get('disks') or {}).get(name)
        if not old or old['major_minor'] != row['major_minor']:
            continue
        keys = ('reads', 'writes', 'read_ms', 'write_ms', 'read_sectors', 'write_sectors', 'io_ms', 'weighted_ms')
        delta = {k: row[k] - old[k] for k in keys}
        if min(delta.values()) < 0:
            out['gaps'].append('disk counter reset: ' + name)
            continue
        operations = delta['reads'] + delta['writes']
        out['disks'][name] = {'await_ms': (delta['read_ms'] + delta['write_ms']) / operations if operations else None,
                             'iops': operations / dt,
                             'read_bytes_s': delta['read_sectors'] * 512 / dt,
                             'write_bytes_s': delta['write_sectors'] * 512 / dt,
                             'util_pct': delta['io_ms'] / (dt * 10),
                             'avg_queue_depth': delta['weighted_ms'] / (dt * 1000)}
    for pid, row in (current.get('processes') or {}).get('values', {}).items():
        old = (previous.get('processes') or {}).get('values', {}).get(pid)
        if not old or row['start_ticks'] != old['start_ticks']:
            continue
        ticks = sum(row[k] - old[k] for k in ('user_ticks', 'system_ticks'))
        hz = current.get('clock_ticks')
        if ticks >= 0 and hz:
            out['processes'][pid] = {'comm': row['comm'], 'cpu_one_core_pct': 100 * ticks / hz / dt,
                                     'state': row['state'], 'wchan': row.get('wchan')}
    for field, dest in [('backlog_wait_time_actual', 'audit_wait_delta'), ('lost', 'audit_lost_delta')]:
        x, y = (previous.get('audit') or {}).get(field), (current.get('audit') or {}).get(field)
        if number(x) and number(y) and y >= x:
            out[dest] = y - x  # Native audit counter units, not falsely labelled milliseconds.
    old, row = previous.get('audit_log') or {}, current.get('audit_log') or {}
    if old and row:
        if old['inode'] != row['inode'] or row['bytes'] < old['bytes']:
            out['rotation'] = True
            out['gaps'].append('log rotation/truncation interval; rate unknown')
        else:
            out['log_bytes_per_second'] = (row['bytes'] - old['bytes']) / dt
    return out


class Guard:
    def __init__(self, config, initial_guest=None):
        self.config = validate(config)
        self.previous = {'guest': initial_guest} if initial_guest is not None else {}
        self.since = {}

    def held(self, name, condition, now, seconds):
        if not condition:
            self.since.pop(name, None)
            return False
        self.since.setdefault(name, now)
        return now - self.since[name] >= seconds

    def busy_poll_cores(self, current):
        baseline = self.config.get('guest_busy_poll_baseline')
        if baseline is None:
            return set(), []
        reasons = []
        selected = {row['cpu'] for row in baseline['cores']}
        actual = {name for name in (current.get('cpu') or {}) if re.fullmatch(r'cpu[0-9]+', name)}
        if current.get('boot_id') != baseline['boot_id']:
            reasons.append('guest busy-poll baseline boot changed')
        if not selected <= actual:
            reasons.append('guest busy-poll baseline references an unavailable CPU')
        if not actual - selected:
            reasons.append('guest busy-poll baseline must leave at least one guest core monitored')
        threads = current.get('busy_poll_threads') or {}
        if threads.get('unavailable') or not threads.get('values'):
            reasons.append('guest busy-poll thread observations unavailable')
        for expected in baseline['cores']:
            key = f"{expected['pid']}:{expected['tid']}"
            row = (threads.get('values') or {}).get(key) or {}
            if any(row.get(field) != expected[field] for field in expected if field != 'cpu'):
                reasons.append('guest busy-poll task identity/affinity changed: ' + expected['cpu'])
            elif any(row.get(field) not in {'R', 'S', 'D', 'I'}
                     for field in ('process_state', 'thread_state')):
                reasons.append('guest busy-poll task stopped/state unavailable: ' + expected['cpu'])
        verified = selected if not reasons else set()
        current['busy_poll_baseline'] = {'expected_cores': sorted(selected), 'verified_cores': sorted(verified),
                                        'observed_seconds': baseline['observed_seconds'],
                                        'evidence_sha256': baseline['evidence_sha256'],
                                        'scope': 'absolute-busy-pct-only; counters remain measured'}
        return verified, reasons

    def check(self, sample, initial=False):
        reasons = []
        now, cfg = sample['monotonic'], self.config
        measurements = sample.get('measurement') or {}
        for side in ('host', 'guest'):
            current = measurements.get(side)
            if not isinstance(current, dict) or not current.get('cpu') or not current.get('boot_id'):
                reasons.append(side + ' extended measurement unavailable')
                continue
            old = self.previous.get(side)
            if side == 'guest':
                audit = current.get('audit') or {}
                if audit.get('enabled') != 1 or audit.get('failure') not in (0, 1) or not audit.get('pid'):
                    reasons.append('AuditD enabled/failure/pid controls unavailable or unsafe')
                reasons.extend(control_changes(old if old is not None else current, current))
                device = (current.get('audit_fs') or {}).get('device')
                if not device or not any((current.get('disks') or {}).get(name, {}).get('major_minor') == device
                                         for name in cfg['guest_disk_devices']):
                    reasons.append('selected guest disks do not include audit filesystem device')
            if old and old['boot_id'] != current['boot_id']:
                reasons.append(side + ' boot changed')
            derived = derive(old, current)
            current['derived'] = derived
            busy_poll_cores = set()
            if side == 'guest':
                busy_poll_cores, busy_poll_reasons = self.busy_poll_cores(current)
                reasons.extend(busy_poll_reasons)
            for device in cfg[side + '_disk_devices']:
                if device not in (current.get('disks') or {}):
                    reasons.append(side + ' selected disk unavailable: ' + device)
                    continue
                latency = derived['disks'].get(device, {}).get('await_ms')
                if self.held(side + device, latency is not None and latency >= cfg['disk_await_ms'],
                             now, cfg['disk_await_seconds']):
                    reasons.append(side + ' sustained disk latency')
            for core, row in derived['cpu'].items():
                if core in busy_poll_cores:
                    continue  # Only verified guest polling cores; host and other guards remain unchanged.
                if core != 'cpu' and self.held(side + core, row['busy_pct'] >= cfg['single_core_pct'],
                                               now, cfg['single_core_seconds']):
                    reasons.append(side + ' sustained single-core CPU: ' + core)
            self.previous[side] = current
        temps = (measurements.get('host') or {}).get('temperature_c') or {}
        for name in cfg['host_temperature_keys']:
            value = temps.get(name)
            if not number(value):
                reasons.append('selected temperature sensor unavailable: ' + name)
            elif value >= cfg['temperature_start_c' if initial else 'temperature_stop_c']:
                reasons.append('host temperature threshold: ' + name)
        latency = sample.get('service_probe_ms')
        if not number(latency):
            reasons.append('service probe latency unavailable')
        elif self.held('service', latency >= cfg['service_probe_ms'], now, cfg['service_probe_seconds']):
            reasons.append('sustained service probe latency')
        if sample.get('collection_seconds', math.inf) > cfg['max_sample_seconds']:
            reasons.append('measurement collection deadline exceeded')
        return reasons


def load_context(path):
    value = json.loads(Path(path).read_text(encoding='utf-8'))
    keys = {'campaign_id', 'test_id', 'comparison_id', 'workload_id', 'profile_id'}
    if set(value) != keys or not all(isinstance(v, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}', v)
                                    for v in value.values()):
        raise ValueError('context requires only five bounded campaign/test/comparison/workload/profile IDs')
    if not re.fullmatch(r'T(?:0[0-9]|1[0-9]|20)|T15-[RF]', value['test_id']):
        raise ValueError('invalid methodology test ID')
    return value


def manifest(directory):
    files = []
    for path in sorted(Path(directory).iterdir()):
        if path.is_file() and path.name != 'manifest.json':
            digest = hashlib.sha256()
            with path.open('rb') as src:
                for block in iter(lambda: src.read(1048576), b''):
                    digest.update(block)
            files.append({'file': path.name, 'bytes': path.stat().st_size, 'sha256': digest.hexdigest()})
    return {'schema_version': 1, 'files': files}
