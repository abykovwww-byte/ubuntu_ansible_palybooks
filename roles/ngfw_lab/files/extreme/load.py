"""Bounded async NGFW lab traffic; JSONL preserves progress and failures on stdout.

Companion `serve` reuses the existing checked echo/held receiver with an explicit
capacity, and serves HTTP/DNS asynchronously. It does not start iperf3 or change
network configuration. Provision endpoint limits and source IPs through IaC first.
"""
import argparse
import asyncio
from collections import Counter
import contextlib
import hashlib
import ipaddress
import json
import math
from pathlib import Path
import socket
import signal
import struct
import subprocess
import sys
import time

BOUNDS = [.1, .25, .5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]
PAYLOAD = b'ngfw-lab\n'
QUESTION = b'\x03lab\x07invalid\x00\x00\x01\x00\x01'
FRAME = struct.Struct('!8sQIIQ')


def emit(value):
    print(json.dumps(value, allow_nan=False), flush=True)


class Stats:
    def __init__(self, args):
        self.args = args
        self.attempted = self.success = self.skipped = 0
        self.censored = 0
        self.errors = Counter()
        self.hist = [0] * (len(BOUNDS) + 1)
        self.lag_ms = 0
        self.active = self.peak = self.opened = 0
        self.confirmed = set()
        self.last_echo = {}
        self.minimum_active = None
        self.begin = time.time()
        self.measure_begin = None
        self.measure_end = None
        self.stop_reason = None

    def observe(self, elapsed):
        self.success += 1
        index = next((i for i, bound in enumerate(BOUNDS) if elapsed * 1000 <= bound), len(BOUNDS))
        self.hist[index] += 1

    def failure(self, error):
        key = type(error).__name__ + ':' + str(getattr(error, 'errno', None))
        self.errors[key] += 1

    def reset_measure(self):
        self.attempted = self.success = self.skipped = 0
        self.censored = 0
        self.errors.clear()
        self.hist = [0] * (len(BOUNDS) + 1)
        self.lag_ms = 0
        self.confirmed.clear()
        self.minimum_active = self.active
        self.measure_begin = time.time()

    def result(self, final=False):
        end = self.measure_end or time.time()
        seconds = max(.000001, end - (self.measure_begin or self.begin))
        attempted = self.attempted
        result = dict(schema_version=1, record_type='final' if final else 'progress',
                      run_id=self.args.run_id, kind=self.args.kind, time_start=self.begin,
                      measure_start=self.measure_begin, time_end=end, seconds=seconds,
                      requested_rate=self.args.rate, attempted_requests=attempted,
                      successful_requests=self.success, errors=sum(self.errors.values()),
                      error_classes=dict(self.errors), skipped_slots=self.skipped,
                      censored_at_boundary=self.censored,
                      pending_requests=max(0, attempted - self.success - sum(self.errors.values()) - self.censored),
                      attempt_rate=attempted / seconds, success_rate=self.success / seconds,
                      achieved_rate=attempted / seconds,
                      error_rate_pct=100 * sum(self.errors.values()) / attempted if attempted else None,
                      scheduler_lag_max_ms=self.lag_ms,
                      latency_histogram=dict(bounds_ms=BOUNDS, counts=self.hist, cumulative=False,
                                             population='successful_requests', overflow='unbounded'),
                      stop_reason=self.stop_reason)
        if self.args.kind == 'held':
            result.update(requested_sessions=self.args.sessions, opened_sessions=self.opened,
                          active_sessions=self.active, peak_sessions=self.peak,
                          confirmed_measurement_sessions=len(self.confirmed),
                          minimum_active_measurement_sessions=self.minimum_active,
                          stale_echo_sessions=sum(end - stamp > 2 * self.args.heartbeat for stamp in self.last_echo.values()),
                          generator_limited=None, generator_limit_reason='requires resource and receiver read-back')
        else:
            result.update(offered_rate_shortfall=attempted / seconds < .95 * self.args.rate,
                          generator_limited=None,
                          generator_limit_reason='requires direct-control and resource attribution')
        return result


