#!/usr/bin/env python3
"""Loopback-only, read-only live UI for existing runner artifacts. Never starts tests."""
import argparse
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit

ASSETS = Path(__file__).with_name('monitor')


def finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def queue_projection(value):
    value = value or {}
    allowed = ('size', 'enqueued', 'full', 'discarded.full', 'discarded.nf', 'maxqsize',
               'processed', 'failed', 'suspended', 'resumed', 'suspended.duration', 'submitted')
    return {'age_seconds': finite(value.get('age_seconds')), 'stale': value.get('stale') is True,
            'counters': {str(name)[:100]: {k: finite(row.get(k)) for k in allowed if k in row}
                         for name, row in (value.get('counters') or {}).items() if isinstance(row, dict)}}


def read_json(path, limit=2097152):
    if path.is_symlink():
        raise ValueError('symlink artifact rejected')
    with path.open('rb') as src:
        value = src.read(limit + 1)
    if len(value) > limit:
        raise ValueError('artifact exceeds display bound')
    return json.loads(value)


def tail_rows(path, limit=4194304, rows=360):
    if path.is_symlink():
        raise ValueError('symlink artifact rejected')
    with path.open('rb') as src:
        src.seek(0, 2)
        size = src.tell()
        src.seek(max(0, size - limit))
        data = src.read(limit)
    lines = data.splitlines()
    if size > limit:
        lines = lines[1:]
    output = deque(maxlen=rows)
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                output.append(row)
        except (ValueError, UnicodeDecodeError):
            pass  # A partially appended final row is not a new measurement.
    return list(output)


