#!/usr/bin/env python3
"""Bounded owner-run SQLite snapshot evidence; never serves or delivers data."""
import argparse
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

EXPORT_FILES = ('backup.sqlite', 'backup.sqlite.sha256', 'status.json', 'sizing.json',
                'ingest-minutes.json', 'manifest.json')


def require(value, reason):
    if not value:
        raise ValueError(reason)


def digest(path, deadline, limit):
    result = hashlib.sha256()
    count = 0
    with Path(path).open('rb') as stream:
        while True:
            require(time.monotonic() < deadline, 'evidence_deadline')
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            count += len(chunk)
            require(count <= limit, 'evidence_file_bound')
            result.update(chunk)
    return {'sha256': result.hexdigest(), 'bytes': count}


def save(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, sort_keys=True, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())


def source_sizes(source):
    rows = {}
    for suffix in ('', '-wal', '-shm'):
        path = Path(str(source) + suffix)
        if path.exists():
            require(not path.is_symlink() and path.is_file(), 'source_sidecar_type')
            info = path.stat()
            rows[path.name] = {'logical_bytes': info.st_size,
                               'allocated_bytes': getattr(info, 'st_blocks', 0) * 512 if hasattr(info, 'st_blocks') else None}
    return rows


def cli(helper, action, arguments, deadline, run=subprocess.run):
    remaining = deadline - time.monotonic()
    require(remaining > 0, 'evidence_deadline')
    result = run([sys.executable, str(helper), action, *arguments], capture_output=True,
                 timeout=min(120, remaining))
    require(result.returncode == 0, 'spool_cli_failed:' + action)
    require(len(result.stdout) <= 1024 * 1024, 'spool_cli_response_bound')
    value = json.loads(result.stdout)
    require(isinstance(value, dict), 'spool_cli_response_shape')
    return value


