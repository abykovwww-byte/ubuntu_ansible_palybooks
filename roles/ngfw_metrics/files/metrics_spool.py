#!/usr/bin/env python3
"""Durable, byte-preserving Influx line-protocol receiver and independent sinks.

No third-party dependencies. An ACK means SQLite committed, not sink delivery.
Only explicit nanosecond timestamps are accepted so retries retain point identity.
"""
import argparse
from contextlib import contextmanager, closing
import hashlib
import http.server
import ipaddress
import json
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request


def file_digest(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_payload(body):
    text = body.decode("utf-8", errors="strict")
    points = 0
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        try:
            prefix, timestamp = line.rsplit(" ", 1)
            value = int(timestamp)
            if " " not in prefix or not -(2**63) < value < 2**63 - 1:
                raise ValueError()
        except ValueError as exc:
            raise ValueError("Every point requires an explicit signed int64 ns timestamp") from exc
        points += 1
    if not points:
        raise ValueError("No points")
    return points


class Spool:
    def __init__(self, path, max_bytes=0, reserve_bytes=0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.reserve_bytes = reserve_bytes
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL,
                    payload BLOB NOT NULL, sha256 TEXT NOT NULL, points INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS cursors (
                    name TEXT PRIMARY KEY, batch_id INTEGER NOT NULL DEFAULT 0,
                    delivered_bytes INTEGER NOT NULL DEFAULT 0,
                    last_success REAL, failures INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT, destination_hash TEXT);
                CREATE TABLE IF NOT EXISTS ingest_minutes (
                    minute INTEGER PRIMARY KEY, bytes INTEGER NOT NULL, points INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS totals (
                    name TEXT PRIMARY KEY, value INTEGER NOT NULL);
                INSERT OR IGNORE INTO totals VALUES ('accepted_bytes',0),('accepted_points',0),('retained_bytes',0);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.row_factory = sqlite3.Row
            with db:
                yield db
        finally:
            db.close()

    def accept(self, payload, now=None):
        points = validate_payload(payload)
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            used = db.execute("SELECT value FROM totals WHERE name='retained_bytes'").fetchone()[0]
            free = shutil.disk_usage(self.path.parent).free
            if (self.max_bytes and used + len(payload) > self.max_bytes) or free < self.reserve_bytes + 3 * len(payload):
                raise BufferError("Spool storage budget exhausted; source must retry, data was not acknowledged")
            row = db.execute("INSERT INTO batches(received,payload,sha256,points) VALUES (?,?,?,?)",
                             (now, payload, hashlib.sha256(payload).hexdigest(), points))
            db.execute("INSERT INTO ingest_minutes VALUES (?,?,?) ON CONFLICT(minute) DO UPDATE SET bytes=bytes+excluded.bytes,points=points+excluded.points",
                       (int(now // 60), len(payload), points))
            db.execute("UPDATE totals SET value=value+? WHERE name='accepted_bytes'", (len(payload),))
            db.execute("UPDATE totals SET value=value+? WHERE name='retained_bytes'", (len(payload),))
            db.execute("UPDATE totals SET value=value+? WHERE name='accepted_points'", (points,))
            return row.lastrowid

    def register(self, name, destination):
        identity = hashlib.sha256(destination.encode()).hexdigest()
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO cursors(name,destination_hash) VALUES (?,?)", (name, identity))
            saved = db.execute("SELECT destination_hash FROM cursors WHERE name=?", (name,)).fetchone()[0]
            if saved != identity:
                raise ValueError("Destination changed for an existing cursor; use a new named sink for explicit replay")

    def deliver_one(self, name, send):
        with self.connect() as db:
            cursor = db.execute("SELECT batch_id FROM cursors WHERE name=?", (name,)).fetchone()
            if cursor is None:
                raise ValueError("Unregistered destination")
            row = db.execute("SELECT * FROM batches WHERE id>? ORDER BY id LIMIT 1", (cursor[0],)).fetchone()
        if row is None:
            return 0
        payload = bytes(row["payload"])
        try:
            send(payload)
        except Exception as exc:
            # Never persist response bodies or URLs that may contain credentials.
            with self.connect() as db:
                db.execute("UPDATE cursors SET failures=failures+1,last_error=? WHERE name=?",
                           (type(exc).__name__, name))
            return -1
        with self.connect() as db:
            db.execute("UPDATE cursors SET batch_id=?,delivered_bytes=delivered_bytes+?,last_success=?,last_error=NULL WHERE name=? AND batch_id=?",
                       (row["id"], len(payload), time.time(), name, cursor[0]))
        return len(payload)

    def status(self):
        with self.connect() as db:
            state = {row["name"]: row["value"] for row in db.execute("SELECT * FROM totals")}
            state["sinks"] = []
            for row in db.execute("SELECT * FROM cursors ORDER BY name").fetchall():
                entry = dict(row)
                queue = db.execute("SELECT COUNT(*),COALESCE(SUM(length(payload)),0),MIN(received) FROM batches WHERE id>?", (row["batch_id"],)).fetchone()
                entry.update(pending_batches=queue[0], pending_bytes=queue[1], oldest_pending_age_s=None if queue[2] is None else time.time()-queue[2])
                state["sinks"].append(entry)
            state["sqlite_bytes"] = sum(p.stat().st_size for p in self.path.parent.glob(self.path.name + "*") if p.is_file())
            state["free_bytes"] = shutil.disk_usage(self.path.parent).free
            return state

    def sizing(self):
        with self.connect() as db:
            # Exclude partial current minute; do not call an average a peak.
            rows = db.execute("SELECT * FROM ingest_minutes WHERE minute<? ORDER BY minute", (int(time.time()//60),)).fetchall()
            if not rows:
                return {"status": "INSUFFICIENT_OBSERVATION", "peak_bytes_per_second": None}
            peak = max(row["bytes"] for row in rows) / 60
            return {"status": "OBSERVED_TRAFFIC_ONLY_NOT_CAPACITY_PROOF", "observed_minutes": len(rows),
                    "peak_60s_bytes_per_second": peak,
                    "queue_24h_payload_bytes_30pct_margin": int(peak*86400*1.3+0.999),
                    "raw_30d_payload_bytes_30pct_margin": int(peak*86400*30*1.3+0.999),
                    "archive_90d_payload_bytes_30pct_margin": int(peak*86400*90*1.3+0.999),
                    "note": "Payload lower bounds; add measured SQLite/WAL/index and InfluxDB amplification, backups and free reserve. Peak is a 60s mean; bursts require separate allowance."}

    def raw_export(self, target):
        """Export original bytes in receive order; no dedup or field conversion."""
        with Path(target).open("xb") as output, self.connect() as db:
            count = 0
            for row in db.execute("SELECT payload FROM batches ORDER BY id"):
                payload = bytes(row[0])
                output.write(payload)
                if not payload.endswith(b"\n"):
                    output.write(b"\n")
                count += 1
            output.flush()
            os.fsync(output.fileno())
        return {"batches": count, "sha256": file_digest(target), "path": str(target)}

    def prune(self, archive_days=90):
        if archive_days < 90:
            raise ValueError("Archive retention must be at least 90 days")
        with self.connect() as db:
            cursors = db.execute("SELECT MIN(batch_id) FROM cursors").fetchone()[0]
            if cursors is None:
                return 0
            cutoff = time.time()-archive_days*86400
            expired_bytes = db.execute("SELECT COALESCE(SUM(length(payload)),0) FROM batches WHERE id<=? AND received<?", (cursors, cutoff)).fetchone()[0]
            result = db.execute("DELETE FROM batches WHERE id<=? AND received<?", (cursors, cutoff))
            db.execute("UPDATE totals SET value=value-? WHERE name='retained_bytes'", (expired_bytes,))
            return result.rowcount

    def backup(self, target):
        target = Path(target)
        if target.exists():
            raise FileExistsError(target)
        # Reserve target exclusively before SQLite connects.
        with target.open("xb"):
            pass
        with self.connect() as source, closing(sqlite3.connect(target)) as dest:
            source.backup(dest)
            if dest.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity failure")
        digest = file_digest(target)
        target.with_suffix(target.suffix + ".sha256").write_text(digest + "\n", encoding="ascii")
        return {"sha256": digest, "path": str(target)}


def restore(source, target):
    source, target = Path(source), Path(target)
    expected = source.with_suffix(source.suffix + ".sha256").read_text(encoding="ascii").strip()
    if file_digest(source) != expected:
        raise ValueError("Backup digest mismatch")
    if target.exists() or Path(str(target)+"-wal").exists() or Path(str(target)+"-shm").exists():
        raise FileExistsError("Restore requires a new path and stopped service")
    with closing(sqlite3.connect("file:"+source.as_posix()+"?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Backup integrity failure")
    with target.open("xb") as out, source.open("rb") as inp:
        shutil.copyfileobj(inp, out)
        out.flush()
        os.fsync(out.fileno())


def http_sender(sink):
    url = sink["write_url"]
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        raise ValueError("Use an HTTP(S) URL and separate secret headers")
    if urllib.parse.parse_qs(parsed.query).get("precision") not in (["ns"], ["n"]):
        raise ValueError("Destination write URL must specify precision=ns")
    headers = {"Content-Type": "text/plain; charset=utf-8", **sink.get("headers", {})}
    def send(payload):
        request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=sink.get("timeout_seconds", 10)) as response:
            if not 200 <= response.status < 300:
                raise OSError("Write was not acknowledged")
    return send


def worker(spool, sink, stop):
    spool.register(sink["name"], sink["write_url"])
    send = http_sender(sink)
    rate = sink.get("max_bytes_per_second", 1048576)
    if rate <= 0:
        raise ValueError("Drain rate must be positive")
    while not stop.is_set():
        start = time.monotonic()
        count = spool.deliver_one(sink["name"], send)
        delay = sink.get("retry_seconds", 5) if count < 0 else (1 if count == 0 else max(0, count/rate-(time.monotonic()-start)))
        stop.wait(delay)


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    """Limit stalled/parallel source requests; rejection is an unacknowledged retry."""
    daemon_threads = True

    def __init__(self, address, handler, max_connections=16):
        self.slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(15)
        return connection, address

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_handler(spool, max_body_bytes, token=None, allowed_sources=None):
    networks = [ipaddress.ip_network(value) for value in (allowed_sources or ["127.0.0.1/32", "::1/128"])]
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if not any(ipaddress.ip_address(self.client_address[0]) in network for network in networks):
                self.send_error(403)
                return
            if self.path == "/ping":
                self.send_response(204)
                self.end_headers()
                return
            if self.path != "/health":
                self.send_error(404)
                return
            body = json.dumps(spool.status()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if not any(ipaddress.ip_address(self.client_address[0]) in network for network in networks):
                self.send_error(403)
                return
            if token and self.headers.get("Authorization") != "Token " + token:
                self.send_error(401)
                return
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/write" or urllib.parse.parse_qs(parsed.query).get("precision") not in (["ns"], ["n"]):
                self.send_error(400, "Expected /write?precision=ns")
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= max_body_bytes:
                    raise ValueError("Invalid content length")
                self.connection.settimeout(15)
                payload = self.rfile.read(length)
                if len(payload) != length:
                    raise ValueError("Incomplete body")
                encoding = self.headers.get("Content-Encoding", "identity")
                if encoding != "identity":
                    raise ValueError("Configure uncompressed Telegraf HTTP output")
                spool.accept(payload)
            except BufferError:
                self.send_error(507, "Queue full; retry required")
                return
            except (ValueError, UnicodeError):
                self.send_error(400, "Invalid line protocol or request")
                return
            except (OSError, sqlite3.Error):
                self.send_error(503, "Storage unavailable; retry required")
                return
            self.send_response(204)
            self.end_headers()
    return Handler


def serve(config):
    spool = Spool(config["spool"], config.get("max_spool_payload_bytes", 0), config.get("disk_reserve_bytes", 0))
    stop = threading.Event()
    sinks = config["sinks"]
    names = [sink["name"] for sink in sinks]
    if len(set(names)) != len(names) or "local" not in names:
        raise ValueError("Require unique sink names and local sink")
    threads = []
    for sink in sinks:
        spool.register(sink["name"], sink["write_url"])
        http_sender(sink)  # Fail on invalid configuration before starting receiver.
        if sink.get("max_bytes_per_second", 1048576) <= 0:
            raise ValueError("Drain rate must be positive")
        thread = threading.Thread(target=worker, args=(spool, sink, stop), daemon=True)
        thread.start()
        threads.append(thread)
    server = BoundedHTTPServer((config.get("listen", "127.0.0.1"), config.get("port", 18086)),
                               make_handler(spool, config.get("max_body_bytes", 1048576), config.get("ingest_token"), config.get("allowed_sources")))
    try:
        server.serve_forever()
    finally:
        stop.set()
        server.server_close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("serve", "status", "sizing", "backup", "restore", "prune", "raw-export"))
    parser.add_argument("--config", help="Private runtime JSON for serve")
    parser.add_argument("--spool")
    parser.add_argument("--file")
    parser.add_argument("--retention-days", type=int, help="Explicit archive retention for prune, minimum 90; otherwise read config or use 90")
    args = parser.parse_args()
    if args.action == "serve":
        serve(json.loads(Path(args.config).read_text(encoding="utf-8")))
    elif args.action == "restore":
        restore(args.file, args.spool)
        print(json.dumps({"restored": args.spool}))
    else:
        spool = Spool(args.spool)
        if args.action == "backup":
            result = spool.backup(args.file)
        elif args.action == "raw-export":
            result = spool.raw_export(args.file)
        elif args.action == "prune":
            config = json.loads(Path(args.config).read_text(encoding="utf-8")) if args.config else {}
            days = args.retention_days if args.retention_days is not None else config.get("archive_days", 90)
            result = {"pruned_batches": spool.prune(days), "archive_days": days}
        else:
            result = getattr(spool, args.action)()
        print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
