#!/usr/bin/env python3
"""Read-only Influx measurement-liveness receipts with bounded clock evidence.

Source timestamps stay unchanged. This is not all-series/window completeness,
NTP synchronization, or workload acceptance. Clock transport is supplied by the
operator as a fixed argv file; no SSH credentials/configuration are managed here.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

MAX_EVIDENCE_BYTES = 16 * 1024 * 1024


class Unhealthy(ValueError):
    pass


def encode(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(payload):
    return hashlib.sha256(payload).hexdigest()


def rfc3339_ns(epoch_ns):
    if type(epoch_ns) is not int:
        raise Unhealthy("timestamp_requires_integer_ns")
    seconds, nanos = divmod(epoch_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")+".%09dZ" % nanos


def positive_number(value, name, ceiling):
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= ceiling:
        raise Unhealthy("invalid_"+name)
    return value


def read_bounded(path, cap=MAX_EVIDENCE_BYTES):
    with Path(path).open("rb") as stream:
        payload = stream.read(cap+1)
    if len(payload) > cap:
        raise Unhealthy("evidence_file_bound")
    return payload


class EvidenceStore:
    """Content-addressed files, exclusive create, no in-place mutation."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def save(self, kind, payload):
        sha = digest(payload)
        target = self.directory / (kind+"-"+sha+".json")
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if digest(read_bounded(target)) != sha:
                raise Unhealthy("immutable_evidence_changed")
        else:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        return {"path": str(target), "sha256": sha}

    def referenced(self, ref, base):
        if not isinstance(ref, dict) or not re.fullmatch(r"[a-f0-9]{64}", ref.get("sha256", "")):
            raise Unhealthy("invalid_evidence_reference")
        path = Path(ref["path"])
        if not path.is_absolute():
            path = Path(base) / path
        payload = read_bounded(path)
        if digest(payload) != ref["sha256"]:
            raise Unhealthy("source_evidence_digest_mismatch")
        return payload, self.save("catalog-source", payload)


def ensure_complete(value):
    if isinstance(value, dict):
        if value.get("partial") or value.get("truncated") or value.get("error"):
            raise Unhealthy("partial_truncated_or_error_evidence")
        for child in value.values():
            ensure_complete(child)
    elif isinstance(value, list):
        for child in value:
            ensure_complete(child)


def all_series(value):
    if isinstance(value, dict):
        if isinstance(value.get("series"), list):
            for series in value["series"]:
                if isinstance(series, dict):
                    yield series
        for key, child in value.items():
            if key != "series":
                yield from all_series(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_series(child)


def json_pointer(document, pointer):
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise Unhealthy("interval_evidence_requires_json_pointer")
    value = document
    for component in pointer[1:].split("/"):
        key = component.replace("~1", "/").replace("~0", "~")
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def load_catalog(path, store):
    path = Path(path)
    payload = read_bounded(path)
    catalog = json.loads(payload)
    if catalog.get("schema_version") != 1:
        raise Unhealthy("catalog_schema_version")
    interval = positive_number(catalog.get("source_interval_seconds"), "source_interval", 60)
    if interval < 1:
        raise Unhealthy("source_interval_below_one_second")
    database = catalog.get("database")
    if not isinstance(database, str) or not database or len(database) > 128:
        raise Unhealthy("catalog_database")
    selector = catalog.get("host_selector", {})
    if not all(isinstance(selector.get(key), str) and 0 < len(selector[key]) <= 256 for key in ("tag", "value")):
        raise Unhealthy("catalog_exact_host_selector")
    observed_bytes, observation_ref = store.referenced(catalog["observation_evidence"], path.parent)
    observed = json.loads(observed_bytes)
    ensure_complete(observed)
    interval_bytes, interval_ref = store.referenced(catalog["interval_evidence"], path.parent)
    interval_value = json_pointer(json.loads(interval_bytes), catalog["interval_evidence"].get("json_pointer"))
    if interval_value != interval and interval_value != "%gs" % interval:
        raise Unhealthy("catalog_interval_not_observed")
    measurements = catalog.get("measurements")
    if not isinstance(measurements, list) or not 1 <= len(measurements) <= 64:
        raise Unhealthy("catalog_measurement_count")
    names = []
    observed_series = list(all_series(observed))
    for entry in measurements:
        if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and 0 < len(entry[key]) <= 256 for key in ("name", "field")):
            raise Unhealthy("catalog_measurement_or_field")
        name, field = entry["name"], entry["field"]
        names.append(name)
        verified = False
        for series in observed_series:
            columns = series.get("columns", [])
            if series.get("name") != name or field not in columns or "time" not in columns:
                continue
            field_idx, time_idx = columns.index(field), columns.index("time")
            for row in series.get("values", []):
                if not isinstance(row, list) or len(row) != len(columns) or type(row[time_idx]) is not int or row[field_idx] is None:
                    continue
                tag_value = series.get("tags", {}).get(selector["tag"])
                if selector["tag"] in columns:
                    column_tag = row[columns.index(selector["tag"])]
                    if tag_value is not None and column_tag != tag_value:
                        raise Unhealthy("conflicting_observed_host_selector")
                    tag_value = column_tag
                if tag_value == selector["value"]:
                    verified = True
        if not verified:
            raise Unhealthy("catalog_field_and_host_not_observed:"+name)
    if len(set(names)) != len(names):
        raise Unhealthy("duplicate_required_measurement")
    return catalog, store.save("catalog", payload), {"observation": observation_ref, "interval": interval_ref}


