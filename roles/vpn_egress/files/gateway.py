"""Selective VLESS egress with bounded Telegram probes and country failover."""
import base64
import collections
import concurrent.futures
import dataclasses
import hashlib
import http.server
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid

MAX_SUBSCRIPTION = 1024 * 1024
MAX_NODES = 512
PROBE_URL = "https://api.telegram.org/"


@dataclasses.dataclass
class Node:
    identity: str
    country: str
    outbound: dict


def country_code(label):
    for a, b in zip(label, label[1:]):
        if all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in (a, b)):
            return "".join(chr(ord(c) - 0x1F1E6 + 65) for c in (a, b))
    return "UNKNOWN"


def parse_subscription(body):
    if len(body) > MAX_SUBSCRIPTION:
        raise ValueError("subscription_too_large")
    text = body.decode("utf-8-sig").strip()
    if not text.startswith("vless://"):
        compact = "".join(text.split())
        text = base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True).decode("utf-8-sig")
    nodes = {}
    rejected = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            uri = urllib.parse.urlsplit(line.strip())
            q = {k: v[0] for k, v in urllib.parse.parse_qs(uri.query, keep_blank_values=True).items()}
            if uri.scheme != "vless" or q.get("security") != "tls" or q.get("type") != "ws":
                raise ValueError("unsupported_transport")
            if q.get("allowInsecure", "0").lower() not in ("0", "false"):
                raise ValueError("insecure_tls")
            if q.get("encryption", "none") != "none" or q.get("flow", ""):
                raise ValueError("unsupported_encryption_or_flow")
            credential = str(uuid.UUID(uri.username))
            port = uri.port or 443
            host = uri.hostname
            if not host or not 1 <= port <= 65535:
                raise ValueError("invalid_endpoint")
            values = (host, q.get("sni", host), q.get("host", host), q.get("path", "/"))
            if any(any(ord(c) < 32 or ord(c) == 127 for c in v) for v in values):
                raise ValueError("invalid_control_character")
            tls = {"serverName": values[1], "allowInsecure": False}
            if q.get("fp"):
                if q["fp"] not in ("chrome", "firefox", "safari", "ios", "android", "edge", "random", "randomized"):
                    raise ValueError("unsupported_fingerprint")
                tls["fingerprint"] = q["fp"]
            if q.get("alpn"):
                alpn = q["alpn"].split(",")
                if any(x not in ("h2", "http/1.1") for x in alpn):
                    raise ValueError("unsupported_alpn")
                tls["alpn"] = alpn
            outbound = {
                "tag": "vpn", "protocol": "vless",
                "settings": {"vnext": [{"address": host, "port": port, "users": [{"id": credential, "encryption": "none"}]}]},
                "streamSettings": {"network": "ws", "security": "tls", "tlsSettings": tls,
                                   "wsSettings": {"path": values[3], "headers": {"Host": values[2]}}},
            }
            identity = hashlib.sha256(json.dumps(outbound, sort_keys=True).encode()).hexdigest()[:20]
            nodes[identity] = Node(identity, country_code(urllib.parse.unquote(uri.fragment)), outbound)
        except (ValueError, TypeError, AttributeError):
            rejected += 1
    if not nodes or len(nodes) > MAX_NODES:
        raise ValueError("no_supported_nodes_or_limit_exceeded")
    return list(nodes.values()), rejected


def candidate_order(nodes, previous=None):
    """Try one node per other country before more nodes from the same country."""
    groups = collections.defaultdict(collections.deque)
    for node in nodes:
        if not previous or node.identity != previous.identity:
            groups[node.country].append(node)
    countries = list(groups)
    if previous:
        countries.sort(key=lambda c: c == previous.country)
    ordered = []
    while any(groups.values()):
        for country in countries:
            if groups[country]:
                ordered.append(groups[country].popleft())
    return ordered


