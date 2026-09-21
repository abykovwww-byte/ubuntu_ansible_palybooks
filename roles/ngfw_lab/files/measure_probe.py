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
DEBIAN_AUDITD_VERSION = '1:3.0.9-1'
DEBIAN_AUDITD_DEFAULT_SOURCE = {
    'source': 'https://deb.debian.org/debian/pool/main/a/audit/audit_3.0.9-1.dsc',
    'source_member': 'audit-3.0.9/src/auditd-config.c:351',
    'orig_sha256': 'fd9570444df1573a274ca8ba23590082298a083cfc0618138957f590e845bc78',
    'debian_sha256': 'b80d2685b79a617098a3389f41356ffd77d8d62d59bee03b189e31dd9b81580e',
}


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


def auditd_package():
    # auditd 3.0.9 has no -v option. Query the installed package, never start a
    # second daemon or mistake auditctl's version for the daemon package version.
    rows = command(['dpkg-query', '-W', '-f=${Package}\n${Version}\n${Status}\n',
                    'auditd']).splitlines()
    if (len(rows) != 3 or rows[0] != 'auditd' or rows[2] != 'install ok installed'
            or not re.fullmatch(r'[A-Za-z0-9.+:~_-]{1,160}', rows[1])):
        raise ValueError('installed auditd package identity unavailable')
    return {'name': rows[0], 'version': rows[1], 'status': rows[2], 'origin': 'dpkg-query'}


def effective_defaults(config, package):
    """Only the exact source-reviewed Debian package; never fill configured data."""
    if (package != {'name': 'auditd', 'version': DEBIAN_AUDITD_VERSION,
                    'status': 'install ok installed', 'origin': 'dpkg-query'}
            or 'overflow_action' in config):
        return {}
    # The Debian orig/patch archive SHA256 values were checked against its DSC;
    # none of that revision's four patches changes this built-in default.
    return {'overflow_action': {'value': 'syslog', 'origin': 'verified-package-default',
                               'package_name': 'auditd', 'package_version': DEBIAN_AUDITD_VERSION,
                               **DEBIAN_AUDITD_DEFAULT_SOURCE}}


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


def validate_process_boot_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value):
        raise ValueError('guest_process_boot_id requires the actual guest boot UUID')
    return value


def validate_process_targets(targets, names):
    """Exact prior-inventory identities, not arbitrary /proc paths or queries."""
    if not isinstance(targets, list) or not 1 <= len(targets) <= 64:
        raise ValueError('guest_process_targets requires 1..64 explicit identities')
    if (not isinstance(names, list) or not names or len(names) > 64
            or not all(isinstance(name, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', name)
                       for name in names)):
        raise ValueError('guest_process_targets requires exact selected process_names')
    seen = set()
    for row in targets:
        if not isinstance(row, dict) or set(row) != {'comm', 'pid', 'start_ticks'}:
            raise ValueError('guest_process_targets requires exact comm/pid/start_ticks fields')
        if (not isinstance(row['comm'], str) or row['comm'] not in names
                or type(row['pid']) is not int or not 0 < row['pid'] <= 2147483647
                or type(row['start_ticks']) is not int or not 0 < row['start_ticks'] <= 2**63 - 1):
            raise ValueError('invalid guest_process_targets identity')
        if row['pid'] in seen:
            raise ValueError('duplicate guest_process_targets PID')
        seen.add(row['pid'])
    if {row['comm'] for row in targets} != set(names):
        raise ValueError('guest_process_targets must cover every selected process name')
    return targets


def process_details(item, row):
    """Optional I/O and wait counters; never read argv, environment or raw logs."""
    row.update(wchan=None, io=None)
    try:
        row['wchan'] = text(item / 'wchan', 256).strip()
    except (OSError, ValueError):
        pass
    try:
        row['io'] = {k: int(v) for line in text(item / 'io', 8192).splitlines()
                     for k, v in [line.split(':', 1)] if k in {'read_bytes', 'write_bytes', 'syscr', 'syscw'}}
    except (OSError, ValueError):
        pass
    return row


def selected_process_counters(names, targets):
    """Never enumerate /proc in selected mode, including on missing/reused PIDs."""
    validate_process_targets(targets, names)
    result, unavailable = {}, []
    for expected in targets:
        pid = expected['pid']
        item = PROC / str(pid)
        try:
            before = task_stat(item / 'stat', pid)
            before_comm = text(item / 'comm', 128).strip()
            row = process_details(item, dict(before))
            after = task_stat(item / 'stat', pid)
            after_comm = text(item / 'comm', 128).strip()
            if (before_comm != expected['comm'] or after_comm != expected['comm']
                    or any(value[field] != expected[field] for value in (before, after)
                           for field in ('comm', 'start_ticks'))):
                raise ValueError('selected process identity changed')
            result[str(pid)] = row
        except (OSError, ValueError, IndexError):
            unavailable.append(str(pid))
    return {'values': result, 'scan_truncated': False, 'mode': 'selected',
            'unavailable': unavailable,
            'requested_not_found': sorted(set(names) - {row['comm'] for row in result.values()})}


def process_counters(names, targets=None):
    if targets is not None:
        return selected_process_counters(names, targets)
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
                   'rss_pages': int(fields[21])}
            result[item.name] = process_details(item, row)
        except (OSError, ValueError, IndexError):
            continue  # Process exit races are expected, never invent zero counters.
    return {'values': result, 'scan_truncated': truncated,
            'requested_not_found': sorted(set(names) - {r['comm'] for r in result.values()})}