def bounded_clock_command(argv, timeout_seconds=5):
    """Bound both pipes and time. No shell, environment credentials, or stderr output."""
    if not isinstance(argv, list) or not argv or not all(isinstance(item, str) and item for item in argv):
        raise Unhealthy("clock_argv_must_be_fixed_nonempty_array")
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    outputs, overflow = {}, threading.Event()
    def drain(name, pipe, limit):
        data = pipe.read(limit+1)
        outputs[name] = data
        if len(data) > limit:
            overflow.set()
            process.kill()
    threads = [threading.Thread(target=drain, args=(name, pipe, limit), daemon=True)
               for name, pipe, limit in (("stdout", process.stdout, 8192), ("stderr", process.stderr, 4096))]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait(timeout=2)
        raise Unhealthy("clock_command_timeout") from exc
    finally:
        for thread in threads:
            thread.join(2)
        process.stdout.close()
        process.stderr.close()
    if overflow.is_set():
        raise Unhealthy("clock_command_output_bound")
    if process.returncode:
        raise Unhealthy("clock_command_nonzero")
    return outputs.get("stdout", b"")


def clock_sample(provider, store, clock):
    sample = {"host_send_epoch_ns": clock.time_ns(), "host_send_monotonic_ns": clock.monotonic_ns()}
    raw = provider()
    sample.update(host_receive_epoch_ns=clock.time_ns(), host_receive_monotonic_ns=clock.monotonic_ns())
    if not isinstance(raw, bytes) or len(raw) > 8192:
        raise Unhealthy("clock_response_bound_or_type")
    guest = json.loads(raw)
    if not isinstance(guest, dict) or set(guest) != {"guest_epoch_ns", "guest_monotonic_ns", "guest_boot_id"}:
        raise Unhealthy("clock_response_exact_schema")
    if type(guest["guest_epoch_ns"]) is not int or type(guest["guest_monotonic_ns"]) is not int or min(guest["guest_epoch_ns"], guest["guest_monotonic_ns"]) <= 0:
        raise Unhealthy("clock_response_requires_positive_integer_ns")
    try:
        uuid.UUID(guest["guest_boot_id"])
    except (ValueError, TypeError, AttributeError) as exc:
        raise Unhealthy("clock_boot_identity_invalid") from exc
    sample.update(guest)
    sample["guest_evidence"] = store.save("guest-clock", raw)
    return sample


