#!/usr/bin/env python3
"""Offline audit window attribution and exact source/receipt matching. No raw output.

Read immutable private exports, including rotations, from ONE guest and boot.
SQLite keeps bounded working memory. Output still contains sensitive timing metadata
and hashes: keep private, review aggregated reports before publishing.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3

RECORD = re.compile(r'\btype=([A-Z0-9_]+)\s+msg=audit\(([0-9]+\.[0-9]+):([0-9]+)\):')
KEY = re.compile(r'\bkey=(?:"([^"\n]*)"|([^\s]+))')
GROUPS = {'packet', 'network_syscall', 'exec', 'file', 'admin'}


def parse(line, source, mapping):
    received = None
    if source == 'collector':
        row = json.loads(line)
        if not isinstance(row, dict) or row.get('stream') != 'auditd' or row.get('source_ip') != '10.77.0.20':
            return None
        raw = row['raw']
        if not isinstance(raw, str):
            raise ValueError('invalid raw field')
        received = datetime.fromisoformat(row['received_at'].replace('Z', '+00:00'))
        if received.tzinfo is None:
            raise ValueError('receipt timestamp has no timezone')
        received = received.timestamp()
    else:
        raw = line.decode('utf-8', errors='strict')
    match = RECORD.search(raw)
    if not match:
        raise ValueError('no supported audit record')
    kind, stamp, serial = match.groups()
    # Strip the transport header only. Never rewrite the audit payload for matching.
    payload = raw[match.start():].rstrip('\r\n')
    keys = [a or b for a, b in KEY.findall(payload)]
    groups = {mapping[k] for key in keys for k in key.split('\x01') if k in mapping}
    if kind == 'NETFILTER_PKT':
        groups.add('packet')
    group = sorted(groups)[0] if len(groups) == 1 else 'mixed' if groups else 'unattributed'
    return {'event': stamp + ':' + serial, 'epoch': float(stamp), 'kind': kind,
            'group': group, 'received': received, 'bytes': len(payload.encode('utf-8')),
            'keys': sorted({k for key in keys for k in key.split('\x01') if k and k != '(null)'}),
            'hash': hashlib.sha256(payload.encode('utf-8')).hexdigest()}


def summarize(inputs, destination, source, scope, start, end, mapping, max_bytes=268435456):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', scope):
        raise ValueError('scope must identify one guest and boot without spaces')
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end:
        raise ValueError('explicit finite audit epoch window required')
    if not isinstance(mapping, dict) or len(mapping) > 256 or any(
            not isinstance(k, str) or len(k) > 128 or v not in GROUPS for k, v in mapping.items()):
        raise ValueError('invalid approved audit key-to-group mapping')
    if source not in ('audit', 'collector') or not 1 <= max_bytes <= 1073741824:
        raise ValueError('invalid source/read limit')
    os.umask(0o077)
    destination = Path(destination)
    destination.mkdir(mode=0o700)  # Never overwrite evidence.
    db = sqlite3.connect(destination / 'records.sqlite')
    try:
        db.executescript('CREATE TABLE records (digest TEXT PRIMARY KEY, event TEXT, kind TEXT, grp TEXT, '
                         'bytes INTEGER, copies INTEGER, epoch REAL, received REAL); '
                         'CREATE INDEX event_idx ON records(event); '
                         'CREATE TABLE event_keys (event TEXT, key TEXT, PRIMARY KEY(event,key));')
        counts, files, total, gaps = Counter(), [], 0, []
        for path in map(Path, inputs):
            before = path.stat()
            if total + before.st_size > max_bytes:
                raise ValueError('input bound exceeded; split into declared windows')
            digest = hashlib.sha256()
            with path.open('rb') as src:
                while True:
                    line = src.readline(131073)
                    if not line:
                        break
                    if len(line) > 131072 or total + len(line) > max_bytes:
                        raise ValueError('line or total read bound exceeded')
                    digest.update(line)
                    total += len(line)
                    try:
                        row = parse(line, source, mapping)
                    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                        counts['malformed'] += 1
                        continue
                    if row is None:
                        counts['other_source'] += 1
                        continue
                    if not start <= row['epoch'] < end:
                        counts['outside_window'] += 1
                        continue
                    counts['records_in_window'] += 1
                    db.executemany('INSERT OR IGNORE INTO event_keys VALUES (?,?)',
                                   [(row['event'], key) for key in row['keys']])
                    db.execute('INSERT INTO records VALUES (?,?,?,?,?,1,?,?) ON CONFLICT(digest) DO UPDATE SET '
                               'copies=copies+1, received=MIN(received,excluded.received)',
                               (row['hash'], row['event'], row['kind'], row['group'], row['bytes'], row['epoch'], row['received']))
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                gaps.append('an input changed during analysis; immutable export required')
            files.append({'sha256': digest.hexdigest(), 'bytes': before.st_size})
        # PATH/EXECVE siblings inherit their event's unambiguous approved key group.
        db.execute("CREATE TEMP TABLE event_groups AS SELECT event, MIN(grp) grp FROM records "
                   "WHERE grp != 'unattributed' GROUP BY event HAVING COUNT(DISTINCT grp)=1")
        db.execute("UPDATE records SET grp=(SELECT grp FROM event_groups WHERE event_groups.event=records.event) "
                   "WHERE grp='unattributed' AND event IN (SELECT event FROM event_groups)")
        db.commit()
        if counts['malformed']:
            gaps.append('malformed/unsupported records; completeness not established')
        stats = {}
        for dimension, column in [('groups', 'grp'), ('types', 'kind')]:
            stats[dimension] = {name: {'records': n, 'distinct_records': distinct, 'events': events,
                                      'bytes': size, 'records_s': n / (end - start), 'bytes_s': size / (end - start)}
                               for name, n, distinct, events, size in db.execute(
                                   f'SELECT {column},SUM(copies),COUNT(*),COUNT(DISTINCT event),SUM(bytes*copies) FROM records GROUP BY {column}')}
        meta = {'schema_version': 1, 'scope': scope, 'source': source, 'start': start, 'end': end,
                'events_by_key': {key: n for key, n in db.execute('SELECT key,COUNT(*) FROM event_keys GROUP BY key')},
                'audit_eps': db.execute('SELECT COUNT(DISTINCT event) FROM records').fetchone()[0] / (end - start),
                'audit_bytes_s': db.execute('SELECT COALESCE(SUM(bytes),0) FROM records').fetchone()[0] / (end - start),
                'key_map_sha256': hashlib.sha256(json.dumps(mapping, sort_keys=True).encode()).hexdigest(),
                'events': db.execute('SELECT COUNT(DISTINCT event) FROM records').fetchone()[0],
                'counts': dict(counts), 'files': files, 'gaps': gaps, **stats,
                'boundary': 'Observed export window only; no proof of capture completeness or causality. Event counts across groups may overlap.'}
        (destination / 'summary.json').write_text(json.dumps(meta, indent=2) + '\n', encoding='utf-8')
        return meta
    finally:
        db.close()


def compare(source, receiver):
    a, b = [json.loads((Path(p) / 'summary.json').read_text(encoding='utf-8')) for p in (source, receiver)]
    if a['source'] != 'audit' or b['source'] != 'collector' or any(a[k] != b[k] for k in ('scope', 'start', 'end', 'key_map_sha256')):
        raise ValueError('source/receiver must cover the same one-boot window and key map')
    db = sqlite3.connect((Path(source).resolve() / 'records.sqlite').as_uri() + '?mode=ro', uri=True)
    try:
        db.execute('ATTACH DATABASE ? AS receiver', ((Path(receiver).resolve() / 'records.sqlite').as_uri() + '?mode=ro',))
        source_records = db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        matched = db.execute('SELECT COUNT(*) FROM records a JOIN receiver.records b ON a.digest=b.digest').fetchone()[0]
        duplicates = db.execute('SELECT COALESCE(SUM(copies-1),0) FROM receiver.records').fetchone()[0]
        extra = db.execute('SELECT COUNT(*) FROM receiver.records b LEFT JOIN records a ON a.digest=b.digest WHERE a.digest IS NULL').fetchone()[0]
        lag = db.execute('SELECT MIN(b.received-a.epoch),AVG(b.received-a.epoch),MAX(b.received-a.epoch) '
                         'FROM records a JOIN receiver.records b ON a.digest=b.digest').fetchone()
        return {'scope': a['scope'], 'start': a['start'], 'end': a['end'], 'source_distinct_records': source_records,
                'matched_distinct_records': matched, 'missing_in_receiver_export': source_records - matched,
                'receiver_extra_distinct_records': extra, 'receiver_duplicate_records': duplicates,
                'delivery_lag_seconds_clock_dependent': dict(zip(('min', 'mean', 'max'), lag)),
                'gaps': a['gaps'] + b['gaps'], 'verdict': 'OBSERVED_WINDOW_ONLY',
                'boundary': 'Missing is not proven loss: verify complete source/rotations, receiver drain, clock offset and absence of transformation. Raw payloads are not exported.'}
    finally:
        db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='action', required=True)
    capture = subs.add_parser('summarize')
    capture.add_argument('--input', type=Path, nargs='+', required=True)
    capture.add_argument('--output', type=Path, required=True)
    capture.add_argument('--source', choices=['audit', 'collector'], required=True)
    capture.add_argument('--scope', required=True)
    capture.add_argument('--start', type=float, required=True)
    capture.add_argument('--end', type=float, required=True)
    capture.add_argument('--key-map', type=Path, required=True)
    capture.add_argument('--max-bytes', type=int, default=268435456)
    diff = subs.add_parser('compare')
    diff.add_argument('--source', type=Path, required=True)
    diff.add_argument('--receiver', type=Path, required=True)
    args = parser.parse_args()
    if args.action == 'summarize':
        result = summarize(args.input, args.output, args.source, args.scope, args.start, args.end,
                           json.loads(args.key_map.read_text(encoding='utf-8')), args.max_bytes)
    else:
        result = compare(args.source, args.receiver)
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == '__main__':
    main()