async def request(kind, target, source, ident, timeout=2, ports=(9000, 8080, 5353)):
    local = (source, 0) if source else None
    if kind == 'dns':
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        if local:
            sock.bind(local)
        query = struct.pack('!HHHHHH', ident % 65536, 0x100, 1, 0, 0, 0) + QUESTION
        expected = query[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + QUESTION + b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\xc0\x00\x02\x01'
        try:
            sock.connect((target, ports[2]))
            loop = asyncio.get_running_loop()
            await loop.sock_sendall(sock, query)
            if await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout) != expected:
                raise ValueError('DNS transaction/answer mismatch')
        finally:
            sock.close()
        return
    reader, writer = await asyncio.wait_for(asyncio.open_connection(target,
                         ports[0] if kind == 'short-tcp' else ports[1], local_addr=local), timeout)
    try:
        if kind == 'http':
            writer.write(b'GET /health HTTP/1.1\r\nHost: lab.invalid\r\nConnection: close\r\n\r\n')
            await asyncio.wait_for(writer.drain(), timeout)
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), timeout)
            headers = dict(line.split(b':', 1) for line in head.lower().split(b'\r\n')[1:] if b':' in line)
            if not head.startswith((b'HTTP/1.1 200 ', b'HTTP/1.0 200 ')) or headers.get(b'content-length', b'').strip() != b'9':
                raise ValueError('invalid HTTP response')
        else:
            writer.write(PAYLOAD)
            await asyncio.wait_for(writer.drain(), timeout)
        if await asyncio.wait_for(reader.readexactly(len(PAYLOAD)), timeout) != PAYLOAD:
            raise ValueError('response payload mismatch')
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await asyncio.wait_for(writer.wait_closed(), timeout)


async def request_load(args, stats):
    start = asyncio.get_running_loop().time() + .05
    end = start + args.duration
    stats.measure_begin = time.time() + .05
    period = args.concurrency / args.rate

    async def worker(index):
        slot = start + index / args.rate
        serial = index
        loop = asyncio.get_running_loop()
        while slot < end:
            await asyncio.sleep(max(0, slot - loop.time()))
            now = loop.time()
            if now >= end:
                break
            lag = max(0, now - slot)
            stats.lag_ms = max(stats.lag_ms, lag * 1000)
            if lag >= period:
                skipped = int(lag / period)
                stats.skipped += skipped
                slot += skipped * period
            stats.attempted += 1
            source = args.source_ip[index % len(args.source_ip)] if args.source_ip else None
            before = loop.time()
            try:
                ports = (getattr(args, 'tcp_port', 9000), getattr(args, 'http_port', 8080), getattr(args, 'dns_port', 5353))
                await asyncio.wait_for(request(args.kind, args.target, source, serial, ports=ports), min(6, end - now))
                stats.observe(loop.time() - before)
            except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
                if isinstance(error, asyncio.TimeoutError) and loop.time() >= end:
                    stats.censored += 1
                else:
                    stats.failure(error)
            slot += period
            serial += args.concurrency
    await asyncio.gather(*(worker(i) for i in range(args.concurrency)))
    stats.measure_end = stats.measure_begin + args.duration


