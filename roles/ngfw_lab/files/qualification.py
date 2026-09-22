"""Qualification window policy. No workload launch or appliance mutation."""
import json
import math


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def classify(metrics, repeated=False):
    """Safety takes priority over invalidity; a low offer never hides severe loss."""
    if metrics.get('successful_requests') == 0 or metrics.get('received_bps') == 0:
        return 'GLOBAL_STOP', 'zero successful operations'
    error = metrics.get('error_rate_pct', 0)
    if error >= 1 or metrics.get('loss_pct', 0) >= 1:
        return 'GLOBAL_STOP', 'application error or UDP loss at least 1%'
    if metrics.get('generator_limited'):
        return 'INVALID', 'GENERATOR_LIMITED: ' + str(metrics.get('generator_limit_reason'))
    if metrics.get('invalid_reasons'):
        return 'INVALID', '; '.join(metrics['invalid_reasons'])
    if metrics.get('loss_pct', 0) > .5:
        return 'SCENARIO_STOP', 'UDP loss exceeds 0.5%'
    if error >= .1 or (error > 0 and repeated):
        return 'SCENARIO_STOP', 'application errors at least 0.1% or recurring'
    if error > 0:
        return 'WARN', 'application errors below 0.1%; repeat this step once'
    return 'PASS', None


def resources(samples, topology):
    """Docker CPU is percent of one CPU; compare to each endpoint's quota."""
    result, gaps, limited = {}, [], []
    for endpoint in topology.get('endpoints', []):
        service = endpoint['service']
        rows = []
        for sample in samples:
            containers = sample.get('container_resources') or {}
            row = containers.get(service)
            if row:
                rows.append(row)
        quota = endpoint.get('cpus', 0) / 1e9
        if len(rows) < 3 or len(rows) < .8 * len(samples) or quota <= 0:
            gaps.append(service + ' resource coverage/quota unavailable')
        busy = [r['cpu_pct'] >= quota * 95 for r in rows] if quota > 0 else []
        saturated = len(busy) >= 3 and sum(busy) / len(busy) >= .5
        if saturated:
            limited.append(service + ' sustained allocated CPU saturation')
        result[service] = {'samples': len(rows), 'cpu_quota': quota,
            'cpu_peak_pct_one_core': max((r['cpu_pct'] for r in rows), default=None),
            'memory_peak_bytes': max((r['memory_bytes'] for r in rows), default=None),
            'saturated': saturated}
    if set(result) != {'traffic-client', 'traffic-server'}:
        gaps.append('both generator endpoint resources required')
    host_busy = []
    for sample in samples:
        row = ((sample.get('measurement') or {}).get('host') or {}).get('derived', {})
        cpu = row.get('cpu', {}).get('cpu', {})
        if finite(cpu.get('busy_pct')):
            host_busy.append(cpu['busy_pct'] >= 95)
    if len(host_busy) >= 3 and sum(host_busy) / len(host_busy) >= .5:
        limited.append('shared host sustained CPU saturation; generator/VM contention')
    if any(s.get('measurement') for s in samples) and (len(host_busy) < 3 or len(host_busy) < .8 * max(1, len(samples)-1)):
        gaps.append('shared host resource coverage unavailable')
    return result, gaps, limited


def docker_resources(raw, endpoints):
    units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
             'kB': 1000, 'MB': 1000**2, 'GB': 1000**3}
    import re
    output = {}
    for line in raw.splitlines():
        row = json.loads(line)
        service = next((e['service'] for e in endpoints
                        if row.get('ID') and e.get('container_id', '').startswith(row['ID'])), None)
        if not service:
            continue
        memory = row['MemUsage'].split('/')[0].strip()
        match = re.fullmatch(r'([0-9.]+)\s*([A-Za-z]+)', memory)
        cpu = float(row['CPUPerc'].rstrip('%'))
        if not match or match[2] not in units or not finite(cpu):
            raise ValueError('unrecognized Docker resource counters')
        output[service] = {'cpu_pct': cpu, 'memory_bytes': float(match[1]) * units[match[2]]}
    return output
