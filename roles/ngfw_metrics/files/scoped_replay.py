#!/usr/bin/env python3
"""One-shot selected LP replay using the existing spool; no live data access."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

from metrics_spool import Spool, file_digest, http_sender


def windows_from(value):
    if not isinstance(value, list) or not value or len(value) > 16:
        raise ValueError("Explicit nonempty bounded source windows required")
    windows = []
    for item in value:
        lo, hi = item['source_start_ns'], item['source_end_ns_exclusive']
        if type(lo) is not int or type(hi) is not int or not 0 < lo < hi < 2**63:
            raise ValueError("Windows require signed integer ns timestamps")
        windows.append((lo, hi))
    windows.sort()
    if any(b[0] < a[1] for a, b in zip(windows, windows[1:])):
        raise ValueError("Source windows must not overlap")
    return windows


def payloads(path, immutable=True):
    # Owner-generated single-file backup/selection only, never a live WAL DB.
    if path.is_symlink() or not path.is_file() or (immutable and any(Path(str(path)+s).exists() for s in ('-wal', '-shm'))):
        raise ValueError("Immutable single-file snapshot required")
    suffix = '?mode=ro&immutable=1' if immutable else '?mode=ro'
    with closing(sqlite3.connect(path.as_uri()+suffix, uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        for row in db.execute('SELECT id,payload,sha256 FROM batches ORDER BY id'):
            body = bytes(row[1])
            if hashlib.sha256(body).hexdigest() != row[2]:
                raise ValueError("Payload hash mismatch")
            yield row[0], body, row[2]


def prepare(snapshot, output, window_spec, timeout=600, reserve_bytes=2147483648):
    snapshot, output = Path(snapshot).absolute(), Path(output).absolute()
    windows = windows_from(window_spec)
    if snapshot.name != 'backup.sqlite' or output.exists() or output.is_symlink():
        raise ValueError("New output directory and owner-generated backup.sqlite required")
    expected = snapshot.with_suffix('.sqlite.sha256').read_text(encoding='ascii').strip()
    if file_digest(snapshot) != expected:
        raise ValueError("Snapshot checksum mismatch")
    os.umask(0o077)
    output.mkdir(mode=0o700)
    selected = Spool(output/'selected.sqlite', reserve_bytes=reserve_bytes)
    digest = hashlib.sha256()
    receipt = {'schema_version': 1, 'status': 'PREPARING', 'snapshot_sha256': expected,
               'source_windows': window_spec, 'selected_batches': 0, 'selected_bytes': 0,
               'selected_lines': 0, 'pieces': [], 'source_cursors_changed': False}
    deadline = time.monotonic()+timeout
    for batch_id, raw, sha in payloads(snapshot):
        if time.monotonic() >= deadline:
            raise TimeoutError("Selection deadline")
        lines = []
        for line in raw.splitlines(keepends=True):
            if not line.strip() or line.startswith(b'#'):
                continue
            stamp = int(line.rstrip(b'\r\n').rsplit(b' ', 1)[1])
            if any(lo <= stamp < hi for lo, hi in windows):
                lines.append(line)
        if not lines:
            continue
        body = b''.join(lines)
        selected_id = selected.accept(body)
        digest.update(body)
        receipt['selected_batches'] += 1
        receipt['selected_bytes'] += len(body)
        receipt['selected_lines'] += len(lines)
        receipt['pieces'].append({'source_batch_id': batch_id, 'source_payload_sha256': sha,
                                  'selected_batch_id': selected_id,
                                  'selected_sha256': hashlib.sha256(body).hexdigest()})
    if not receipt['selected_batches'] or file_digest(snapshot) != expected:
        raise ValueError("Empty selection or snapshot changed")
    receipt.update(status='SELECTED_NOT_SENT', ordered_payload_sha256=digest.hexdigest())
    with (output/'manifest.json').open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, indent=2)
    return receipt


def verify_selection(directory):
    directory = Path(directory).resolve()
    manifest = json.loads((directory/'manifest.json').read_bytes())
    if manifest['status'] != 'SELECTED_NOT_SENT' or not manifest['selected_batches']:
        raise ValueError("Completed selection manifest required")
    digest, count, size = hashlib.sha256(), 0, 0
    if len(manifest['pieces']) != manifest['selected_batches']:
        raise ValueError("Selected piece manifest changed")
    # Selection is a mutable cursor DB: honor its WAL after a killed replay.
    for batch_id, body, sha in payloads(directory/'selected.sqlite', immutable=False):
        if count >= len(manifest['pieces']):
            raise ValueError("Unexpected selected batch")
        piece = manifest['pieces'][count]
        if (batch_id, sha) != (piece['selected_batch_id'], piece['selected_sha256']):
            raise ValueError("Selected batch identity changed")
        digest.update(body); count += 1; size += len(body)
    if (count, size, digest.hexdigest()) != (manifest['selected_batches'], manifest['selected_bytes'], manifest['ordered_payload_sha256']):
        raise ValueError("Selected payloads changed")
    return directory/'selected.sqlite'


def replay(directory, sink, timeout=600, send=None):
    # Validate endpoint before opening an updateable selection or registering.
    if not sink.get('write_url'):
        raise ValueError("Endpoint not configured; replay remains disabled")
    sender = http_sender(sink) if send is None else send
    path = verify_selection(directory)
    spool = Spool(path)
    spool.register('scoped_external', sink['write_url'])
    deadline = time.monotonic()+timeout
    rate = sink.get('max_bytes_per_second', 1048576)
    if rate <= 0:
        raise ValueError("Positive drain rate required")
    while time.monotonic() < deadline:
        started = time.monotonic()
        count = spool.deliver_one('scoped_external', sender)
        if count == 0:
            return {'status': 'WRITE_ACKNOWLEDGED_READBACK_REQUIRED', 'sink': spool.status()['sinks'][0]}
        delay = sink.get('retry_seconds', 5) if count < 0 else max(0, count/rate-(time.monotonic()-started))
        time.sleep(min(delay, max(0, deadline-time.monotonic())))
    return {'status': 'INCOMPLETE_RETRYABLE', 'sink': spool.status()['sinks'][0]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'replay'))
    parser.add_argument('--directory', required=True)
    parser.add_argument('--snapshot')
    parser.add_argument('--windows-file')
    parser.add_argument('--config')
    parser.add_argument('--timeout-seconds', type=int, default=600)
    parser.add_argument('--reserve-bytes', type=int, default=2147483648)
    args = parser.parse_args()
    try:
        if not 1 <= args.timeout_seconds <= 3600:
            raise ValueError("Bounded timeout required")
        if args.action == 'prepare':
            if args.reserve_bytes < 0:
                raise ValueError("Nonnegative disk reserve required")
            result = prepare(args.snapshot, args.directory, json.loads(Path(args.windows_file).read_bytes()), args.timeout_seconds, args.reserve_bytes)
            result = {k: v for k, v in result.items() if k != 'pieces'}
        else:
            config = json.loads(Path(args.config).read_bytes())
            sink = next((s for s in config['sinks'] if s['name'] == 'external'), {})
            result = replay(args.directory, sink, args.timeout_seconds)
        print(json.dumps(result))
        return 0 if result['status'] != 'INCOMPLETE_RETRYABLE' else 2
    except Exception as error:
        print(json.dumps({'status': 'FAILED', 'error_type': type(error).__name__}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
