#!/usr/bin/env python3
"""Repeatable NGFW traffic matrix. No AuditD configuration or VM lifecycle writes."""
import argparse
import concurrent.futures
import csv
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import time
import uuid

import measurement
import measure_probe
import qualification
from traffic.probe_diagnostics import decode_failure, RequestFailure


class Abort(RuntimeError):
    pass


class CommandFailure(Abort):
    def __init__(self, command, returncode, stdout):
        super().__init__(f"{command} check failed (exit {returncode})")
        self.returncode = returncode
        self.request_error = decode_failure(stdout)


class ProbeFailure(Abort):
    def __init__(self, diagnostic):
        super().__init__('dataplane probe failed')
        self.diagnostic = diagnostic


class WindowStop(Abort):
    def __init__(self, outcome, reason):
        super().__init__(reason)
        self.outcome = outcome


def utc():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def validate(config):
    age = config.get('isolation_max_age_seconds', 86400)
    if age is not None and (type(age) is not int or age <= 0):
        raise ValueError('isolation_max_age_seconds must be positive or null with campaign binding')
    if age is None and not config.get('campaign_id'):
        raise ValueError('unexpired isolation requires campaign_id')
    for name, low, high in [("duration", 1, 3600), ("warmup", 0, 3600), ("idle", 0, 3600),
                            ("repetitions", 1, 10), ("sample_interval", 2, 30)]:
        value = config.get(name)
        if type(value) is not int or not low <= value <= high:
            raise ValueError(f"{name} must be an integer in {low}..{high}")
    for name in ("target", "management_address", "mngt_address"):
        if not ipaddress.ip_address(config[name]).is_private:
            raise ValueError(f"{name} must be a private lab address")
    for name in ("management_port", "mngt_port"):
        if not 1 <= config[name] <= 65535:
            raise ValueError("invalid management port")
    if not config["scenarios"] or len(config["scenarios"]) > 32:
        raise ValueError("expected 1..32 scenarios")
    names = set()
    for item in config["scenarios"]:
        name, kind = item["name"], item["kind"]
        if not re.fullmatch(r"[a-z0-9-]{1,40}", name) or name in names:
            raise ValueError("invalid/duplicate scenario name")
        names.add(name)
        if kind not in ("tcp", "udp", "short-tcp", "http", "dns"):
            raise ValueError("unsupported scenario kind")
        if kind in ("tcp", "udp"):
            if not 1 <= item["mbps"] <= 1000 or not 1 <= item.get("parallel", 1) <= 16:
                raise ValueError("iperf load outside lab bounds")
            if kind == "udp" and not 16 <= item.get("length", 1200) <= 1400:
                raise ValueError("UDP payload outside 16..1400 bytes")
        elif not (1 <= item["rate"] <= 10000 and 1 <= item["concurrency"] <= 64):
            raise ValueError("request workload outside lab bounds")
    limits = config["limits"]
    for key in ("backlog_pct", "disk_used_pct", "cpu_pct", "memory_used_pct"):
        if not 0 < limits[key] < 100:
            raise ValueError(f"invalid {key}")
    for key in ("backlog_seconds", "cpu_seconds", "memory_seconds", "log_projection_hours"):
        if not 0 < limits[key] <= 86400:
            raise ValueError(f"invalid {key}")
    for key in ("udp_loss_pct", "tcp_drop_pct"):
        if not 0 <= limits[key] < 100:
            raise ValueError(f"invalid {key}")
    # Reject NaN/Infinity before serializing artifacts or making comparisons.
    json.dumps(config, allow_nan=False)
    return config


def command(config, scenario, seconds):
    kind = scenario["kind"]
    if kind in ("tcp", "udp"):
        parallel = scenario.get("parallel", 1)
        # iperf3's bitrate is per stream. The configuration expresses total load.
        per_stream = int(scenario["mbps"] * 1_000_000 / parallel)
        args = ["iperf3", "-c", config["target"], "-t", str(seconds), "-J",
                "-P", str(parallel), "-b", str(per_stream), "--connect-timeout", "2000"]
        if kind == "udp":
            args += ["-u", "-l", str(scenario.get("length", 1200))]
        return args
    return ["python3", "/opt/traffic/endpoint.py", "client", "--kind", kind,
            "--target", config["target"], "--duration", str(seconds),
            "--rate", str(scenario["rate"]), "--concurrency", str(scenario["concurrency"])]


def parse_probe(text):
    result = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep:
            continue
        if key == "boot_id":
            result[key] = value
        else:
            try:
                number = float(value)
                if math.isfinite(number):
                    result[key] = number
            except ValueError:
                pass
    return result


def cpu_percent(before, after):
    total = after["cpu_total"] - before["cpu_total"]
    idle = after["cpu_idle"] - before["cpu_idle"]
    return 100 * (1 - idle / total) if total > 0 else 0