def xray_config(node, probe_port, credentials=None):
    inbounds = [{"tag": "probe", "listen": "127.0.0.1", "port": probe_port, "protocol": "socks",
                 "settings": {"auth": "noauth", "udp": False}}]
    if credentials:
        inbounds.extend([
            {"tag": "socks", "listen": "0.0.0.0", "port": 1080, "protocol": "socks",
             "settings": {"auth": "password", "accounts": [credentials], "udp": False}},
            {"tag": "http", "listen": "0.0.0.0", "port": 8080, "protocol": "http",
             "settings": {"accounts": [credentials]}},
        ])
    return {"log": {"loglevel": "none", "access": "none", "dnsLog": False},
            "inbounds": inbounds, "outbounds": [node.outbound]}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


class Worker:
    def __init__(self, node, port, credentials=None):
        self.directory = tempfile.TemporaryDirectory(prefix="node-", dir="/run/vpn-egress")
        config = Path(self.directory.name) / "config.json"
        write_json(config, xray_config(node, port, credentials))
        try:
            self.process = subprocess.Popen(["xray", "run", "-config", str(config)],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            self.directory.cleanup()
            raise
        self.port = port

    def ready(self):
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and self.process.poll() is None:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=.2):
                    return True
            except OSError:
                time.sleep(.1)
        return False

    def healthy(self):
        if self.process.poll() is not None:
            return False
        result = subprocess.run([
            "curl", "--silent", "--noproxy", "", "--proxy", f"socks5h://127.0.0.1:{self.port}",
            "--connect-timeout", "5", "--max-time", "10", "--output", os.devnull,
            "--write-out", "%{http_code}", PROBE_URL,
        ], capture_output=True, timeout=12)
        return result.returncode == 0 and result.stdout.strip() in (b"200", b"302")

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.directory.cleanup()


def probe(node):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    worker = None
    try:
        worker = Worker(node, port)
        return worker.ready() and worker.healthy()
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if worker:
            worker.close()


class State:
    def __init__(self, directory):
        self.path = Path(directory) / "status.json"
        self.lock = threading.Lock()
        try:
            previous = json.loads(self.path.read_text())
        except (OSError, ValueError):
            previous = {}
        self.value = {"state": "starting", "phase": "starting", "country": None,
                      "events": previous.get("events", [])[-100:], "outage_id": previous.get("outage_id")}
        self.update()

    def update(self, **changes):
        with self.lock:
            self.value.update(changes)
            now = int(time.time())
            new = self.value["state"]
            kind = None
            if new == "all_down" and not self.value.get("outage_id"):
                self.value["outage_id"] = str(uuid.uuid4())
                kind = "pool_down"
            elif new == "healthy" and self.value.get("outage_id"):
                kind = "pool_recovered"
            if kind:
                self.value["events"].append({"id": str(uuid.uuid4()), "kind": kind, "at": now,
                                             "outage_id": self.value["outage_id"]})
                self.value["events"] = self.value["events"][-100:]
                print(json.dumps(self.value["events"][-1]), flush=True)
                if kind == "pool_recovered":
                    self.value["outage_id"] = None
            self.value["updated_at"] = now
            write_json(self.path, self.value)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.value))