def validate_alignment(alignment):
    limits = alignment["limits"]
    uncertainty = positive_number(limits["max_uncertainty_seconds"], "max_uncertainty", 1) * 1e9
    spread_limit = positive_number(limits["max_offset_spread_seconds"], "max_offset_spread", 0.5) * 1e9
    step_limit = positive_number(limits["max_clock_step_seconds"], "max_clock_step", 0.05) * 1e9
    before, after, bounds = alignment["before"], alignment["after"], alignment["query_bounds"]
    offsets = []
    for sample in (before, after):
        keys = ("host_send_epoch_ns", "host_receive_epoch_ns", "host_send_monotonic_ns", "host_receive_monotonic_ns", "guest_epoch_ns", "guest_monotonic_ns")
        if any(type(sample.get(key)) is not int or sample[key] <= 0 for key in keys):
            raise Unhealthy("clock_bracket_requires_positive_integer_ns")
        wall_rtt = sample["host_receive_epoch_ns"]-sample["host_send_epoch_ns"]
        mono_rtt = sample["host_receive_monotonic_ns"]-sample["host_send_monotonic_ns"]
        if min(wall_rtt, mono_rtt) < 0 or wall_rtt/2 > uncertainty:
            raise Unhealthy("clock_bracket_uncertainty")
        if abs(wall_rtt-mono_rtt) > step_limit:
            raise Unhealthy("host_clock_step_in_bracket")
        offsets.append((sample["host_send_epoch_ns"]-sample["guest_epoch_ns"], sample["host_receive_epoch_ns"]-sample["guest_epoch_ns"]))
    if before["guest_boot_id"] != after["guest_boot_id"] or alignment["guest_boot_id"] != before["guest_boot_id"]:
        raise Unhealthy("guest_boot_changed")
    if any(type(bounds.get(key)) is not int for key in ("started_epoch_ns", "completed_epoch_ns", "started_monotonic_ns", "completed_monotonic_ns")):
        raise Unhealthy("query_bounds_require_integer_ns")
    if not (before["host_receive_monotonic_ns"] <= bounds["started_monotonic_ns"] <= bounds["completed_monotonic_ns"] <= after["host_send_monotonic_ns"]):
        raise Unhealthy("query_not_bracketed_monotonic")
    if not (before["host_receive_epoch_ns"] <= bounds["started_epoch_ns"] <= bounds["completed_epoch_ns"] <= after["host_send_epoch_ns"]):
        raise Unhealthy("query_not_bracketed_realtime")
    for first_wall, last_wall, first_mono, last_mono in (
        (before["host_receive_epoch_ns"], after["host_send_epoch_ns"], before["host_receive_monotonic_ns"], after["host_send_monotonic_ns"]),
        (before["guest_epoch_ns"], after["guest_epoch_ns"], before["guest_monotonic_ns"], after["guest_monotonic_ns"]),
        (bounds["started_epoch_ns"], bounds["completed_epoch_ns"], bounds["started_monotonic_ns"], bounds["completed_monotonic_ns"])):
        if last_wall < first_wall or last_mono < first_mono or abs((last_wall-first_wall)-(last_mono-first_mono)) > step_limit:
            raise Unhealthy("clock_step_across_query")
    low, high = min(pair[0] for pair in offsets), max(pair[1] for pair in offsets)
    if high-low > spread_limit:
        raise Unhealthy("offset_envelope_exceeds_limit")
    return low, high


def identifier(value):
    return '"'+value.replace("\\", "\\\\").replace('"', '\\"')+'"'


def string_literal(value):
    return "'"+value.replace("\\", "\\\\").replace("'", "\\'")+"'"


def http_get(url, timeout, cap):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(url, timeout=timeout) as response:
            return response.status, response.read(cap+1)
    except urllib.error.HTTPError as exc:
        with exc:
            return exc.code, exc.read(cap+1)


def parse_last(document, measurement):
    ensure_complete(document)
    results = document.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise Unhealthy("query_statement_count")
    series = results[0].get("series", [])
    if len(series) != 1 or series[0].get("name") != measurement:
        raise Unhealthy("measurement_empty_or_unexpected")
    item = series[0]
    if item.get("columns") != ["time", "value"] or len(item.get("values", [])) != 1:
        raise Unhealthy("last_query_shape")
    row = item["values"][0]
    if not isinstance(row, list) or len(row) != 2 or type(row[0]) is not int or row[0] <= 0 or row[1] is None:
        raise Unhealthy("last_query_missing_or_lossy_timestamp")
    if isinstance(row[1], float) and not math.isfinite(row[1]):
        raise Unhealthy("last_query_nonfinite_value")
    return {"measurement": measurement, "time": rfc3339_ns(row[0]), "source_timestamp_ns": row[0], "value": row[1]}