class Guard:
    def __init__(self, limits, audit_required, cpu_action='stop'):
        self.limits, self.audit_required = limits, audit_required
        self.cpu_action = measurement.validate_cpu_action(cpu_action)
        self.previous = None
        self.since = {}

    def held(self, key, condition, now, duration):
        if not condition:
            self.since.pop(key, None)
            return False
        self.since.setdefault(key, now)
        return now - self.since[key] >= duration

    def check(self, sample):
        now, reasons, limits = sample["monotonic"], [], self.limits
        for check in ("dataplane_ok", "management_ok", "mngt_ok", "vms_ok"):
            hold = limits.get('availability_seconds', 0) if self.previous and check != 'vms_ok' else 0
            if self.held(check, not sample.get(check), now, hold):
                reasons.append(check + " failed")
            elif not sample.get(check):
                sample.setdefault('warnings', []).append(check + ' transient failure; awaiting confirmation')
        host = sample.get("host", {})
        if not host:
            reasons.append("host metrics unavailable")
        else:
            if host["disk_used_pct"] >= limits["disk_used_pct"]:
                reasons.append("host report filesystem full threshold")
            if self.held("host-memory", host["memory_used_pct"] >= limits["memory_used_pct"], now,
                         limits["memory_seconds"]):
                reasons.append("sustained host memory pressure")
        audit = sample.get("audit", {})
        mandatory = {"boot_id", "audit_lost", "audit_backlog", "audit_backlog_limit", "cpu_total",
                     "cpu_idle", "root_used_pct", "audit_disk_used_pct", "memory_used_pct",
                     "log_bytes", "log_inode", "audit_free_bytes"}
        if self.audit_required and (not mandatory <= audit.keys() or audit.get("audit_backlog_limit", 0) <= 0):
            reasons.append("AuditD probe incomplete or unavailable")
        if mandatory <= audit.keys() and audit["audit_backlog_limit"] > 0:
            if audit["audit_lost"] > 0:
                reasons.append("AuditD lost is nonzero")
            if self.held("backlog", 100 * audit["audit_backlog"] / audit["audit_backlog_limit"] >= limits["backlog_pct"],
                         now, limits["backlog_seconds"]):
                reasons.append("sustained AuditD backlog")
            if max(audit["root_used_pct"], audit["audit_disk_used_pct"]) >= limits["disk_used_pct"]:
                reasons.append("appliance filesystem full threshold")
            if self.held("guest-memory", audit["memory_used_pct"] >= limits["memory_used_pct"], now,
                         limits["memory_seconds"]):
                reasons.append("sustained appliance memory pressure")
            previous = self.previous.get("audit", {}) if self.previous else {}
            if mandatory <= previous.keys():
                if audit["boot_id"] != previous["boot_id"]:
                    reasons.append("appliance rebooted")
                else:
                    audit["cpu_pct"] = cpu_percent(previous, audit)
                    if self.held("cpu", audit["cpu_pct"] >= limits["cpu_pct"], now, limits["cpu_seconds"]):
                        measurement.cpu_observation(sample, reasons, self.cpu_action, "sustained appliance CPU")
                    seconds = now - self.previous["monotonic"]
                    delta = audit["log_bytes"] - previous["log_bytes"]
                    if seconds > 0 and audit["log_inode"] == previous["log_inode"] and delta >= 0:
                        audit["log_bytes_per_second"] = delta / seconds
                        risky = delta > 0 and audit["audit_free_bytes"] / (delta / seconds) < limits["log_projection_hours"] * 3600
                        if self.held("log-projection", risky, now, 60):
                            reasons.append("projected audit filesystem exhaustion")
                    else:
                        audit["log_rate_gap"] = "rotation or truncation"
                        self.since.pop("log-projection", None)
                    serial_delta = audit.get("event_serial", -1) - previous.get("event_serial", -1)
                    if "event_serial" in audit and "event_serial" in previous and serial_delta >= 0 and seconds > 0:
                        audit["event_serial_rate_estimate"] = serial_delta / seconds
        self.previous = sample
        return reasons


def result_metrics(scenario, raw):
    if "error" in raw:
        raise WindowStop('INVALID', "iperf3 reported an error")
    if scenario["kind"] in ("tcp", "udp"):
        end = raw.get("end", {})
        # Legacy UDP "sum" mixes sender/receiver fields. Require the explicit receiver.
        received = end.get("sum_received", {})
        sent = end.get("sum_sent", {})
        if "bits_per_second" not in received or received.get("seconds", 0) <= 0:
            raise WindowStop('INVALID', "iperf3 receiver summary missing")
        result = {"received_bps": received["bits_per_second"],
                  "sent_bps": sent.get("bits_per_second"), "retransmits": sent.get("retransmits")}
        offered = scenario.get('mbps', 0) * 1_000_000
        result.update(requested_bps=offered, achieved_bps=received['bits_per_second'],
                      sender_bytes=sent.get('bytes'), receiver_bytes=received.get('bytes'),
                      invalid_reasons=[])
        if not sent.get('bytes') or not received.get('bytes') or not sent.get('seconds'):
            result['invalid_reasons'].append('sender/receiver volume confirmation missing')
        result['generator_limited'] = (not qualification.finite(result['sent_bps']) or
                                       result['sent_bps'] < .95 * offered or result['received_bps'] < .95 * offered)
        result['generator_limit_reason'] = ('sent/received rate below 95%; source/path attribution unresolved'
                                            if result['generator_limited'] else None)
        if scenario["kind"] == "udp":
            for field in ("lost_percent", "packets", "lost_packets", "jitter_ms"):
                if field not in received:
                    raise WindowStop('INVALID', "UDP receiver counters missing")
            # iperf_udp.c: packet_count is highest sequence, not received datagrams.
            delivered = received['packets'] - received['lost_packets']
            if delivered < 0:
                raise WindowStop('INVALID', 'inconsistent UDP receiver counters')
            result.update(loss_pct=received["lost_percent"], jitter_ms=received["jitter_ms"],
                          receiver_sequence_packets=received['packets'], receiver_lost_packets=received['lost_packets'],
                          received_packets=delivered, received_report_pps=delivered / received["seconds"])
        return result
    required = ('attempted_requests', 'successful_requests', 'errors', 'attempt_rate', 'success_rate',
                'requested_rate', 'achieved_rate', 'error_rate_pct', 'skipped_slots', 'scheduler_lag_max_ms')
    if any(not qualification.finite(raw.get(k)) or raw[k] < 0 for k in required):
        raw.setdefault('invalid_reasons', []).append('application generator counters incomplete')
    elif (raw['attempted_requests'] != raw['successful_requests'] + raw['errors'] or
          raw['requested_rate'] != scenario.get('rate', raw['requested_rate'])):
        raw.setdefault('invalid_reasons', []).append('inconsistent application generator counters')
    else:
        raw['error_rate_pct'] = 100 * raw['errors'] / raw['attempted_requests'] if raw['attempted_requests'] else 100.0
        raw['generator_limited'] = raw['attempt_rate'] < .95 * raw['requested_rate']
        if raw['generator_limited']:
            raw['generator_limit_reason'] = 'achieved attempt rate below 95% requested'
    return raw