async def held_load(args, stats):
    loop = asyncio.get_running_loop()
    start = loop.time() + .05
    measure = asyncio.Event()
    stop = asyncio.Event()
    ident = int.from_bytes(hashlib.sha256(args.run_id.encode()).digest()[:8], 'big')
    ready = 0

    async def connection(cid):
        nonlocal ready
        writer = None
        established = False
        seq = 0
        try:
            await asyncio.sleep(max(0, start + cid / args.rate - loop.time()))
            source = args.source_ip[cid % len(args.source_ip)]
            reader, writer = await asyncio.wait_for(asyncio.open_connection(args.target, getattr(args, 'tcp_port', 9000),
                                   local_addr=(source, 0)), 3)
            stats.active += 1
            stats.peak = max(stats.peak, stats.active)
            established = True
            while not stop.is_set():
                data = FRAME.pack(b'NGFWHOLD', ident, cid, seq, time.time_ns())
                before = loop.time()
                in_measure = measure.is_set()
                if in_measure:
                    stats.attempted += 1
                writer.write(data)
                await asyncio.wait_for(writer.drain(), 2)
                if await asyncio.wait_for(reader.readexactly(FRAME.size), 2) != data:
                    raise ValueError('held echo identity/sequence mismatch')
                if seq == 0:
                    stats.opened += 1
                    ready += 1
                stats.last_echo[cid] = time.time()
                if in_measure:
                    stats.observe(loop.time() - before)
                    stats.confirmed.add(cid)
                seq += 1
                try:
                    await asyncio.wait_for(stop.wait(), args.heartbeat)
                except asyncio.TimeoutError:
                    pass
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError) as error:
            stats.failure(error)
            if seq == 0:
                ready += 1
        finally:
            if established:
                stats.active -= 1
                if measure.is_set() and not stop.is_set():
                    stats.minimum_active = min(stats.minimum_active, stats.active)
            if writer:
                writer.close()
                with contextlib.suppress(OSError, asyncio.TimeoutError):
                    await asyncio.wait_for(writer.wait_closed(), 2)

    tasks = [asyncio.create_task(connection(cid)) for cid in range(args.sessions)]
    try:
        deadline = start + args.sessions / args.rate + 10
        while ready < args.sessions and loop.time() < deadline:
            await asyncio.sleep(.05)
        emit(dict(stats.result(), record_type='buildup', buildup_complete=ready == args.sessions))
        if stats.active != args.sessions:
            stats.stop_reason = 'requested live sessions not established; measurement not started'
            return
        stats.reset_measure()
        measure.set()
        await asyncio.sleep(args.duration)
        stats.measure_end = time.time()
        # Capture final live count before deliberate teardown.
        stats.final_active = stats.active
    finally:
        stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def client(args):
    stats = Stats(args)
    async def progress():
        while True:
            await asyncio.sleep(5)
            emit(stats.result())
    ticker = asyncio.create_task(progress())
    try:
        await (held_load(args, stats) if args.kind == 'held' else request_load(args, stats))
    finally:
        ticker.cancel()
        await asyncio.gather(ticker, return_exceptions=True)
        result = stats.result(final=True)
        if args.kind == 'held':
            result['active_sessions_at_measure_end'] = getattr(stats, 'final_active', None)
        emit(result)


