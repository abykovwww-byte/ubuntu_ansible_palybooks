"""Supervise packaged syslog-ng and bounded log rotation; never change a source."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

CONFIG = Path('/etc/collector.json')
RUNTIME = Path('/run')
STATE = Path('/var/lib/syslog-ng')
DATA = Path('/data')
CONTROL = str(RUNTIME / 'syslog-ng.ctl')
ROTATION_INTERVAL = 30


def validate(config):
    def address(value):
        ip = ipaddress.IPv4Address(value)
        if not ip.is_private or ip.is_unspecified or ip.is_multicast:
            raise ValueError('only explicit private IPv4 addresses are allowed')
        return str(ip)

    config = dict(config)
    config['listen_address'] = address(config['listen_address'])
    for key in ('audit_port', 'ngfw_port'):
        if type(config[key]) is not int or not 1024 <= config[key] <= 65535:
            raise ValueError('listener ports must be integers in 1024..65535')
    if config['audit_port'] == config['ngfw_port']:
        raise ValueError('streams must use different ports')
    for key in ('audit_sources', 'ngfw_sources'):
        sources = config[key]
        if not isinstance(sources, list) or not 1 <= len(sources) <= 16:
            raise ValueError('each stream needs 1..16 explicit source IPs')
        config[key] = sorted({address(ip) for ip in sources})
    for key, high in [('rotate_mib', 1024), ('rotate_count', 30)]:
        if type(config[key]) is not int or not 1 <= config[key] <= high:
            raise ValueError('invalid retention bound: ' + key)
    return config


def syslog_config(config):
    config = validate(config)
    lines = ['@version: 3.38', '@include "scl.conf"', '''options {
    use-dns(no); dns-cache(no); keep-hostname(no); chain-hostnames(no);
    log-msg-size(65536); log-fifo-size(2048); flush-lines(1);
    time-reopen(5); stats-freq(0); stats-level(1); threaded(yes);
};''']
    for stream, key in [('auditd', 'audit'), ('ngfw', 'ngfw')]:
        ip, port = config['listen_address'], config[key + '_port']
        allowed = ' or '.join('netmask("' + value + '/32")' for value in config[key + '_sources'])
        lines.append(f'''source s_{stream} {{
    network(ip("{ip}") port({port}) transport("tcp") flags(no-parse)
            max-connections(8) log-iw-size(1024));
    network(ip("{ip}") port({port}) transport("udp") flags(no-parse));
}};
filter f_{stream} {{ {allowed}; }};
destination d_{stream} {{
    file("/data/{stream}/events.jsonl" create-dirs(yes) dir-perm(0700) perm(0600)
         template("$(format-json --scope none stream={stream} received_at=$R_ISODATE source_ip=$SOURCEIP raw=$MESSAGE)\\n"));
}};
log {{ source(s_{stream}); filter(f_{stream}); destination(d_{stream}); }};''')
    return '\n'.join(lines) + '\n'


def rotation_config(config):
    config = validate(config)
    return f'''/data/auditd/events.jsonl /data/ngfw/events.jsonl {{
    daily
    maxsize {config['rotate_mib']}M
    rotate {config['rotate_count']}
    missingok
    notifempty
    compress
    delaycompress
    create 0600 collector collector
    sharedscripts
    postrotate
        /usr/sbin/syslog-ng-ctl reload --control={CONTROL} >/dev/null
    endscript
}}
'''


def ctl(*args):
    return subprocess.run(['/usr/sbin/syslog-ng-ctl', *args, '--control=' + CONTROL],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3).returncode


def health():
    # Fresh rotation heartbeat, live daemon and writable output are all required.
    age = time.time() - (RUNTIME / 'rotation-ok').stat().st_mtime
    if not 0 <= age <= 90 or ctl('stats'):
        return 1
    for stream in ('auditd', 'ngfw'):
        probe = DATA / stream / '.health'
        with probe.open('w', encoding='utf-8') as out:
            out.write('ok\n')
            out.flush()
            os.fsync(out.fileno())
        probe.unlink()
    return 0


def find_audit_receipt(marker):
    """Return only receipt metadata for our exact probe, never raw audit data."""
    if not re.fullmatch(r'NGFW_AUDIT_FORWARDER_[0-9]{10,20}', marker):
        raise ValueError('invalid audit probe marker')
    for name in ('events.jsonl', 'events.jsonl.1'):
        try:
            with (DATA / 'auditd' / name).open('rb') as log:
                log.seek(max(0, log.seek(0, 2) - 262144))
                tail = log.read(262144)
        except FileNotFoundError:
            continue
        for line in tail.splitlines():
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            raw = row.get('raw', '')
            if row.get('stream') != 'auditd' or row.get('source_ip') != '10.77.0.20' or not isinstance(raw, str):
                continue
            match = re.search(r'\btype=USER\s+msg=audit\(([0-9.]+:[0-9]+)\)', raw)
            if match and re.search(r'(?<![A-Za-z0-9_])' + re.escape(marker) + r'(?![A-Za-z0-9_])', raw):
                return {'stream': 'auditd', 'source_ip': '10.77.0.20',
                        'audit_id': match.group(1), 'marker': marker}
    return None


def serve(config):
    os.umask(0o077)
    for stream in ('auditd', 'ngfw'):
        (DATA / stream).mkdir(mode=0o700, exist_ok=True)
    (RUNTIME / 'syslog-ng.conf').write_text(syslog_config(config), encoding='utf-8')
    (RUNTIME / 'logrotate.conf').write_text(rotation_config(config), encoding='utf-8')
    argv = ['/usr/sbin/syslog-ng', '--foreground', '--stderr', '--no-caps',
            '--cfgfile=' + str(RUNTIME / 'syslog-ng.conf'),
            '--persist-file=' + str(STATE / 'syslog-ng.persist'),
            '--pidfile=' + str(RUNTIME / 'syslog-ng.pid'), '--control=' + CONTROL]
    subprocess.run(argv + ['--syntax-only'], check=True, timeout=10)
    child = subprocess.Popen(argv)
    stopped = False

    def stop(_signum, _frame):
        nonlocal stopped
        stopped = True

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    try:
        deadline = time.monotonic() + 10
        while ctl('stats'):
            if stopped or child.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError('syslog-ng did not become ready')
            time.sleep(.2)
        next_rotation = 0
        while not stopped:
            if child.poll() is not None:
                raise RuntimeError('syslog-ng exited unexpectedly')
            if time.monotonic() >= next_rotation:
                subprocess.run(['/usr/sbin/logrotate', '--state', str(STATE / 'logrotate.status'),
                                str(RUNTIME / 'logrotate.conf')], check=True, timeout=20)
                (RUNTIME / 'rotation-ok').touch()
                next_rotation = time.monotonic() + ROTATION_INTERVAL
            time.sleep(.2)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['serve', 'health', 'validate', 'audit-receipt'])
    parser.add_argument('--marker')
    args = parser.parse_args()
    if args.action == 'audit-receipt':
        if args.marker is None:
            parser.error('--marker is required for audit-receipt')
        deadline = time.monotonic() + 20
        while True:
            receipt = find_audit_receipt(args.marker)
            if receipt:
                print(json.dumps(receipt))
                return 0
            if time.monotonic() >= deadline:
                print('AuditD probe was not found in the bounded recent log window')
                return 1
            time.sleep(.5)
    if args.action == 'health':
        try:
            return health()
        except (OSError, subprocess.SubprocessError):
            return 1
    config = validate(json.loads(CONFIG.read_text(encoding='utf-8')))
    if args.action == 'validate':
        print('Collector settings valid')
        return 0
    return serve(config)


if __name__ == '__main__':
    raise SystemExit(main())