class Backend:
    def __init__(self, config, probe_argv=None, measurement_argv=None, measurement_config=None):
        self.config, self.probe_argv = config, probe_argv
        self.measurement_argv, self.measurement_config = measurement_argv, measurement_config
        self.compose = ["docker", "compose", "--project-directory", config["project_dir"]]
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.container_ids = []
        self.endpoints = []

    def call(self, argv, timeout=4, input_text=None):
        result = subprocess.run(argv, input=input_text, text=True, capture_output=True, timeout=timeout)
        if result.returncode:
            raise CommandFailure(argv[0], result.returncode, result.stdout)
        return result.stdout

    def endpoint(self, service, args, timeout=4):
        return self.call(self.compose + ["exec", "-T", service] + args, timeout)

    def topology(self):
        details = []
        for service in ("traffic-client", "traffic-server"):
            cid = self.call(self.compose + ["ps", "-q", service]).strip()
            if not cid:
                raise Abort(service + " is not running")
            if cid not in self.container_ids:
                self.container_ids.append(cid)
            entry = json.loads(self.call(["docker", "inspect", cid]))[0]
            if not entry["State"]["Running"]:
                raise Abort(service + " is not running")
            networks = entry["NetworkSettings"]["Networks"]
            if len(networks) != 1:
                raise Abort("endpoint must have exactly one network")
            details.append({"service": service, "image": entry["Image"],
                            "networks": {k: {f: v[f] for f in ("NetworkID", "IPAddress", "Gateway")}
                                         for k, v in networks.items()},
                            "cpus": entry["HostConfig"]["NanoCpus"], "memory": entry["HostConfig"]["Memory"]})
        self.endpoints = [e | {'container_id': cid} for e, cid in zip(details, self.container_ids)]
        network_names = [next(iter(e["networks"])) for e in details]
        if network_names[0] == network_names[1]:
            raise Abort("endpoints share one network")
        networks = json.loads(self.call(["docker", "network", "inspect"] + network_names))
        for network in networks:
            if network["Driver"] != "ipvlan" or network.get("Options", {}).get("ipvlan_mode") != "l2":
                raise Abort("expected ipvlan l2 endpoint networks")
        server_network = next(iter(details[1]["networks"].values()))
        if server_network["IPAddress"] != self.config["target"]:
            raise Abort("target differs from traffic-server address")
        return {"endpoints": details, "networks": [{"name": n["Name"], "id": n["Id"],
                 "options": n["Options"], "ipam": n["IPAM"]} for n in networks],
                "ngfw_xml": self.call(["virsh", "-c", "qemu:///system", "dumpxml", self.config["ngfw_vm"], "--inactive"])}

    def state(self, vm):
        return self.call(["virsh", "-c", "qemu:///system", "domstate", vm]).strip()

    def probe_endpoint(self, service, target):
        started = time.monotonic()
        try:
            self.endpoint(service, ["python3", "/opt/traffic/endpoint.py", "probe", "--target", target])
        except (OSError, ValueError, subprocess.SubprocessError, CommandFailure) as error:
            detail = {'layer': 'command', 'exception_class': ('ValueError' if isinstance(error, ValueError)
                      else 'OSError' if isinstance(error, OSError) else 'SubprocessError'),
                      'errno': RequestFailure('unknown', error).errno}
            if isinstance(error, CommandFailure):
                detail = {'layer': 'command', 'exception_class': 'CommandFailure',
                          'returncode': error.returncode}
                request_error = error.request_error
                if request_error is not None:
                    detail = {'layer': 'request', 'returncode': error.returncode, **request_error}
            elif isinstance(error, subprocess.TimeoutExpired):
                detail = {'layer': 'command', 'exception_class': 'TimeoutExpired'}
            detail['elapsed_ms'] = 1000 * (time.monotonic() - started)
            raise ProbeFailure(detail) from None
        return True

    @staticmethod
    def tcp_ok(address, port):
        with socket.create_connection((address, port), timeout=1):
            return True

    def host(self):
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            memory[key] = int(value.split()[0])
        fs = os.statvfs(self.config["reports_dir"])
        return {"memory_used_pct": 100 * (1 - memory["MemAvailable"] / memory["MemTotal"]),
                "disk_used_pct": 100 * (1 - fs.f_bavail / fs.f_blocks),
                "loadavg": list(os.getloadavg()), "diskstats": Path("/proc/diskstats").read_text(),
                "vmstat": Path("/proc/vmstat").read_text()}

    def audit(self):
        if not self.probe_argv:
            return {}
        script = Path(__file__).with_name("ngfw_probe.sh").read_text(encoding="utf-8")
        return parse_probe(self.call(self.probe_argv, input_text=script))

    def extended_guest(self, inventory=False):
        config = self.measurement_config or {}
        return json.loads(self.call(self.measurement_argv, timeout=12,
                                   input_text=measurement.source(config.get('process_names'), inventory,
                                                                 config.get('guest_busy_poll_baseline'),
                                                                 config.get('guest_process_targets'),
                                                                 config.get('guest_process_boot_id'))))

    def extended_host(self):
        return measure_probe.snapshot({'host_only': True,
                                       'process_names': (self.measurement_config or {}).get('process_names', [])})

    def endpoint_counters(self):
        # Receiver interface bytes include management/probe traffic, not useful throughput.
        raw = self.endpoint('traffic-server', ['python3', '-c',
            "import json,time;from pathlib import Path;print(json.dumps({'monotonic':time.monotonic(),"
            "'rx_bytes':int(Path('/sys/class/net/eth0/statistics/rx_bytes').read_text())}))"])
        return json.loads(raw)

    def sample(self):
        config = self.config
        started = time.monotonic()
        probe_timing = {}

        def dataplane():
            before = time.monotonic()
            result = self.probe_endpoint("traffic-client", config["target"])
            probe_timing['service_probe_ms'] = 1000 * (time.monotonic() - before)
            return result

        checks = {
            "dataplane_ok": dataplane,
            "management_ok": lambda: self.tcp_ok(config["management_address"], config["management_port"]),
            "mngt_ok": lambda: self.tcp_ok(config["mngt_address"], config["mngt_port"]),
            "audit": self.audit, "host": self.host,
            "domstats": lambda: self.call(["virsh", "-c", "qemu:///system", "domstats", config["ngfw_vm"], config["mngt_vm"]]),
            "vms_ok": lambda: self.state(config["ngfw_vm"]) == "running" and self.state(config["mngt_vm"]) == "running",
            "containers": lambda: self.call(["docker", "stats", "--no-stream", "--format", "{{json .}}"] + self.container_ids, timeout=4),
        }
        if self.measurement_argv:
            checks.pop('audit')
            checks.update(extended_guest=self.extended_guest, extended_host=self.extended_host,
                          endpoint_counters=self.endpoint_counters,
                          collector=lambda: json.loads(self.call(['docker', 'compose', '--project-directory',
                              '/srv/apps/ngfw-logs', 'exec', '-T', 'collector', 'python3',
                              '/opt/collector/collector.py', 'metrics'], timeout=5)))
        futures = {key: self.pool.submit(fn) for key, fn in checks.items()}
        sample = {"time": utc(), "monotonic": time.monotonic(), "unavailable": []}
        for key, future in futures.items():
            try:
                sample[key] = future.result()
            except (OSError, ValueError, subprocess.SubprocessError, Abort) as error:
                sample["unavailable"].append(key)
                if isinstance(error, ProbeFailure):
                    sample.setdefault('check_errors', {})[key] = error.diagnostic
        if self.measurement_argv:
            guest = sample.pop('extended_guest', None)
            sample['measurement'] = {'guest': guest, 'host': sample.pop('extended_host', None)}
            sample['audit'] = measurement.legacy_guest(guest) if guest else {}
        try:
            sample['container_resources'] = qualification.docker_resources(sample.get('containers', ''), self.endpoints)
        except (ValueError, KeyError, TypeError):
            sample['unavailable'].append('container_resources')
        sample.update(probe_timing)
        sample['collection_seconds'] = time.monotonic() - started
        return sample

    def isolation(self):
        if self.state(self.config["ngfw_vm"]) != "shut off":
            raise Abort("check-isolation requires NGFW already shut off by the operator")
        self.probe_endpoint("traffic-server", "127.0.0.1")
        # Verify the probe executable on the client before interpreting its failure.
        self.endpoint("traffic-client", ["python3", "/opt/traffic/endpoint.py", "--help"])
        result = subprocess.run(self.compose + ["exec", "-T", "traffic-client", "python3",
                                "/opt/traffic/endpoint.py", "probe", "--target", self.config["target"]],
                                capture_output=True, text=True, timeout=4)
        if result.returncode == 0:
            raise Abort("isolation failed: reachable receiver while NGFW is off")
        if "TimeoutError" not in result.stderr and "OSError" not in result.stderr and "ConnectionRefusedError" not in result.stderr:
            raise Abort("isolation probe failed for an unrecognized reason")
        if self.state(self.config["ngfw_vm"]) != "shut off":
            raise Abort("NGFW changed state during isolation check")
        return {"checked_at": time.time(), "time": utc(), "topology": self.topology(),
                "campaign_id": self.config.get('campaign_id'),
                "receiver_local_health": True, "client_to_receiver": "unreachable", "ngfw_state": "shut off"}


