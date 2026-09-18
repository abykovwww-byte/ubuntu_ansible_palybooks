"""Bounded lab services and short TCP/HTTP/DNS workloads; Python stdlib only."""
import argparse
import concurrent.futures
import http.client
import http.server
import json
import os
import signal
import socket
import socketserver
import struct
import subprocess
import threading
import time

import policy_probe


QUESTION = b"\x03lab\x07invalid\x00\x00\x01\x00\x01"
PAYLOAD = b"ngfw-lab\n"
# Upper bounds in milliseconds, not a random sample. Quantiles are bucket bounds.
BUCKETS = [0.5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]


def dns_answer(data):
    if len(data) != 12 + len(QUESTION) or data[12:] != QUESTION:
        return None
    if data[2] & 0x80 or data[4:12] != b"\x00\x01\x00\x00\x00\x00\x00\x00":
        return None
    # Static answer, no recursion, no upstream resolver, no arbitrary names.
    return (data[:2] + b"\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00" + QUESTION
            + b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\xc0\x00\x02\x01")


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(2)
        try:
            if self.request.recv(64) == PAYLOAD:
                self.request.sendall(PAYLOAD)
        except OSError:
            pass


class HTTPHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/policy-probe/"):
            try:
                payload = policy_probe.reply(self.path, self.client_address, self.connection.getsockname())
            except ValueError:
                self.send_error(400)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except OSError:
                pass
            return
        self.send_response(200 if self.path == "/health" else 404)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(PAYLOAD)
        except OSError:
            pass

    def log_message(self, *_args):
        pass


class DNSHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        answer = dns_answer(data)
        if answer:
            sock.sendto(answer, self.client_address)


class TCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    request_queue_size = 128


def serve():
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: stopped.set())
    policy_receiver = policy_probe.Receiver(json.loads(os.environ.get("NGFW_POLICY_LISTEN_RANGES", "[]")))
    servers = [TCPServer(("0.0.0.0", 9000), TCPHandler),
               TCPServer(("0.0.0.0", 8080), HTTPHandler),
               socketserver.UDPServer(("0.0.0.0", 5353), DNSHandler)]
    iperf = subprocess.Popen(["iperf3", "--server"], stdout=subprocess.DEVNULL)
    try:
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
        while not stopped.wait(0.25):
            if policy_receiver.error:
                raise RuntimeError("policy receiver exited: " + policy_receiver.error)
            if iperf.poll() is not None:
                raise RuntimeError("iperf3 server exited")
    finally:
        policy_receiver.close()
        for server in servers:
            server.shutdown()
            server.server_close()
        iperf.terminate()
        try:
            iperf.wait(timeout=3)
        except subprocess.TimeoutExpired:
            iperf.kill()
            iperf.wait()


def request(kind, target, timeout=2):
    if kind == "http":
        conn = http.client.HTTPConnection(target, 8080, timeout=timeout)
        try:
            conn.request("GET", "/health")
            response = conn.getresponse()
            if response.status != 200 or response.read(1024) != PAYLOAD:
                raise ValueError("unexpected HTTP response")
        finally:
            conn.close()
    elif kind == "short-tcp":
        with socket.create_connection((target, 9000), timeout) as sock:
            sock.sendall(PAYLOAD)
            received = b""
            while len(received) < len(PAYLOAD):
                part = sock.recv(len(PAYLOAD) - len(received))
                if not part:
                    break
                received += part
            if received != PAYLOAD:
                raise ValueError("unexpected TCP response")
    else:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((target, 5353))
            query_id = struct.pack("!H", time.monotonic_ns() & 65535)
            query = query_id + b"\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00" + QUESTION
            sock.send(query)
            if sock.recv(1024) != dns_answer(query):
                raise ValueError("unexpected DNS response")


def workload(kind, target, duration, rate, concurrency):
    stop_at = time.monotonic() + duration

    def worker(index):
        ok, errors, histogram = 0, 0, [0] * len(BUCKETS)
        interval = concurrency / rate
        next_at = time.monotonic() + index / rate
        while next_at < stop_at:
            time.sleep(max(0, next_at - time.monotonic()))
            if time.monotonic() >= stop_at:
                break
            start = time.monotonic()
            try:
                request(kind, target)
                elapsed = (time.monotonic() - start) * 1000
                bucket = next((i for i, b in enumerate(BUCKETS) if elapsed <= b), len(BUCKETS) - 1)
                histogram[bucket] += 1
                ok += 1
            except (OSError, ValueError, http.client.HTTPException):
                errors += 1
            # No catch-up burst when the receiver or scheduler falls behind.
            next_at = max(next_at + interval, time.monotonic())
        return ok, errors, histogram

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        parts = list(pool.map(worker, range(concurrency)))
    seconds = time.monotonic() - started
    ok = sum(p[0] for p in parts)
    errors = sum(p[1] for p in parts)
    hist = [sum(p[2][i] for p in parts) for i in range(len(BUCKETS))]
    quantiles = {}
    for label, q in [("p50", .5), ("p95", .95), ("p99", .99)]:
        cumulative = 0
        quantiles[label] = None
        for bound, count in zip(BUCKETS, hist):
            cumulative += count
            if ok and cumulative >= ok * q:
                quantiles[label] = bound
                break
    return {"kind": kind, "seconds": seconds, "requested_rate": rate,
            "successful_requests": ok, "errors": errors, "requests_per_second": ok / seconds,
            "latency_ms_bucket_upper_bounds": quantiles}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["server", "client", "probe"])
    parser.add_argument("--kind", choices=["short-tcp", "http", "dns"], default="http")
    parser.add_argument("--target", default="10.77.20.10")
    parser.add_argument("--duration", type=int, default=10)
    parser.add_argument("--rate", type=int, default=100)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    socket.inet_aton(args.target)
    if not 1 <= args.duration <= 3600 or not 1 <= args.rate <= 10000 or not 1 <= args.concurrency <= 64:
        parser.error("duration/rate/concurrency outside lab bounds")
    if args.action == "server":
        serve()
    elif args.action == "probe":
        request(args.kind, args.target, timeout=1)
    else:
        print(json.dumps(workload(args.kind, args.target, args.duration, args.rate, args.concurrency)))


if __name__ == "__main__":
    main()
