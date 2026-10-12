#!/usr/bin/env python3
"""Read-only appliance discovery. Never emit credential/config contents."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time


def command(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=12)
        raw = p.stdout.strip()
        return {"returncode": p.returncode, "stdout": raw[:32768],
                "stdout_truncated": len(raw) > 32768,
                "stdout_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                "nonempty_lines": len([x for x in raw.splitlines() if x and x != 'No rules'])}
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"unavailable": type(e).__name__}


def fingerprint(path):
    try:
        data = Path(path).read_bytes()
        return {"path": str(path), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    except OSError as e:
        return {"path": str(path), "unavailable": type(e).__name__}


def config_summary(text):
    """Allowlist properties only; outputs/URLs/commands/headers never emitted."""
    import tomllib
    try:
        cfg = tomllib.loads(text)
    except (ValueError, TypeError):
        return {"unavailable": "TOML_PARSE_FAILED"}
    agent = cfg.get("agent", {})
    names = ("interval", "round_interval", "metric_batch_size", "metric_buffer_limit",
             "collection_jitter", "flush_interval", "flush_jitter", "precision",
             "buffer_strategy", "buffer_directory")
    safe = {k: agent[k] for k in names if k in agent and k != "buffer_directory"}
    if "buffer_directory" in agent:
        safe["buffer_directory_configured"] = True
    inputs = []
    for name, instances in cfg.get("inputs", {}).items():
        for i, instance in enumerate(instances if isinstance(instances, list) else [instances]):
            inputs.append({"plugin": name, "instance": i,
                           "interval": instance.get("interval", agent.get("interval")),
                           "configured_keys": sorted(instance),
                           "command_count": len(instance.get("commands", []))})
    return {"agent": safe, "inputs": inputs, "outputs": sorted(cfg.get("outputs", {})),
            "processors": sorted(cfg.get("processors", {})),
            "aggregators": sorted(cfg.get("aggregators", {})),
            "native_metric_names": None,
            "limitation": "Config metadata only; live emitted fields and dashboard formulas not observed"}


def discover():
    config = Path('/etc/telegraf/telegraf.conf')
    configs = [config] + sorted(Path('/etc/telegraf/telegraf.d').glob('*.conf'))
    result = {"schema_version": 1, "captured_at_epoch": time.time(),
              "evidence_class": "live_read_only_discovery", "configs": []}
    for path in configs:
        row = fingerprint(path)
        if 'unavailable' not in row:
            try:
                row['summary'] = config_summary(path.read_text())
            except (OSError, UnicodeError, ImportError) as e:
                row['summary'] = {"unavailable": type(e).__name__}
        result['configs'].append(row)
    result['telegraf_version'] = command(['telegraf', '--version'])
    help_result = command(['telegraf', '--help'])
    result['disk_buffer_help_mention'] = bool(re.search(r'disk.?buffer|buffer.?strategy',
                                                        help_result.get('stdout', ''), re.I))
    result['disk_buffer_guarantee'] = 'NOT_ESTABLISHED: help absence is not proof; version and outage test required'
    result['packages'] = command(['dpkg-query', '-W', '-f=${binary:Package}\t${Version}\t${db:Status-Status}\n',
                                  'telegraf', 'vxagent', 'auditd', 'pt-ngfw*'])
    result['services'] = {name: command(['systemctl', 'is-active', name])
                          for name in ('telegraf', 'pt-ngfw-core', 'vxagent', 'auditd', 'ngfw-audit-forwarder')}
    result['audit_status'] = command(['auditctl', '-s'])
    rules = command(['auditctl', '-l'])
    if rules.get('returncode') == 0:
        result['audit_rules'] = {'count': rules['nonempty_lines'],
                                 'sha256': rules['stdout_sha256'],
                                 'hash_scope': 'full auditctl -l stdout stripped, never truncated'}
    else:
        result['audit_rules'] = {'unavailable': 'auditctl_failed'}
    result['config_hashes'] = [fingerprint(p) for p in
        ('/opt/pt-ngfw/ngfw-core/etc/config/pt-ngfw.conf', '/etc/audit/audit.rules',
         '/etc/audit/auditd.conf')]
    result['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    result['meminfo'] = Path('/proc/meminfo').read_text()
    result['os_cpu_raw'] = Path('/proc/stat').read_text()
    result['cpu_scope'] = 'OS counters only; not dataplane useful load'
    result['disk'] = command(['df', '-B1', '/var/log', '/'])
    return result


if __name__ == '__main__':
    print(json.dumps(discover(), ensure_ascii=False, indent=2))