def validate_isolation(evidence, topology, now, max_age=86400, campaign_id=None):
    age = now - evidence["checked_at"]
    if (age < 0 or (max_age is not None and age > max_age) or evidence["topology"] != topology
            or (campaign_id and evidence.get('campaign_id') != campaign_id)
            or (max_age is None and not campaign_id)):
        raise Abort("isolation evidence expired or topology changed; repeat check-isolation")


def bind_isolation(evidence, topology, campaign_id, attestation):
    if (not campaign_id or not attestation or evidence.get('topology') != topology
            or evidence.get('receiver_local_health') is not True
            or evidence.get('client_to_receiver') != 'unreachable' or evidence.get('ngfw_state') != 'shut off'):
        raise Abort('existing T00 topology/negative test or explicit unchanged-campaign attestation missing')
    return {'campaign_id': campaign_id, 'evidence_sha256': fingerprint(evidence),
            'bound_at': utc(), 'attestation': attestation}


def signature(config, topology):
    return fingerprint({k: config[k] for k in ("duration", "warmup", "idle", "repetitions", "sample_interval", "scenarios", "limits")} | {"topology": topology, 'qualification_mode': config.get('qualification_mode', False)})


def baseline_values(baseline, sig):
    if baseline.get("status") != "completed" or baseline.get("signature") != sig:
        raise Abort("baseline is incomplete or has a different matrix/topology")
    values = {}
    for row in baseline["results"]:
        if row["phase"] == "measure" and "received_bps" in row["metrics"]:
            if row.get('status', 'PASS') != 'PASS' or row['metrics'].get('generator_limited') or row['metrics'].get('invalid_reasons'):
                raise Abort('baseline contains an invalid or stopped measurement window')
            values.setdefault(row["scenario"], []).append(row["metrics"]["received_bps"])
    return {k: statistics.median(v) for k, v in values.items()}


