"""Create unmistakably synthetic, completed UI preview data. Never contacts the lab."""
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys


def create(root):
    directory = Path(root) / 'SYNTHETIC-UI-PREVIEW'
    directory.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    rows = []
    for i in range(100):
        cpu = 40 + 8 * math.sin(i / 9)
        cores = {'cpu': {'busy_pct': cpu, 'iowait_pct': 2, 'system_pct': 13, 'irq_pct': .1, 'softirq_pct': 5, 'steal_pct': 0}}
        cores.update({'cpu' + str(n): {'busy_pct': cpu + n * 2 - 7} for n in range(8)})
        guest = {'audit': {'backlog': 30 + 10 * (i % 8), 'backlog_limit': 8192, 'lost': 0},
                 'derived': {'cpu': cores, 'audit_wait_delta': 0, 'log_bytes_per_second': 81920,
                             'disks': {'vda1': {'await_ms': 3.2, 'write_bytes_s': 131072, 'iops': 32, 'avg_queue_depth': .1}},
                             'processes': {'42': {'comm': 'auditd', 'cpu_one_core_pct': 12, 'state': 'S', 'wchan': 'do_poll'}}},
                 'forwarder': {'age_seconds': 2, 'counters': {'action-1-builtin:omfwd queue': {'size': 0, 'full': 0, 'discarded.full': 0}}}}
        rows.append({'time': (now - timedelta(seconds=5 * (99-i))).isoformat(), 'phase': 'measure',
                     'scenario': 'tcp-example', 'endpoint_rx_bps': (38 + math.sin(i/7))*1e6,
                     'service_probe_ms': 210 + 18*math.sin(i/4), 'collection_seconds': 1.2,
                     'measurement': {'guest': guest, 'host': {'temperature_c': {'DEMO/package': 55}, 'derived': {'cpu': {'cpu': {'busy_pct': 30}}}}},
                     'collector': {'counters': [{'id': 'd_auditd', 'counter': 'processed', 'value': 10000+i*500}]}})
    summary = {'schema_version': 1, 'status': 'completed', 'audit_profile': 'B-DEMO',
               'started_at': rows[0]['time'], 'finished_at': now.isoformat(),
               'context': {'test_id': 'SYNTHETIC', 'campaign_id': 'UI-PREVIEW'},
               'current': {'scenario': 'Демонстрация интерфейса', 'phase': 'measure', 'repetition': 1, 'offered': {'mbps': 40}},
               'reason': 'СИНТЕТИЧЕСКИЙ ПРИМЕР — не измерения NGFW, сервер не использовался.',
               'measurement_config': {'single_core_pct': 90}, 'config': {'sample_interval': 5},
               'gaps': ['Все значения созданы только для проверки интерфейса. Выводов по тезисам нет.'],
               'results': [{'scenario': 'tcp-example', 'phase': 'measure', 'repetition': 1, 'kind': 'tcp', 'metrics': {'received_bps': 38000000}},
                           {'scenario': 'http-example', 'phase': 'measure', 'repetition': 1, 'kind': 'http',
                            'metrics': {'requests_per_second': 98, 'errors': 0, 'latency_ms_bucket_upper_bounds': {'p50': 2, 'p95': 5, 'p99': 10}}}]}
    (directory / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False), encoding='utf-8')
    (directory / 'metrics.ndjson').write_text('\n'.join(map(json.dumps, rows)) + '\n', encoding='utf-8')
    return directory


if __name__ == '__main__':
    print(create(sys.argv[1]))