def export_minutes(snapshot, start, end, captured_at, deadline):
    # Exclude partially covered workload and snapshot minutes, including when
    # this analysis runs later than the snapshot. Times are host receipt times.
    lower = math.ceil(start / 60)
    upper = math.floor(min(end, captured_at) / 60)
    require(upper > lower and upper - lower <= 10080, 'complete_minute_window_bound')
    with closing(sqlite3.connect(snapshot.as_uri() + '?mode=ro&immutable=1', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        rows = db.execute('SELECT minute,bytes,points FROM ingest_minutes WHERE minute>=? AND minute<? ORDER BY minute LIMIT 10081',
                          (lower, upper)).fetchall()
        require(len(rows) <= 10080, 'ingest_row_bound')
        require(all(all(type(v) is int and v >= 0 for v in row) for row in rows), 'ingest_values_invalid')
    present = {row[0] for row in rows}
    missing = [minute for minute in range(lower, upper) if minute not in present]
    peak = max((row[1] for row in rows), default=None)
    return {'status': 'OBSERVED_COMPLETE_MINUTES' if not missing else 'MISSING_MINUTES',
            'requested_host_start_epoch': start, 'requested_host_end_epoch': end,
            'snapshot_capture_started_epoch': captured_at,
            'minute_start_inclusive': lower, 'minute_end_exclusive': upper,
            'rows': [{'minute': m, 'bytes': b, 'points': p} for m, b, p in rows],
            'missing_minutes': missing, 'complete': not missing, 'peak_minute_bytes': peak,
            'queue_24h_payload_bytes_30pct_margin': peak * 1872 if peak is not None and not missing else None,
            'raw_30d_payload_bytes_30pct_margin': peak * 56160 if peak is not None and not missing else None,
            'archive_90d_payload_bytes_30pct_margin': peak * 168480 if peak is not None and not missing else None,
            'scope': 'Observed accepted LP bytes, including retries; not physical disk capacity, all-workload peak, or external delivery acceptance.'}


def collect(source, helper, output, start, end, max_bytes=1024**3,
            reserve_bytes=2 * 1024**3, timeout_seconds=180, run=subprocess.run):
    require(all(math.isfinite(v) for v in (start, end)) and 0 < start < end, 'host_time_bounds')
    require(end <= time.time(), 'past_host_interval_required')
    require(120 <= timeout_seconds <= 600 and 0 < max_bytes <= 128 * 1024**3
            and reserve_bytes >= 0, 'resource_bounds')
    source, helper, output = Path(source).absolute(), Path(helper).absolute(), Path(output).absolute()
    require(source.is_file() and not source.is_symlink(), 'existing_source_required')
    require(helper.is_file() and not helper.is_symlink(), 'installed_helper_required')
    require(source.parent.resolve() not in (output.resolve(), *output.resolve().parents), 'evidence_outside_spool_required')
    require(output.parent.is_dir() and not output.is_symlink(), 'existing_evidence_parent_required')
    deadline = time.monotonic() + timeout_seconds
    helper_ref = digest(helper, deadline, 2 * 1024**2)
    collector_ref = digest(Path(__file__), deadline, 2 * 1024**2)
    request = {'source': str(source), 'helper': str(helper), 'helper_sha256': helper_ref['sha256'],
               'collector_sha256': collector_ref['sha256'],
               'start_epoch': start, 'end_epoch': end, 'max_snapshot_bytes': max_bytes,
               'reserve_bytes': reserve_bytes, 'timeout_seconds': timeout_seconds}
    if output.exists():
        # A completed identical request may be fetched again; partials are never
        # resumed, overwritten or silently accepted.
        manifest = json.loads((output / 'manifest.json').read_bytes())
        require(manifest['request'] == request and manifest['status'] == 'COLLECTED_SQLITE_SNAPSHOT', 'existing_request_differs')
        require(set(manifest['files']) == set(EXPORT_FILES) - {'manifest.json'}, 'manifest_file_set')
        for name, reference in manifest['files'].items():
            path = output / name
            require(not path.is_symlink() and digest(path, deadline, max_bytes) == reference, 'existing_artifact_changed')
        return manifest
    before = source_sizes(source)
    require(sum(row['logical_bytes'] for row in before.values()) <= max_bytes, 'source_snapshot_bound')
    # The existing CLI initializes its schema. Refuse a wrong/empty source
    # before invoking it, so an evidence request cannot provision a new spool.
    with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        require({'batches', 'cursors', 'ingest_minutes', 'totals'} <= tables, 'existing_spool_schema_required')
    require(shutil.disk_usage(output.parent).free >= 3 * max_bytes + reserve_bytes, 'snapshot_space_reserve')
    os.umask(0o077)
    output.mkdir(mode=0o700)
    started = time.time()
    save(output / 'request.json', dict(request, started_epoch=started, source_files_before=before))
    snapshot, work = output / 'backup.sqlite', output / 'working.sqlite'
    try:
        backup = cli(helper, 'backup', ['--spool', str(source), '--file', str(snapshot)], deadline, run)
        snapshot_ref = digest(snapshot, deadline, max_bytes)
        require(snapshot_ref['sha256'] == backup['sha256'] == snapshot.with_suffix('.sqlite.sha256').read_text().strip(), 'backup_sha_differs')
        cli(helper, 'restore', ['--file', str(snapshot), '--spool', str(work)], deadline, run)
        status = cli(helper, 'status', ['--spool', str(work)], deadline, run)
        sizing = cli(helper, 'sizing', ['--spool', str(work)], deadline, run)
        # The CLI sizing is preserved as its own all-history observation. Only
        # this bounded export aligns complete minutes to the supplied workload.
        minutes = export_minutes(snapshot, start, end, started, deadline)
        save(output / 'status.json', status)
        save(output / 'sizing.json', sizing)
        save(output / 'ingest-minutes.json', minutes)
        require(digest(helper, deadline, 2 * 1024**2) == helper_ref, 'installed_helper_changed')
        files = {name: digest(output / name, deadline, max_bytes) for name in EXPORT_FILES if name != 'manifest.json'}
        require(files['backup.sqlite'] == snapshot_ref, 'immutable_backup_changed')
        manifest = {'schema_version': 1, 'status': 'COLLECTED_SQLITE_SNAPSHOT', 'request': request,
                    'started_epoch': started, 'completed_epoch': time.time(), 'files': files,
                    'source_files_before': before, 'source_files_after': source_sizes(source),
                    'analysis_copy': str(work), 'analysis_status_scope': 'working copy, not live filesystem sizes',
                    'evidence_helper': collector_ref,
                    'ingest_window_complete': minutes['complete'], 'storage_acceptance': False}
        save(output / 'manifest.json', manifest)
        return manifest
    except BaseException as exc:
        save(output / 'failure.json', {'status': 'INCOMPLETE', 'exception_type': type(exc).__name__,
                                    'started_epoch': started, 'failed_epoch': time.time()})
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'helper', 'output-dir'):
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ('start-epoch', 'end-epoch'):
        parser.add_argument('--' + name, type=float, required=True)
    parser.add_argument('--max-snapshot-bytes', type=int, default=1024**3)
    parser.add_argument('--reserve-bytes', type=int, default=2 * 1024**3)
    parser.add_argument('--timeout-seconds', type=int, default=180)
    args = parser.parse_args()
    result = collect(args.source, args.helper, args.output_dir, args.start_epoch, args.end_epoch,
                     args.max_snapshot_bytes, args.reserve_bytes, args.timeout_seconds)
    print(json.dumps({'status': result['status'], 'manifest': str(args.output_dir / 'manifest.json'),
                      'ingest_window_complete': result['ingest_window_complete'], 'storage_acceptance': False}))


if __name__ == '__main__':
    main()
