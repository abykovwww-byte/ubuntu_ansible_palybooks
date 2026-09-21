#!/usr/bin/env python3
"""Bounded read-only Linux snapshot. No packages, signals, config writes or raw logs.

May be sent to an already authorized guest as `python3 -` over SSH stdin.
OPTIONS is replaced with validated non-secret options by measurement.py.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time

OPTIONS = {}
PROC = Path('/proc')
SYS = Path('/sys')
AUDIT_CONF = Path('/etc/audit/auditd.conf')
AUDIT_LOG = Path('/var/log/audit/audit.log')
CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')
AUDIT_FIELDS = {'enabled', 'failure', 'pid', 'rate_limit', 'backlog_limit', 'lost',
                'backlog', 'backlog_wait_time', 'backlog_wait_time_actual'}
CONF_FIELDS = {'flush', 'freq', 'num_logs', 'max_log_file', 'max_log_file_action',
               'space_left', 'space_left_action', 'admin_space_left',
               'admin_space_left_action', 'disk_full_action', 'disk_error_action',
               'q_depth', 'overflow_action', 'write_logs', 'log_format'}


def text(path, limit=262144):
    with Path(path).open('r', encoding='utf-8', errors='replace') as src:
        value = src.read(limit + 1)
    if len(value) > limit:
        raise ValueError('read bound exceeded')
    return value


def command(argv):
    # Only fixed read-only commands below call this helper. Do not return stderr.
    result = subprocess.run(argv, text=True, capture_output=True, timeout=2,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode or len(result.stdout) > 1048576:
        raise ValueError('read-only command unavailable')
    return result.stdout


def cpu_counters(value):
    return {parts[0]: dict(zip(CPU_FIELDS, map(int, parts[1:9])))
            for line in value.splitlines() if (parts := line.split())
            and re.fullmatch(r'cpu[0-9]*', parts[0]) and len(parts) >= 9}


def disk_counters(value):
    result = {}
    for line in value.splitlines():
        p = line.split()
        if len(p) < 14:
            continue
        nums = list(map(int, p[3:]))
        result[p[2]] = {'major_minor': p[0] + ':' + p[1],
                       'reads': nums[0], 'read_sectors': nums[2], 'read_ms': nums[3],
                       'writes': nums[4], 'write_sectors': nums[6], 'write_ms': nums[7],
                       'in_flight': nums[8], 'io_ms': nums[9], 'weighted_ms': nums[10]}
    return result


def status(value):
    result = {}
    for line in value.splitlines():
        p = line.split()
        if len(p) == 2 and p[0] in AUDIT_FIELDS and p[1].isdigit():
            result[p[0]] = int(p[1])
    return result


def config_summary(value):
    result = {}
    for line in value.splitlines():
        key, sep, val = line.partition('=')
        key, val = key.strip().lower(), val.split('#', 1)[0].strip()
        if sep and key in CONF_FIELDS:
            # No exec paths, arguments, mail addresses, hosts or arbitrary text.
            result[key] = val.lower() if re.fullmatch(r'[a-zA-Z0-9_%]+', val) else 'REDACTED'
        if sep and key == 'log_file':
            result['standard_log_path'] = val == str(AUDIT_LOG)
    return result


def filesystem(path):
    st, fs = os.stat(path), os.statvfs(path)
    return {'device': f'{os.major(st.st_dev)}:{os.minor(st.st_dev)}',
            'free_bytes': fs.f_bavail * fs.f_frsize,
            'total_bytes': fs.f_blocks * fs.f_frsize,
            'free_inodes': fs.f_favail, 'total_inodes': fs.f_files}


def memory():
    values = {p[0].rstrip(':'): int(p[1]) for line in text(PROC / 'meminfo').splitlines()
              if len(p := line.split()) >= 2 and p[0] in {'MemTotal:', 'MemAvailable:'}}
    return {'used_pct': 100 * (1 - values['MemAvailable'] / values['MemTotal'])}


def sensors():
    values = {}
    for hwmon in sorted((SYS / 'class/hwmon').glob('hwmon*'))[:32]:
        try:
            name = text(hwmon / 'name', 128).strip()
        except (OSError, ValueError):
            name = hwmon.name
        for entry in sorted(hwmon.glob('temp*_input'))[:64]:
            try:
                label = text(entry.with_name(entry.name.replace('_input', '_label')), 128).strip()
            except (OSError, ValueError):
                label = entry.stem
            try:
                values[f'{name}/{hwmon.name}/{label}'] = int(text(entry, 64)) / 1000
            except (OSError, ValueError):
                continue
    return values


def frequencies():
    result = {}
    for cpu in sorted((SYS / 'devices/system/cpu').glob('cpu[0-9]*'))[:512]:
        row = {}
        for name in ('scaling_cur_freq', 'scaling_max_freq'):
            try:
                row[name + '_khz'] = int(text(cpu / 'cpufreq' / name, 64))
            except (OSError, ValueError):
                pass
        for name in ('core_throttle_count', 'package_throttle_count'):
            try:
                row[name] = int(text(cpu / 'thermal_throttle' / name, 64))
            except (OSError, ValueError):
                pass
        if row:
            result[cpu.name] = row
    return result


def process_counters(names):
    result = {}
    scanned = 0
    truncated = False
    for item in PROC.iterdir():
        if not item.name.isdigit():
            continue
        scanned += 1
        if scanned > 8192 or len(result) >= 64:
            truncated = True
            break
        try:
            name = text(item / 'comm', 128).strip()
            if name not in names:
                continue
            # stat comm can contain spaces and parentheses; fields start after last ')'.
            fields = text(item / 'stat', 8192).rsplit(')', 1)[1].split()
            row = {'comm': name, 'state': fields[0], 'start_ticks': int(fields[19]),
                   'user_ticks': int(fields[11]), 'system_ticks': int(fields[12]),
                   'rss_pages': int(fields[21]), 'wchan': None, 'io': None}
            try:
                row['wchan'] = text(item / 'wchan', 256).strip()
            except (OSError, ValueError):
                pass
            try:
                row['io'] = {k: int(v) for line in text(item / 'io', 8192).splitlines()
                             for k, v in [line.split(':', 1)] if k in {'read_bytes', 'write_bytes', 'syscr', 'syscw'}}
            except (OSError, ValueError):
                pass
            result[item.name] = row
        except (OSError, ValueError, IndexError):
            continue  # Process exit races are expected, never invent zero counters.
    return {'values': result, 'scan_truncated': truncated,
            'requested_not_found': sorted(set(names) - {r['comm'] for r in result.values()})}


def inventory():
    result, gaps = {}, []
    for key, fn in {
        'rules_sha256': lambda: hashlib.sha256(command(['auditctl', '-l']).encode()).hexdigest(),
        'auditd_conf_sha256': lambda: hashlib.sha256(AUDIT_CONF.read_bytes()).hexdigest(),
        'auditd_conf': lambda: config_summary(text(AUDIT_CONF)),
        'forwarder_conf_sha256': lambda: hashlib.sha256(text('/etc/ngfw-audit-forwarder/rsyslog.conf').encode()).hexdigest(),
        'auditd_version': lambda: command(['auditd', '-v']).strip()[:160],
        'auditd_active': lambda: command(['systemctl', 'is-active', 'auditd.service']).strip() == 'active',
        'dataplane_active': lambda: command(['systemctl', 'is-active', 'pt-ngfw-core.service']).strip() == 'active',
    }.items():
        try:
            result[key] = fn()
        except (OSError, ValueError, subprocess.SubprocessError):
            result[key] = None
            gaps.append(key)
    result['gaps'] = gaps
    return result


def forwarder_stats():
    path = Path('/run/ngfw-audit-forwarder/metrics.jsonl')
    st = path.stat()
    with path.open('rb') as src:
        src.seek(max(0, st.st_size - 65536))
        lines = src.read(65536).splitlines()
    counters = {}
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict) or row.get('origin') not in {'core.queue', 'core.action', 'imfile'}:
            continue
        name = row.get('name', '')
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9 _.:-]{1,100}', name):
            continue
        counters[name] = {k: v for k, v in row.items() if k in {
            'size', 'enqueued', 'full', 'discarded.full', 'discarded.nf', 'maxqsize',
            'processed', 'failed', 'suspended', 'resumed', 'suspended.duration', 'submitted'}
            and type(v) in (int, float) and v >= 0}
    age = time.time() - st.st_mtime
    return {'age_seconds': age, 'counters': counters if 0 <= age <= 15 else {}, 'stale': not 0 <= age <= 15,
            'oldest_queue_age_seconds': None, 'counter_reset_on_service_restart': True}


def snapshot(options=None):
    options = options or {}
    begin, cpu_begin = time.monotonic(), time.process_time()
    result = {'schema_version': 1, 'wall_time': time.time(), 'monotonic': begin, 'gaps': []}
    collectors = {
        'boot_id': lambda: text(PROC / 'sys/kernel/random/boot_id', 128).strip(),
        'kernel': lambda: text(PROC / 'sys/kernel/osrelease', 256).strip(),
        'clock_ticks': lambda: os.sysconf('SC_CLK_TCK'),
        'page_size': lambda: os.sysconf('SC_PAGE_SIZE'),
        'cpu': lambda: cpu_counters(text(PROC / 'stat')),
        'disks': lambda: disk_counters(text(PROC / 'diskstats')),
        'memory': memory, 'root_fs': lambda: filesystem('/'),
        'temperature_c': sensors, 'frequency': frequencies,
        'processes': lambda: process_counters(options.get('process_names', ['auditd', 'rsyslogd'])),
        'audit': lambda: status(command(['auditctl', '-s'])),
        'audit_fs': lambda: filesystem(AUDIT_LOG.parent),
        'audit_log': lambda: {'inode': AUDIT_LOG.stat().st_ino, 'bytes': AUDIT_LOG.stat().st_size},
        'forwarder': forwarder_stats,
    }
    if options.get('inventory'):
        collectors['inventory'] = inventory
    if options.get('host_only'):
        for name in ('audit', 'audit_fs', 'audit_log', 'inventory', 'forwarder'):
            collectors.pop(name, None)
    for name, fn in collectors.items():
        try:
            result[name] = fn()
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            result[name] = None
            result['gaps'].append(name)
    result['probe_elapsed_seconds'] = time.monotonic() - begin
    result['probe_cpu_seconds'] = time.process_time() - cpu_begin
    return result


if __name__ == '__main__':
    print(json.dumps(snapshot(OPTIONS), allow_nan=False))