async def serve(args):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'traffic'))
    from held_receiver import Receiver
    held = None
    iperf = None
    server = None
    transport = None
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[sig] = signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    receiver = dict(http_requests=0, http_success=0, http_errors=0, dns_queries=0,
                    dns_valid=0, dns_rejected=0, dns_send_errors=0)
    async def http(reader, writer):
        receiver['http_requests'] += 1
        try:
            data = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 3)
            if not data.startswith(b'GET /health HTTP/1.'):
                raise ValueError('unsupported request')
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\n' + PAYLOAD)
            await asyncio.wait_for(writer.drain(), 2)
            receiver['http_success'] += 1
        except (OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            receiver['http_errors'] += 1
        finally:
            writer.close()
    class DNS(asyncio.DatagramProtocol):
        def connection_made(self, transport):
            self.transport = transport
        def datagram_received(self, data, address):
            receiver['dns_queries'] += 1
            if len(data) == 12 + len(QUESTION) and data[2:12] == b'\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00' and data[12:] == QUESTION:
                answer = data[:2] + b'\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00' + QUESTION + b'\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\xc0\x00\x02\x01'
                self.transport.sendto(answer, address)
                receiver['dns_valid'] += 1
            else:
                receiver['dns_rejected'] += 1
        def error_received(self, error):
            receiver['dns_send_errors'] += 1
    try:
        held = Receiver(host=args.bind, port=args.tcp_port, max_connections=args.max_sessions, max_runs=10000,
                        state_path=args.state_path)
        server = await asyncio.start_server(http, args.bind, args.http_port, backlog=8192)
        transport, _ = await loop.create_datagram_endpoint(DNS, local_addr=(args.bind, args.dns_port))
        if args.with_iperf:
            iperf = subprocess.Popen(['iperf3', '--server', '--bind', args.bind, '--port', str(args.iperf_port)],
                                     stdout=sys.stderr, stderr=sys.stderr)
        while not stop.is_set():
            if held.error:
                raise RuntimeError(held.error)
            if iperf is not None and iperf.poll() is not None:
                raise RuntimeError('iperf3 receiver exited: ' + str(iperf.returncode))
            emit(dict(record_type='receiver_progress', time=time.time(), counters=dict(receiver),
                      resources=process_resources(), held_state_path=args.state_path))
            try:
                await asyncio.wait_for(stop.wait(), 5)
            except asyncio.TimeoutError:
                pass
    finally:
        if transport:
            transport.close()
        if server:
            server.close()
            await server.wait_closed()
        if held:
            held.close()
        stop_child(iperf)
        for sig, handler in previous_handlers.items():
            signal.signal(sig, handler)
        emit(dict(record_type='receiver_final', time=time.time(), counters=receiver,
                  resources=process_resources()))


def stop_child(process):
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def process_resources():
    """Independent process observations; unavailable values remain explicit."""
    result = {'cpu_seconds': time.process_time(), 'fd_count': None, 'max_rss_kib': None}
    try:
        import resource
        result['max_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result['fd_soft_limit'], result['fd_hard_limit'] = resource.getrlimit(resource.RLIMIT_NOFILE)
        result['fd_count'] = len(list(Path('/proc/self/fd').iterdir()))
    except (ImportError, OSError):
        result['resource_gap'] = 'Linux procfs/resource unavailable'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['client', 'serve'])
    parser.add_argument('--kind', choices=['short-tcp', 'http', 'dns', 'held'], default='short-tcp')
    parser.add_argument('--target')
    parser.add_argument('--source-ip', action='append', default=[])
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--rate', type=float, default=5000)
    parser.add_argument('--concurrency', type=int, default=512)
    parser.add_argument('--sessions', type=int, default=20000)
    parser.add_argument('--heartbeat', type=float, default=5)
    parser.add_argument('--run-id', default='unidentified')
    parser.add_argument('--bind', default='0.0.0.0')
    parser.add_argument('--max-sessions', type=int, default=60000)
    parser.add_argument('--state-path', default='/tmp/ngfw-held-server.json')
    parser.add_argument('--with-iperf', action='store_true', help='supervise an iperf3 receiver alongside async services')
    parser.add_argument('--tcp-port', type=int, default=9000)
    parser.add_argument('--http-port', type=int, default=8080)
    parser.add_argument('--dns-port', type=int, default=5353)
    parser.add_argument('--iperf-port', type=int, default=5201)
    args = parser.parse_args()
    if any(not 1 <= port <= 65535 for port in (args.tcp_port, args.http_port, args.dns_port, args.iperf_port)):
        parser.error('invalid service port')
    if not (1 <= args.duration <= 3600 and 0 < args.rate <= 1_000_000 and 1 <= args.concurrency <= 20000
            and 1 <= args.sessions <= 1_000_000 and 1 <= args.heartbeat <= 10 and 1 <= args.max_sessions <= 1_000_000):
        parser.error('outside explicit bounded workload limits')
    if args.action == 'client':
        if not args.target or not ipaddress.ip_address(args.target).is_private:
            parser.error('explicit private lab target required')
        if args.kind == 'held' and not args.source_ip:
            parser.error('held requires provisioned source addresses')
        for address in args.source_ip:
            if ipaddress.ip_address(address).version != 4 or not ipaddress.ip_address(address).is_private:
                parser.error('source address must be private IPv4')
    asyncio.run(client(args) if args.action == 'client' else serve(args))


if __name__ == '__main__':
    main()