def project_sample(row):
    observed = row.get('measurement') or {}
    guest, host = observed.get('guest') or {}, observed.get('host') or {}
    gd, hd = guest.get('derived') or {}, host.get('derived') or {}
    audit = guest.get('audit') or {}
    legacy = row.get('audit') or {}
    cpus = {name: finite(val.get('busy_pct')) for name, val in gd.get('cpu', {}).items() if name != 'cpu'}
    baseline = guest.get('busy_poll_baseline') or {}
    polling = [name for name in baseline.get('verified_cores', [])
               if isinstance(name, str) and re.fullmatch(r'cpu[0-9]{1,3}', name) and name in cpus]
    temperature = {name: finite(value) for name, value in (host.get('temperature_c') or {}).items()}
    disks = {name: {k: finite(v.get(k)) for k in ('await_ms', 'write_bytes_s', 'iops', 'avg_queue_depth')}
             for name, v in gd.get('disks', {}).items()}
    backlog, limit = audit.get('backlog', legacy.get('audit_backlog')), audit.get('backlog_limit', legacy.get('audit_backlog_limit'))
    return {'time': row.get('time'), 'phase': row.get('phase'), 'scenario': row.get('scenario'),
            'guest_cpu_pct': finite(gd.get('cpu', {}).get('cpu', {}).get('busy_pct', legacy.get('cpu_pct'))),
            'host_cpu_pct': finite(hd.get('cpu', {}).get('cpu', {}).get('busy_pct')),
            'guest_cpu_breakdown': {k: finite(gd.get('cpu', {}).get('cpu', {}).get(k + '_pct'))
                                    for k in ('system', 'iowait', 'irq', 'softirq', 'steal')},
            'processes': [{k: value.get(k) for k in ('comm', 'cpu_one_core_pct', 'state', 'wchan')}
                          for value in gd.get('processes', {}).values()],
            'cores': cpus, 'verified_polling_cores': polling,
            'temperature_c': temperature, 'disks': disks,
            'backlog': finite(backlog), 'backlog_pct': 100 * backlog / limit if finite(backlog) is not None and finite(limit) and limit > 0 else None,
            'lost': finite(audit.get('lost', legacy.get('audit_lost'))),
            'audit_wait_delta': finite(gd.get('audit_wait_delta')),
            'audit_bytes_s': finite(gd.get('log_bytes_per_second', legacy.get('log_bytes_per_second'))),
            'rotation': bool(gd.get('rotation')),
            'forwarder': queue_projection(guest.get('forwarder')),
            'collector': {'counters': [{'id': str(v.get('id', ''))[:100], 'counter': str(v.get('counter', ''))[:100],
                                        'value': finite(v.get('value'))}
                                       for v in (row.get('collector') or {}).get('counters', [])[:100]]},
            'endpoint_rx_bps': finite(row.get('endpoint_rx_bps')),
            'service_probe_ms': finite(row.get('service_probe_ms')),
            'collection_seconds': finite(row.get('collection_seconds')),
            'unavailable': [str(x)[:160] for x in (row.get('unavailable', []) +
                ['guest: ' + str(g) for g in guest.get('gaps', [])] +
                ['host: ' + str(g) for g in host.get('gaps', [])])][:32],
            'checks': {k: row.get(k) is True for k in ('dataplane_ok', 'management_ok', 'mngt_ok', 'vms_ok')}}


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()

    def directory(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise ValueError('invalid run ID')
        path = self.root / name
        if path.is_symlink() or path.resolve().parent != self.root:
            raise ValueError('run outside report root')
        return path

    def runs(self):
        runs = []
        if not self.root.exists():
            return runs
        for path in sorted(self.root.iterdir(), reverse=True):
            if len(runs) >= 100:
                break
            if not path.is_dir() or path.is_symlink():
                continue
            try:
                item = read_json(self.directory(path.name) / 'summary.json')
                runs.append({'id': path.name, 'status': item.get('status'),
                             'profile': item.get('audit_profile'), 'started_at': item.get('started_at')})
            except (OSError, ValueError):
                continue
        return runs

    def run(self, name):
        directory = self.directory(name)
        summary = read_json(directory / 'summary.json')
        try:
            rows = [project_sample(r) for r in tail_rows(directory / 'metrics.ndjson')]
        except FileNotFoundError:
            rows = []
        age = None
        if rows and rows[-1].get('time'):
            try:
                age = time.time() - datetime.fromisoformat(rows[-1]['time'].replace('Z', '+00:00')).timestamp()
            except (ValueError, TypeError):
                pass
        current = summary.get('current') or {}
        context = summary.get('context') or {}
        results = []
        for r in summary.get('results', [])[-32:]:
            m = r.get('metrics') or {}
            results.append({'scenario': r.get('scenario'), 'phase': r.get('phase'),
                            'repetition': r.get('repetition'), 'kind': r.get('kind'),
                            'metrics': {k: finite(m.get(k)) for k in (
                                'received_bps', 'requests_per_second', 'errors', 'loss_pct',
                                'jitter_ms')},
                            'latency_bucket_upper_ms': {k: finite((m.get('latency_ms_bucket_upper_bounds') or {}).get(k))
                                                        for k in ('p50', 'p95', 'p99')}})
        cfg = summary.get('config') or {}
        return {'id': name, 'status': summary.get('status'), 'profile': summary.get('audit_profile'),
                'thresholds': {k: (summary.get('measurement_config') or {}).get(k)
                               for k in ('single_core_pct', 'temperature_start_c', 'temperature_stop_c')},
                'started_at': summary.get('started_at'), 'finished_at': summary.get('finished_at'),
                'context': {k: context.get(k) for k in ('campaign_id', 'test_id', 'comparison_id', 'workload_id')},
                'current': {k: current.get(k) for k in ('scenario', 'repetition', 'phase', 'phase_started_at', 'phase_duration_seconds')},
                'offered': {k: (current.get('offered') or {}).get(k) for k in ('kind', 'mbps', 'rate', 'concurrency', 'parallel')},
                'reason': str(summary.get('reason') or '')[:512],
                'gaps': [str(gap)[:256] for gap in summary.get('gaps', [])][:32],
                'results': results, 'samples': rows, 'sample_age_seconds': age,
                'stale': age is None or age < -5 or age > max(20, 3 * cfg.get('sample_interval', 5)),
                'generated_at': datetime.now(timezone.utc).isoformat(),
                'boundary': 'Observation only. Hypothesis verdict requires paired comparisons and audit acceptance.'}


def handler(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            # Literal loopback host + same-origin browser requests prevent DNS rebinding.
            host = self.headers.get('Host', '')
            if not re.fullmatch(r'(?:127\.0\.0\.1|localhost)(?::[0-9]+)?', host):
                return self.send_error(403)
            if self.headers.get('Sec-Fetch-Site') not in (None, 'same-origin', 'none'):
                return self.send_error(403)
            origin = self.headers.get('Origin')
            if origin and origin != 'http://' + host:
                return self.send_error(403)
            route = urlsplit(self.path)
            try:
                if route.query or route.fragment:
                    return self.send_error(400)
                if route.path == '/api/runs':
                    body = json.dumps(store.runs(), allow_nan=False).encode()
                    mime = 'application/json'
                elif route.path.startswith('/api/run/'):
                    body = json.dumps(store.run(route.path[9:]), allow_nan=False).encode()
                    mime = 'application/json'
                elif route.path in ('/', '/monitor.js', '/monitor.css'):
                    filename = 'index.html' if route.path == '/' else route.path[1:]
                    body = (ASSETS / filename).read_bytes()
                    mime = {'index.html': 'text/html', 'monitor.js': 'text/javascript', 'monitor.css': 'text/css'}[filename]
                else:
                    return self.send_error(404)
            except (OSError, ValueError, TypeError):
                return self.send_error(404, 'Run unavailable or incomplete')
            self.send_response(200)
            self.send_header('Content-Type', mime + '; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reports', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8787)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('port must be 1024..65535')
    server = ThreadingHTTPServer(('127.0.0.1', args.port), handler(Store(args.reports)))
    server.daemon_threads = True
    print(f'Read-only test monitor: http://127.0.0.1:{args.port}/', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