def atomic_publish(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def collect(config, catalog_path, output_path, evidence_dir, provider, get=http_get, clock=time):
    store = EvidenceStore(evidence_dir)
    health = {"schema_version": 2, "status": "BLOCKED", "scope": "selected_field_measurement_liveness_only_not_all_series_or_window_completeness"}
    raw = {"schema_version": 2, "partial": True, "points": [], "api_requests": [], "scope": health["scope"]}
    alignment = None
    try:
        catalog, catalog_ref, source_refs = load_catalog(catalog_path, store)
        interval = catalog["source_interval_seconds"]
        health.update(database=catalog["database"], source_interval_seconds=interval,
                      required_measurements=[item["name"] for item in catalog["measurements"]], catalog_evidence=catalog_ref,
                      catalog_source_evidence=source_refs)
        raw.update(database=catalog["database"], required_measurements=health["required_measurements"], catalog_evidence=catalog_ref)
        parsed = urllib.parse.urlsplit(config["influx_url"])
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise Unhealthy("influx_endpoint_requires_bare_http_origin_no_credentials")
        endpoint = config["influx_url"].rstrip("/")
        response_cap = config.get("max_response_bytes", 65536)
        if type(response_cap) is not int or not 1024 <= response_cap <= 1048576:
            raise Unhealthy("response_bound_configuration")
        timeout = positive_number(config.get("query_timeout_seconds", 2), "query_timeout", 5)
        query_budget = positive_number(config.get("query_budget_seconds", 10), "query_budget", 20)
        before = clock_sample(provider, store, clock)
        raw["clock_samples"] = {"before": before}
        raw["guest_boot_id"] = health["guest_boot_id"] = before["guest_boot_id"]
        minimum_uptime_ns = int((2*interval+10)*1e9)
        raw["boot_association"] = {"method": "fresh_selected_host_clock_not_native_point_boot_tag",
                                   "minimum_guest_uptime_ns": minimum_uptime_ns}
        if before["guest_monotonic_ns"] < minimum_uptime_ns:
            raise Unhealthy("guest_boot_too_recent_for_native_freshness")
        bounds = {"started_epoch_ns": clock.time_ns(), "started_monotonic_ns": clock.monotonic_ns()}
        lower = before["guest_epoch_ns"]-int((4*interval+30)*1e9)
        upper = before["guest_epoch_ns"]+int((query_budget+5)*1e9)
        selector = catalog["host_selector"]
        for entry in catalog["measurements"]:
            remaining = query_budget-(clock.monotonic_ns()-bounds["started_monotonic_ns"])/1e9
            if remaining <= 0:
                raise Unhealthy("query_budget_exhausted")
            statement = ("SELECT LAST("+identifier(entry["field"])+") AS \"value\" FROM "+identifier(entry["name"])
                         +" WHERE "+identifier(selector["tag"])+" = "+string_literal(selector["value"])
                         +" AND time >= "+str(lower)+" AND time <= "+str(upper))
            url = endpoint+"/query?"+urllib.parse.urlencode({"db": catalog["database"], "epoch": "ns", "q": statement})
            request = {"measurement": entry["name"], "selected_field": entry["field"], "query": statement,
                       "database": catalog["database"], "epoch": "ns", "endpoint": endpoint,
                       "started_epoch_ns": clock.time_ns(), "started_monotonic_ns": clock.monotonic_ns()}
            try:
                status, body = get(url, min(timeout, remaining), response_cap)
            except Exception as exc:
                request.update(error_class=type(exc).__name__, complete=False)
                raw["api_requests"].append(request)
                raise Unhealthy("influx_request_failed") from exc
            request.update(completed_epoch_ns=clock.time_ns(), completed_monotonic_ns=clock.monotonic_ns(),
                           http_status=status, response_bytes=len(body), truncated=len(body)>response_cap,
                           response_evidence=store.save("influx-response", body), complete=False)
            raw["api_requests"].append(request)
            if status != 200 or len(body) > response_cap:
                raise Unhealthy("influx_http_status_or_response_bound")
            point = parse_last(json.loads(body), entry["name"])
            point["selected_field"] = entry["field"]
            request["complete"] = True
            raw["points"].append(point)
        bounds.update(completed_epoch_ns=clock.time_ns(), completed_monotonic_ns=clock.monotonic_ns())
        raw["query_bounds"] = bounds
        if bounds["completed_monotonic_ns"]-bounds["started_monotonic_ns"] > query_budget*1e9:
            raise Unhealthy("query_budget_exhausted")
        after = clock_sample(provider, store, clock)
        raw["clock_samples"]["after"] = after
        if after["guest_monotonic_ns"] < minimum_uptime_ns:
            raise Unhealthy("guest_boot_too_recent_for_native_freshness")
        alignment = {"schema_version": 1, "scope": "bounded_clock_alignment_not_ntp_synchronization",
                     "guest_boot_id": before["guest_boot_id"], "before": before, "after": after, "query_bounds": bounds,
                     "limits": {"max_uncertainty_seconds": config.get("max_uncertainty_seconds", 1),
                                "max_offset_spread_seconds": config.get("max_offset_spread_seconds", 0.5),
                                "max_clock_step_seconds": config.get("max_clock_step_seconds", 0.05)}}
        low, high = validate_alignment(alignment)
        alignment.update(offset_min_ns=low, offset_max_ns=high, offset_convention="host_minus_guest",
                         max_observed_uncertainty_ns=max((sample["host_receive_epoch_ns"]-sample["host_send_epoch_ns"])/2 for sample in (before, after)))
        now = clock.time_ns()
        freshness_ns = int((2*interval+10)*1e9)
        ages = []
        for point in raw["points"]:
            low_age = now-(point["source_timestamp_ns"]+high)
            high_age = now-(point["source_timestamp_ns"]+low)
            ages.append({"measurement": point["measurement"], "raw_source_timestamp_ns": point["source_timestamp_ns"],
                         "age_min_ns": low_age, "age_max_ns": high_age})
        raw["partial"] = False
        raw["evaluated_at_epoch_ns"] = now
        raw["aligned_age_intervals"] = ages
        query_ref = store.save("query", encode(raw))
        alignment["query_evidence_sha256"] = query_ref["sha256"]
        health.update(query_evidence=query_ref, clock_alignment=store.save("alignment", encode(alignment)),
                      source_timestamp_policy="preserved_unmodified", aligned_age_intervals=ages)
        if any(not 0 <= item["age_min_ns"] <= item["age_max_ns"] <= freshness_ns for item in ages):
            raise Unhealthy("required_measurement_stale_or_future_after_alignment")
        health["status"] = "HEALTHY"
    except Exception as exc:
        health["status"] = "BLOCKED"
        health["error_class"] = type(exc).__name__
        health["reason"] = str(exc) if isinstance(exc, Unhealthy) else "invalid_input_or_unavailable_evidence"
        # Never leave a previous healthy latest-file behind when this poll failed.
        if "query_evidence" not in health:
            raw["failure"] = {"error_class": health["error_class"], "reason": health["reason"]}
            health["query_evidence"] = store.save("query-failed", encode(raw))
            if alignment is not None:
                alignment["query_evidence_sha256"] = health["query_evidence"]["sha256"]
                health["clock_alignment"] = store.save("alignment-failed", encode(alignment))
    health["collected_at"] = rfc3339_ns(clock.time_ns())
    health["receipt_archive"] = store.save("health", encode(health))
    atomic_publish(output_path, encode(health))
    return health


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--catalog", required=True, type=Path)
    parser.add_argument("--clock-argv-file", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    args = parser.parse_args()
    try:
        config = json.loads(read_bounded(args.config))
        argv = json.loads(read_bounded(args.clock_argv_file, 16384))
    except Exception as exc:
        store = EvidenceStore(args.evidence_dir)
        reason = "configuration_or_clock_argv_unavailable"
        raw = {"schema_version": 2, "partial": True, "points": [], "api_requests": [],
               "failure": {"reason": reason, "error_class": type(exc).__name__}}
        receipt = {"schema_version": 2, "status": "BLOCKED", "collected_at": rfc3339_ns(time.time_ns()),
                   "reason": reason, "error_class": type(exc).__name__, "query_evidence": store.save("query-failed", encode(raw))}
        receipt["receipt_archive"] = store.save("health", encode(receipt))
        atomic_publish(args.output, encode(receipt))
        print(json.dumps({"status": "BLOCKED", "output": str(args.output), "reason": reason}))
        return 1
    provider = lambda: bounded_clock_command(argv, timeout_seconds=5)
    receipt = collect(config, args.catalog, args.output, args.evidence_dir, provider)
    print(json.dumps({"status": receipt["status"], "output": str(args.output), "reason": receipt.get("reason")}))
    return 0 if receipt["status"] == "HEALTHY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