def write_report(directory, report):
    save(directory / "summary.json", report)
    fields = ["scenario", "repetition", "phase", "kind", "received_bps", "sent_bps", "loss_pct",
              "jitter_ms", "received_report_pps", "retransmits", "requests_per_second", "errors",
              "attempted_requests", "successful_requests", "attempt_rate", "success_rate", "error_rate_pct",
              "requested_rate", "achieved_rate", "requested_bps", "achieved_bps", "skipped_slots",
              "scheduler_lag_max_ms", "generator_limited", "generator_limit_reason", "attempt",
              "status", "stop_reason"]
    with (directory / "results.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fields, extrasaction="ignore")
        writer.writeheader()
        for row in report["results"]:
            writer.writerow(row | row["metrics"])
    lines = ["# NGFW traffic run", "", f"Status: **{report['status']}**", "",
             f"AuditD profile label: `{report['audit_profile']}`. Mode: `{report['mode']}`.", "",
             f"Started: {report['started_at']}. Finished: {report.get('finished_at', 'in progress')}.", "",
             "This records workload execution, not acceptance of AuditD rules.", "",
             "| Scenario | Repeat | Phase | Result |", "| --- | ---: | --- | --- |"]
    for row in report["results"]:
        value = row["metrics"]
        detail = (f"{value['received_bps'] / 1e6:.2f} Mbit/s" if "received_bps" in value
                  else f"{value.get('requests_per_second', 0):.2f} successful requests/s")
        if "loss_pct" in value:
            detail += f"; loss {value['loss_pct']:.3f}%"
        if "errors" in value:
            detail += f"; errors {value['errors']}"
        if row.get('status'):
            detail += '; ' + row['status'] + ': ' + (row.get('stop_reason') or '')
        lines.append(f"| {row['scenario']} | {row['repetition']} | {row['phase']} | {detail} |")
    lines += ["", "## Gaps", ""] + ["- " + gap for gap in report["gaps"]]
    lines += ["", "## CPU observations (not stop reasons)", "",
              "CPU action: " + (report.get('measurement_config') or {}).get('cpu_action', 'stop')]
    lines += ["- " + warning for warning in report.get('warnings', [])]
    if report.get("reason"):
        lines += ["", "Stop reason: " + report["reason"]]
    lines += ["", "Raw measurements: metrics.ndjson; each workload: *.json / *.stderr.",
              "Event serial rates are estimates; log rotation creates an explicit rate gap.", ""]
    (directory / "report.md").write_text("\n".join(lines), encoding="utf-8")


class Runner:
    def __init__(self, config, backend, directory, report, guard, baseline, extra_guard=None):
        self.config, self.backend, self.directory, self.report = config, backend, directory, report
        self.guard, self.baseline = guard, baseline
        self.cancelled = False
        self.context = {}
        self.extra_guard = extra_guard
        self.initial_sample = True
        self.previous_endpoint = None
        self.window_samples = []
        self.attempt = 1

    def monitor(self):
        if self.cancelled:
            raise Abort("operator interrupted run")
        sample = self.backend.sample()
        sample['warnings'] = []
        reasons = self.guard.check(sample)
        if self.extra_guard:
            reasons += self.extra_guard.check(sample, initial=self.initial_sample)
        self.initial_sample = False
        endpoint = sample.get('endpoint_counters')
        if endpoint and self.previous_endpoint:
            dt = endpoint['monotonic'] - self.previous_endpoint['monotonic']
            delta = endpoint['rx_bytes'] - self.previous_endpoint['rx_bytes']
            sample['endpoint_rx_bps'] = 8 * delta / dt if dt > 0 and delta >= 0 else None
        self.previous_endpoint = endpoint
        sample.update(self.context)
        self.window_samples.append(sample)
        with (self.directory / "metrics.ndjson").open("a", encoding="utf-8") as output:
            output.write(json.dumps(sample) + "\n")
        for warning in sample['warnings']:
            if warning not in self.report.setdefault('warnings', []):
                self.report['warnings'].append(warning)
        self.report['current'] = self.context | {'sample_time': sample['time'], 'stop_reasons': reasons,
                                               'warnings': sample['warnings']}
        save(self.directory / 'summary.json', self.report)
        for key in sample["unavailable"]:
            gap = key + " metrics unavailable in one or more samples"
            if gap not in self.report["gaps"]:
                self.report["gaps"].append(gap)
        if reasons:
            invalid = all(any(word in reason.lower() for word in
                ('unavailable', 'incomplete', 'changed', 'deadline', 'identity', 'not found')) for reason in reasons)
            raise WindowStop('INVALID' if invalid else 'GLOBAL_STOP', '; '.join(reasons))

    def pause(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.monitor()
            time.sleep(max(0, min(self.config["sample_interval"], end - time.monotonic())))

    def workload(self, scenario, repetition, phase, seconds):
        if not seconds:
            return
        before = len(self.report['results'])
        started = time.monotonic()
        try:
            return self._workload(scenario, repetition, phase, seconds)
        except (Abort, OSError, ValueError, subprocess.SubprocessError) as error:
            if len(self.report['results']) == before:
                outcome = getattr(error, 'outcome', 'GLOBAL_STOP' if isinstance(error, Abort) else 'INVALID')
                self.report['results'].append({'scenario': scenario['name'], 'kind': scenario['kind'],
                    'repetition': repetition, 'phase': phase, 'attempt': self.attempt,
                    'window_started_monotonic': started, 'window_finished_monotonic': time.monotonic(),
                    'metrics': {}, 'status': outcome,
                    'stop_reason': str(error) if isinstance(error, Abort) else type(error).__name__})
                write_report(self.directory, self.report)
            if not isinstance(error, Abort):
                raise WindowStop('INVALID', type(error).__name__) from error
            raise

    def _workload(self, scenario, repetition, phase, seconds):
        if not seconds:
            return
        self.context = {"scenario": scenario["name"], "repetition": repetition, "phase": phase, 'attempt': self.attempt}
        self.context.update(phase_started_at=utc(), phase_duration_seconds=seconds,
                            offered=scenario)
        self.window_samples = []
        self.monitor()
        prefix = self.directory / f"{scenario['name']}-{repetition}-{phase}-attempt{self.attempt}"
        job = uuid.uuid4().hex
        argv = self.backend.compose + ["exec", "-T", "traffic-client", "python3", "/opt/traffic/job.py",
                                       "run", "--timeout", str(seconds + 10), job, "--"] + command(self.config, scenario, seconds)
        deadline = time.monotonic() + seconds + 15
        process = None
        completed = False
        window_start = time.monotonic()
        with prefix.with_suffix(".json").open("w", encoding="utf-8") as out, prefix.with_suffix(".stderr").open("w", encoding="utf-8") as err:
            try:
                process = subprocess.Popen(argv, stdout=out, stderr=err)
                while process.poll() is None:
                    self.monitor()
                    if time.monotonic() > deadline:
                        raise Abort("workload deadline exceeded")
                    time.sleep(.2)
                    # Poll cancellation frequently; metrics at the configured cadence.
                    until = time.monotonic() + self.config["sample_interval"]
                    while process.poll() is None and time.monotonic() < until:
                        if self.cancelled:
                            raise Abort("operator interrupted run")
                        time.sleep(.2)
                if process.returncode:
                    raise Abort(f"workload failed (exit {process.returncode})")
                completed = True
            finally:
                if process is not None and not completed:
                    try:
                        self.backend.endpoint("traffic-client", ["python3", "/opt/traffic/job.py", "stop", job])
                    except (Abort, OSError, subprocess.SubprocessError):
                        self.report["gaps"].append("stop could not be confirmed; container watchdog remains bounded by duration + 10 seconds")
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
        raw = json.loads(prefix.with_suffix(".json").read_text(encoding="utf-8"))
        try:
            metrics = result_metrics(scenario, raw)
        except WindowStop as error:
            metrics = {'invalid_reasons': [str(error)]}
        resource_summary, gaps, limited = qualification.resources(self.window_samples, self.report.get('topology', {}))
        metrics['generator_resources'] = resource_summary
        metrics.setdefault('invalid_reasons', []).extend(gaps)
        if limited:
            metrics.update(generator_limited=True, generator_limit_reason='; '.join(limited))
        outcome, reason = qualification.classify(metrics, repeated=self.attempt > 1)
        if outcome in ('PASS', 'WARN') and phase == 'measure' and scenario['kind'] == 'tcp' and self.baseline:
            base = self.baseline.get(scenario['name'])
            if not base or metrics.get('received_bps', 0) < base * (1 - self.config['limits']['tcp_drop_pct'] / 100):
                outcome, reason = 'SCENARIO_STOP', 'TCP throughput below control threshold'
        row = self.context | {"kind": scenario["kind"], "metrics": metrics,
            "window_started_monotonic": window_start, "window_finished_monotonic": time.monotonic()}
        row.update(status=outcome, stop_reason=reason)
        self.report["results"].append(row)
        write_report(self.directory, self.report)
        if outcome != 'PASS':
            raise WindowStop(outcome, reason)

    def run(self):
        stopped_kinds = set()
        for scenario in self.config["scenarios"]:
            if scenario['kind'] in stopped_kinds:
                continue
            for repetition in range(1, self.config["repetitions"] + 1):
                print(f"{utc()} {scenario['name']} repeat {repetition}", flush=True)
                for attempt in (1, 2):
                    self.attempt = attempt
                    outcome = 'PASS'
                    try:
                        self.workload(scenario, repetition, "warmup", self.config["warmup"])
                        self.workload(scenario, repetition, "measure", self.config["duration"])
                    except WindowStop as error:
                        outcome = error.outcome
                        if outcome == 'GLOBAL_STOP':
                            raise
                        if outcome != 'WARN':
                            stopped_kinds.add(scenario['kind'])
                            self.report['gaps'].append(scenario['kind'] + ' ladder stopped: ' + str(error))
                    self.context.update(phase='idle', phase_started_at=utc(),
                                        phase_duration_seconds=self.config['idle'], offered={})
                    self.pause(self.config["idle"])
                    self.monitor()  # Independent scenarios require a fresh healthy check.
                    if outcome != 'WARN':
                        break
                if scenario['kind'] in stopped_kinds:
                    break
        self.monitor()
        outcomes = {row.get('status', 'PASS') for row in self.report.get('results', [])}
        self.report['outcome'] = next((v for v in ('GLOBAL_STOP', 'INVALID', 'SCENARIO_STOP', 'WARN') if v in outcomes), 'PASS')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "check-isolation", "bind-isolation", "measure-check", "run"])
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("runner.json"))
    parser.add_argument("--audit-profile", default="P0")
    parser.add_argument("--traffic-only", action="store_true")
    parser.add_argument("--probe-argv-file", type=Path, help="local JSON argv for SSH sh -s; never copied to report")
    parser.add_argument("--baseline", type=Path, help="completed compatible summary.json")
    parser.add_argument('--measurement-argv-file', type=Path, help='private SSH argv ending in python3 -')
    parser.add_argument('--measurement-config', type=Path, help='operator-approved sensors and early stop thresholds')
    parser.add_argument('--run-context', type=Path, help='five campaign/test/comparison/workload/profile IDs')
    parser.add_argument('--qualification-plan', choices=['edr-calibration', 'edr-ablation', 'edr-confirmation', 'edr-endurance'])
    parser.add_argument('--campaign-id', help='bind unchanged T00 topology evidence to this campaign')
    parser.add_argument('--selected-workloads', type=Path, help='private calibrated workload selection JSON')
    parser.add_argument('--endurance-minutes', type=int, choices=[30, 60])
    parser.add_argument('--unchanged-campaign-attestation', help='operator evidence reference: no VM/snapshot/route/topology change since existing T00')
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.qualification_plan:
        config.update(json.loads((Path(__file__).parent / 'plans' / (args.qualification_plan + '.json')).read_text()))
        config['limits'] = config['limits'] | {'availability_seconds': 10}
        config['campaign_id'] = args.campaign_id
        if args.audit_profile not in config['allowed_profiles']:
            parser.error('profile not allowed for selected qualification stage')
        if config.get('requires_workload_selection'):
            if not args.selected_workloads:
                parser.error('select identical workloads from calibration with --selected-workloads')
            selected = json.loads(args.selected_workloads.read_text(encoding='utf-8'))
            if not selected.get('calibration_run_ids') or not selected.get('selection_reason'):
                parser.error('workload selection requires calibration_run_ids and selection_reason')
            config['scenarios'] = selected['scenarios']
            config['workload_selection'] = selected
        if args.endurance_minutes:
            if args.qualification_plan != 'edr-endurance':
                parser.error('--endurance-minutes only for endurance')
            config['duration'] = args.endurance_minutes * 60
    config = validate(config)
    measure_config = measurement.validate(json.loads(args.measurement_config.read_text(encoding='utf-8'))) if args.measurement_config else None
    if config.get('qualification_mode') and measure_config:
        measure_config = measure_config | {'cpu_action': 'warn', 'qualification_mode': True}
    context = measurement.load_context(args.run_context) if args.run_context else None
    if context and config.get('campaign_id') and context['campaign_id'] != config['campaign_id']:
        parser.error('run context campaign differs from T00 campaign')
    if context and (not measure_config or not args.measurement_argv_file or args.traffic_only):
        parser.error('methodology context requires extended measurements and cannot use traffic-only')
    if context and context['profile_id'] != args.audit_profile:
        parser.error('context profile_id differs from --audit-profile')
    if (args.action == 'run' and (measure_config or {}).get('cpu_action') == 'warn'
            and (not args.measurement_argv_file or args.traffic_only)):
        parser.error('cpu_action=warn requires extended measurements and cannot use traffic-only')
    if args.measurement_argv_file and args.probe_argv_file:
        parser.error('choose JSON measurement probe OR legacy shell probe, not both')
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", args.audit_profile):
        parser.error("invalid audit profile label")
    if args.action == "plan":
        print(json.dumps({"duration_seconds": len(config["scenarios"]) * config["repetitions"] *
                          (config["warmup"] + config["duration"] + config["idle"]),
                          "repetitions": config["repetitions"], "audit_profile": args.audit_profile,
                          "cpu_action": (measure_config or {}).get('cpu_action', 'stop'),
                          "measure_commands": [command(config, s, config["duration"]) for s in config["scenarios"]]}, indent=2))
        return 0
    if os.name != "posix":
        parser.error("execution requires the Linux KVM host; plan is portable")
    probe_argv = json.loads(args.probe_argv_file.read_text()) if args.probe_argv_file else None
    if probe_argv is not None and (not isinstance(probe_argv, list) or not probe_argv or
                                   not all(isinstance(x, str) and x for x in probe_argv)):
        parser.error("probe file must be a nonempty JSON argv array")
    measurement_argv = json.loads(args.measurement_argv_file.read_text()) if args.measurement_argv_file else None
    if measurement_argv is not None and (not isinstance(measurement_argv, list) or not measurement_argv or
            not all(isinstance(x, str) and x for x in measurement_argv)):
        parser.error('measurement argv must be a nonempty string array')
    if measurement_argv and measurement_argv[-2:] != ['python3', '-']:
        parser.error('measurement argv must end in two arguments: python3 -')
    if args.action == 'measure-check' and not measurement_argv:
        parser.error('measure-check requires --measurement-argv-file')
    if args.action == 'run' and measurement_argv and not measure_config:
        parser.error('extended run requires approved --measurement-config thresholds')
    if args.action == "run" and not args.traffic_only and not (probe_argv or measurement_argv):
        parser.error("AuditD run needs --probe-argv-file; use --traffic-only for a traffic-only control")
    if args.traffic_only and (probe_argv or measurement_argv):
        parser.error("choose either --traffic-only or --probe-argv-file")
    import fcntl
    os.umask(0o077)
    root = Path(config["reports_dir"])
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".runner.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Abort("another traffic runner holds the lock") from exc
        backend = Backend(config, probe_argv, measurement_argv, measure_config)
        try:
            if args.action == 'measure-check':
                directory = root / ('preflight-' + uuid.uuid4().hex[:12])
                directory.mkdir(mode=0o700)
                snapshot = {'host': backend.extended_host(), 'guest': backend.extended_guest(inventory=True)}
                risks = measurement.inventory_risks(snapshot['guest'], measure_config)
                save(directory / 'readiness.json', {'snapshots': snapshot, 'risks': risks,
                     'status': 'BLOCKED' if risks else 'READ_ONLY_CHECKED',
                     'boundary': 'No traffic, no isolation/readiness acceptance, no config changes'})
                print(f"Read-only measurement discovery: {directory / 'readiness.json'}")
                return 2 if risks else 0
            if args.action == "check-isolation":
                evidence = backend.isolation()
                save(root / "isolation.json", evidence)
                print("Isolation evidence saved; start/configure NGFW before run.")
                return 0
            topology = backend.topology()
            isolation = json.loads((root / 'isolation.json').read_text())
            if args.action == 'bind-isolation':
                binding = bind_isolation(isolation, topology, config.get('campaign_id'), args.unchanged_campaign_attestation)
                save(root / 'isolation-binding.json', binding)
                print('Existing T00 bound without traffic or changing original evidence.')
                return 0
            binding_path = root / 'isolation-binding.json'
            if not isolation.get('campaign_id') and binding_path.exists():
                binding = json.loads(binding_path.read_text())
                if binding.get('evidence_sha256') == fingerprint(isolation):
                    isolation = isolation | {'campaign_id': binding['campaign_id']}
            validate_isolation(isolation, topology, time.time(),
                               config.get('isolation_max_age_seconds', 86400), config.get('campaign_id'))
            sig = signature(config, topology)
            baseline_report = json.loads(args.baseline.read_text()) if args.baseline else None
            baseline = baseline_values(baseline_report, sig) if baseline_report else {}
            directory = root / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
            directory.mkdir()
            report = {"schema_version": 1, "started_at": utc(), "audit_profile": args.audit_profile,
                      "status": "running", "mode": "traffic-only" if args.traffic_only else "audit-observed",
                      "config": config, "signature": sig, "topology": topology,
                      "baseline": str(args.baseline) if args.baseline else None, "results": [],
                      "gaps": ["Audit rules/configuration identity and administrative audit events require separate acceptance."]}
            report['run_id'] = directory.name
            report['context'] = context
            report['measurement_config'] = measure_config
            report['tool_sha256'] = {name: hashlib.sha256((Path(__file__).parent / name).read_bytes()).hexdigest()
                                    for name in ('runner.py', 'measurement.py', 'measure_probe.py', 'qualification.py',
                                                 'traffic/endpoint.py', 'traffic/probe_diagnostics.py')}
            if config.get('qualification_mode') and (not context or not measurement_argv or not measure_config):
                raise Abort('qualification requires campaign context and extended measurements')
            if args.traffic_only:
                report["gaps"].append("AuditD and guest filesystem/CPU thresholds are not monitored in traffic-only mode.")
            if not baseline:
                report["gaps"].append("No control run supplied; TCP degradation versus control was not checked.")
            elif baseline_report.get("mode") != report["mode"]:
                report["gaps"].append("Control and current run use different observation modes; SSH/probe overhead is a comparison confounder.")
            save(directory / "isolation.json", json.loads((root / "isolation.json").read_text()))
            if binding_path.exists():
                save(directory / 'isolation-binding.json', json.loads(binding_path.read_text()))
            runner = Runner(config, backend, directory, report,
                            Guard(config["limits"], not args.traffic_only,
                                  (measure_config or {}).get('cpu_action', 'stop')), baseline,
                            measurement.Guard(measure_config) if measure_config else None)
            for sig_num in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig_num, lambda *_a: setattr(runner, "cancelled", True))
            print(f"Reports: {directory}", flush=True)
            write_report(directory, report)
            try:
                if measurement_argv:
                    before = backend.extended_guest(inventory=True)
                    save(directory / 'inventory-before.json', before)
                    risks = measurement.inventory_risks(before, measure_config)
                    if risks:
                        raise Abort('; '.join(risks))
                    # Pin the inventory read-back before the first measured sample;
                    # an EDR control change between preflight and load invalidates it.
                    runner.extra_guard = measurement.Guard(measure_config, initial_guest=before)
                report["iperf_version"] = backend.endpoint("traffic-client", ["iperf3", "--version"])
                runner.run()
                report["status"] = "completed"
            except (Abort, OSError, ValueError, subprocess.SubprocessError) as exc:
                report["status"] = "aborted"
                report["reason"] = str(exc)
                report['outcome'] = getattr(exc, 'outcome', 'GLOBAL_STOP')
            finally:
                if measurement_argv:
                    try:
                        after = backend.extended_guest(inventory=True)
                        save(directory / 'inventory-after.json', after)
                        previous = json.loads((directory / 'inventory-before.json').read_text())
                        changes = measurement.control_changes(previous, after)
                        changes.extend(measurement.inventory_risks(after, measure_config))
                        if previous.get('boot_id') != after.get('boot_id'):
                            changes.append('guest boot changed between inventories')
                        if changes:
                            report['status'] = 'aborted'
                            report['gaps'].extend(changes)
                        for key in ('rules_sha256', 'auditd_conf_sha256', 'forwarder_conf_sha256'):
                            if (previous.get('inventory') or {}).get(key) != (after.get('inventory') or {}).get(key):
                                report['status'] = 'aborted'
                                report['gaps'].append('effective configuration changed: ' + key)
                    except (OSError, ValueError, subprocess.SubprocessError, Abort):
                        report['status'] = 'aborted'
                        report['gaps'].append('final inventory unavailable; comparison incomplete')
                report["finished_at"] = utc()
                write_report(directory, report)
                save(directory / 'manifest.json', measurement.manifest(directory))
            print(f"{report['status']}: {directory / 'report.md'}")
            return 0 if report["status"] == "completed" else 2
        finally:
            backend.pool.shutdown(wait=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (Abort, OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        print(f"Cannot start: {error}")
        raise SystemExit(2)