def task_stat(path, expected_id):
    raw = text(path, 8192)
    prefix, sep, rest = raw.partition('(')
    comm, close, fields = rest.rpartition(')')
    if not sep or not close or int(prefix.strip()) != expected_id:
        raise ValueError('task stat identity unavailable')
    fields = fields.split()
    return {'comm': comm, 'state': fields[0], 'start_ticks': int(fields[19]),
            'user_ticks': int(fields[11]), 'system_ticks': int(fields[12]), 'rss_pages': int(fields[21])}


def selected_thread_counters(targets):
    """Read at most 16 explicit PID/TID pairs; never walk every process thread."""
    if not isinstance(targets, list) or not 1 <= len(targets) <= 16:
        raise ValueError('invalid selected thread targets')
    result, unavailable, seen = {}, [], set()
    for target in targets:
        if (not isinstance(target, dict) or set(target) != {'pid', 'tid'}
                or any(type(target[k]) is not int or not 0 < target[k] <= 2147483647
                       for k in ('pid', 'tid'))):
            raise ValueError('invalid selected thread identity')
        pid, tid = target['pid'], target['tid']
        key = f'{pid}:{tid}'
        if key in seen:
            raise ValueError('duplicate selected thread')
        seen.add(key)
        process_path, thread_path = PROC / str(pid), PROC / str(pid) / 'task' / str(tid)
        try:
            before_process = task_stat(process_path / 'stat', pid)
            before_thread = task_stat(thread_path / 'stat', tid)
            affinity = sorted(os.sched_getaffinity(tid))
            if not affinity or len(affinity) > 512:
                raise ValueError('selected thread affinity unavailable or outside read bound')
            process = task_stat(process_path / 'stat', pid)
            thread = task_stat(thread_path / 'stat', tid)
            if any(old[k] != new[k] for old, new in ((before_process, process), (before_thread, thread))
                   for k in ('comm', 'start_ticks')):
                raise ValueError('task identity changed during collection')
            result[key] = {'pid': pid, 'tid': tid, 'process_comm': process['comm'],
                           'process_start_ticks': process['start_ticks'], 'process_state': process['state'],
                           'thread_comm': thread['comm'], 'thread_start_ticks': thread['start_ticks'],
                           'thread_state': thread['state'], 'affinity': affinity,
                           'user_ticks': thread['user_ticks'], 'system_ticks': thread['system_ticks']}
        except (OSError, ValueError, IndexError, AttributeError):
            unavailable.append(key)  # Missing/racing task must block the specific exception.
    return {'values': result, 'unavailable': unavailable}


def inventory():
    result, gaps = {}, []
    for key, fn in {
        'rules_sha256': lambda: hashlib.sha256(command(['auditctl', '-l']).encode()).hexdigest(),
        'auditd_conf_sha256': lambda: hashlib.sha256(AUDIT_CONF.read_bytes()).hexdigest(),
        'auditd_conf': lambda: config_summary(text(AUDIT_CONF)),
        'forwarder_conf_sha256': lambda: hashlib.sha256(text('/etc/ngfw-audit-forwarder/rsyslog.conf').encode()).hexdigest(),
        'auditd_package': auditd_package,
        'auditd_active': lambda: command(['systemctl', 'is-active', 'auditd.service']).strip() == 'active',
        'dataplane_active': lambda: command(['systemctl', 'is-active', 'pt-ngfw-core.service']).strip() == 'active',
    }.items():
        try:
            result[key] = fn()
        except (OSError, ValueError, subprocess.SubprocessError):
            result[key] = None
            gaps.append(key)
    package = result['auditd_package'] or {}
    result['auditd_version'] = package.get('version')
    if result['auditd_version'] is None:
        gaps.append('auditd_version')
    result['effective_defaults'] = effective_defaults(result['auditd_conf'] or {}, package)
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
    result = {'schema_version': 1, 'wall_time': time.time(), 'monotonic': begin,
              'probe_pid': os.getpid(), 'gaps': []}
    def processes():
        targets = options.get('process_targets')
        if targets is not None:
            expected_boot = validate_process_boot_id(options.get('process_boot_id'))
            if result.get('boot_id') != expected_boot:
                raise ValueError('selected process boot changed; new baseline required')
        return process_counters(options.get('process_names', ['auditd', 'rsyslogd']), targets)
    collectors = {
        'boot_id': lambda: text(PROC / 'sys/kernel/random/boot_id', 128).strip(),
        'kernel': lambda: text(PROC / 'sys/kernel/osrelease', 256).strip(),
        'clock_ticks': lambda: os.sysconf('SC_CLK_TCK'),
        'page_size': lambda: os.sysconf('SC_PAGE_SIZE'),
        'cpu': lambda: cpu_counters(text(PROC / 'stat')),
        'disks': lambda: disk_counters(text(PROC / 'diskstats')),
        'memory': memory, 'root_fs': lambda: filesystem('/'),
        'temperature_c': sensors, 'frequency': frequencies,
        'processes': processes,
        'audit': lambda: status(command(['auditctl', '-s'])),
        'audit_fs': lambda: filesystem(AUDIT_LOG.parent),
        'audit_log': lambda: {'inode': AUDIT_LOG.stat().st_ino, 'bytes': AUDIT_LOG.stat().st_size},
        'forwarder': forwarder_stats,
    }
    if options.get('inventory'):
        collectors['inventory'] = inventory
    if options.get('thread_targets') is not None and not options.get('host_only'):
        collectors['busy_poll_threads'] = lambda: selected_thread_counters(options['thread_targets'])
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