class StatusHandler(http.server.BaseHTTPRequestHandler):
    state = None

    def do_GET(self):
        value = self.state.snapshot()
        if self.path == "/healthz":
            fresh = time.time() - value["updated_at"] < 90
            code = 200 if fresh and value["state"] == "healthy" else 503
            body = json.dumps({"state": value["state"], "fresh": fresh}).encode()
            content_type = "application/json"
        elif self.path == "/status.json":
            code, body, content_type = 200, json.dumps(value).encode(), "application/json"
        elif self.path == "/":
            code, body, content_type = 200, Path("/app/status.html").read_bytes(), "text/html; charset=utf-8"
        else:
            code, body, content_type = 404, b"Not found", "text/plain"
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def fetch_nodes(url):
    if urllib.parse.urlsplit(url).scheme != "https":
        raise ValueError("https_subscription_required")
    request = urllib.request.Request(url, headers={"User-Agent": "v2raytun"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    with opener.open(request, timeout=20) as response:
        body = response.read(MAX_SUBSCRIPTION + 1)
    return parse_subscription(body)


def choose_worker(nodes, active, credentials, state, stop):
    state.update(state="all_down" if state.snapshot()["state"] == "all_down" else "checking",
                 phase="scanning", country=None, tested_nodes=0, supported_nodes=len(nodes))
    candidates = candidate_order(nodes, active)
    if active and any(n.identity == active.identity for n in nodes):
        candidates.append(active)
    tested = 0
    for start in range(0, len(candidates), 4):
        if stop.is_set():
            return None, None
        batch = candidates[start:start + 4]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(probe, batch))
        tested += len(batch)
        state.update(tested_nodes=tested, phase="scanning")
        for candidate, good in zip(batch, results):
            if not good or stop.is_set():
                continue
            trial = None
            try:
                trial = Worker(candidate, 1081, credentials)
                if trial.ready() and trial.healthy():
                    state.update(state="healthy", phase="monitoring", country=candidate.country)
                    return candidate, trial
            except (OSError, subprocess.SubprocessError):
                pass
            if trial:
                trial.close()
    if not stop.is_set():
        state.update(state="all_down", phase="waiting", country=None, tested_nodes=tested,
                     scan_completed_at=int(time.time()))
    return None, None


def run():
    os.umask(0o077)
    settings = json.loads(Path("/run/secrets/gateway.json").read_text())
    credentials = {"user": settings["proxy_user"], "pass": settings["proxy_password"]}
    if not credentials["user"] or len(credentials["pass"]) < 24:
        raise ValueError("private_proxy_credentials_required")
    state = State("/data")
    StatusHandler.state = state
    server = http.server.ThreadingHTTPServer(("0.0.0.0", 8099), StatusHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    nodes, active, worker = [], None, None
    last_refresh, failures = 0, 0
    cache = Path("/data/private/nodes.json")
    try:
        saved = json.loads(cache.read_text())
        nodes = [Node(**entry) for entry in saved["nodes"]]
        state.update(subscription_received_at=saved["received_at"], subscription_cached=True)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    try:
        while not stop.is_set():
            if time.monotonic() - last_refresh >= 3600 or last_refresh == 0:
                try:
                    fresh, rejected = fetch_nodes(settings["subscription_url"])
                    received_at = int(time.time())
                    write_json(cache, {"received_at": received_at, "nodes": [dataclasses.asdict(n) for n in fresh]})
                    nodes = fresh
                    state.update(subscription_received_at=received_at, subscription_cached=False,
                                 subscription_error=False, supported_nodes=len(nodes), rejected_nodes=rejected,
                                 declared_countries=sorted({n.country for n in nodes}))
                    if active and not any(n.identity == active.identity for n in nodes):
                        worker.close()
                        worker = None
                except Exception:
                    state.update(subscription_error=True)
                last_refresh = time.monotonic()
            if worker:
                try:
                    good = worker.healthy()
                except subprocess.SubprocessError:
                    good = False
                failures = 0 if good else failures + 1
                state.update(state="healthy" if good else "degraded", phase="monitoring", country=active.country)
                if failures < 2:
                    stop.wait(30)
                    continue
                worker.close()
                worker = None
            if not nodes:
                state.update(state="no_nodes", phase="waiting", country=None)
                stop.wait(30)
                last_refresh = 0
                continue
            selected, worker = choose_worker(nodes, active, credentials, state, stop)
            if selected:
                active, failures = selected, 0
            stop.wait(30)
    finally:
        if worker:
            worker.close()
        state.update(state="stopped", phase="stopped")
        server.shutdown()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # Exceptions and child logs can contain endpoints or subscription credentials.
        print(json.dumps({"error_type": type(exc).__name__}), flush=True)
        raise SystemExit(1)
