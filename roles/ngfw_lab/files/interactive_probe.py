#!/usr/bin/env python3
"""Short-lived read-only NGFW probe over an interactively authenticated SSH PTY.

The host is unprivileged. Passwords travel only between the terminal and SSH/sudo;
they are never parsed, stored, or passed as arguments. Only pinned probe variants
can be requested through the private Unix socket. No guest installation.
"""
import argparse
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import signal
import socket
import stat
import struct
import sys
import tempfile
import time

import measurement


GUEST = 'ngfw@10.77.0.20'
REQUEST_SECONDS = 9
MAX_SOURCE = 262144
MAX_REPLY = 2097152
MAX_GUEST_REQUEST = 16384
NONCE = re.compile(r'[0-9a-f]{32}')
DIGEST = re.compile(r'[0-9a-f]{64}')
DEFAULT_NAMES = ['auditd', 'rsyslogd']


class ProbeError(Exception):
    pass


def validate_names(names):
    names = DEFAULT_NAMES if names is None or names == [] else names
    if (not isinstance(names, list) or len(names) > 64 or
            not all(isinstance(x, str) and re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', x) for x in names)):
        raise ProbeError('invalid exact process names')
    return list(names)


def source_allowlist(source, names=None, baseline=None, process_targets=None, process_boot_id=None):
    """Match measurement.source exactly; never parse/evaluate caller Python."""
    if len(source.encode('utf-8')) > MAX_SOURCE or source.count('OPTIONS = {}') != 1:
        raise ProbeError('unexpected deployed probe source')
    if baseline is not None:
        baseline = measurement.validate_busy_poll_baseline(baseline)
    names = validate_names(names)
    if (process_targets is None) != (process_boot_id is None):
        raise ValueError('guest_process_targets and guest_process_boot_id must be set together')
    if process_targets is not None:
        measurement.measure_probe.validate_process_targets(process_targets, names)
        measurement.measure_probe.validate_process_boot_id(process_boot_id)
    allowed = {}
    # A pinned-process broker never accepts a discovery/default-name downgrade.
    for process_names in ((names,) if process_targets is not None else (DEFAULT_NAMES, names)):
        for inventory in (False, True):
            options = {'process_names': process_names, 'inventory': inventory}
            if baseline is not None:
                options['thread_targets'] = [{'pid': row['pid'], 'tid': row['tid']} for row in baseline['cores']]
            if process_targets is not None:
                options['process_targets'] = process_targets
                options['process_boot_id'] = process_boot_id
            rendered = source.replace('OPTIONS = {}', 'OPTIONS = ' + repr(options), 1)
            allowed[hashlib.sha256(rendered.encode('utf-8')).hexdigest()] = options
    return allowed


def load_measurement_config(path):
    with path.open('r', encoding='utf-8') as src:
        raw = src.read(65537)
    if len(raw) > 65536:
        raise ProbeError('measurement config too large')
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ProbeError('measurement config must be an object')
    return measurement.validate(config)


def request_options(request, allowed):
    if (not isinstance(request, dict) or set(request) != {'nonce', 'source_sha256'} or
            not isinstance(request['nonce'], str) or not NONCE.fullmatch(request['nonce']) or
            not isinstance(request['source_sha256'], str) or not DIGEST.fullmatch(request['source_sha256'])):
        raise ProbeError('invalid request')
    if request['source_sha256'] not in allowed:
        raise ProbeError('probe source is not allowlisted; restart broker after approved code/config change')
    return allowed[request['source_sha256']]


def checked_reply(reply, nonce, previous=None):
    if (not isinstance(reply, dict) or set(reply) != {'nonce', 'snapshot'} or reply.get('nonce') != nonce):
        raise ProbeError('reply nonce/schema mismatch')
    row = reply['snapshot']
    if not isinstance(row, dict) or row.get('schema_version') != 1:
        raise ProbeError('invalid probe snapshot')
    for name in ('monotonic', 'wall_time'):
        value = row.get(name)
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ProbeError('invalid probe clock')
    if previous is not None and row['monotonic'] <= previous:
        raise ProbeError('stale or replayed snapshot')
    return row


def recv_line(sock, maximum, deadline):
    data = bytearray()
    while b'\n' not in data:
        left = deadline - time.monotonic()
        if left <= 0:
            raise ProbeError('socket deadline expired')
        sock.settimeout(left)
        chunk = sock.recv(min(65536, maximum + 1 - len(data)))
        if not chunk:
            raise ProbeError('socket closed before reply')
        data.extend(chunk)
        if len(data) > maximum:
            raise ProbeError('socket message too large')
    line, tail = data.split(b'\n', 1)
    if tail:
        raise ProbeError('unexpected trailing socket data')
    return json.loads(line)


def auth_output(data, marker):
    """Accept only the exact random marker and an LF or CRLF terminator.

    PTY output flags can translate newlines. Do not change appliance terminal
    output policy, and do not interpret arbitrary banner text as readiness.
    """
    endings = (marker + b'\n', marker + b'\r\n')
    matches = [(data.find(item), item) for item in endings if item in data]
    if matches:
        position, matched = min(matches, key=lambda pair: pair[0])
        return data[:position], data[position+len(matched):], True
    size = min(len(data), max(map(len, endings))-1)
    while size and not any(data.endswith(item[:size]) for item in endings if size <= len(item)):
        size -= 1
    return (data[:-size], data[-size:], False) if size else (data, b'', False)


def guest_program(source, allowed, marker, lifetime):
    """A fixed, bounded interpreter loop. Incoming messages contain no code."""
    payload = base64.b64encode(source.encode('utf-8')).decode('ascii')
    options = list(allowed.values())
    constants = ('SOURCE = ' + repr(payload) + '\nOPTIONS = ' + repr(options) +
                 '\nLIFETIME = ' + repr(lifetime) + '\nMARKER = ' + repr(marker) +
                 '\nREQUEST_SECONDS = ' + str(REQUEST_SECONDS) + '\nMAX_REPLY = ' + str(MAX_REPLY) +
                 '\nMAX_GUEST_REQUEST = ' + str(MAX_GUEST_REQUEST) + '\n')
    return constants + '''import base64,json,os,re,select,signal,sys,termios,time
if os.geteuid() != 0:
    raise SystemExit(41)
ns = {'__name__': '_ngfw_read_only_probe'}
exec(compile(base64.b64decode(SOURCE), '<pinned-ngfw-probe>', 'exec'), ns)
allowed = OPTIONS
allowed_encoded = [json.dumps(item,sort_keys=True) for item in allowed]
expiry = time.monotonic() + LIFETIME
original = termios.tcgetattr(0)
settings = termios.tcgetattr(0)
settings[3] &= ~(termios.ECHO | termios.ICANON)
settings[6][termios.VMIN] = 1
settings[6][termios.VTIME] = 0
termios.tcsetattr(0, termios.TCSANOW, settings)
signal.setitimer(signal.ITIMER_REAL, LIFETIME)
seen = set()
try:
    print(MARKER, flush=True)
    while time.monotonic() < expiry:
        line = sys.stdin.buffer.readline(MAX_GUEST_REQUEST+1)
        if not line:
            break
        if len(line) > MAX_GUEST_REQUEST or not line.endswith(b'\\n'):
            raise SystemExit(42)
        request = json.loads(line)
        if (not isinstance(request,dict) or set(request) != {'nonce','options'} or
            not isinstance(request['nonce'],str) or
            not re.fullmatch('[0-9a-f]{32}',request['nonce']) or
            not isinstance(request['options'],dict) or
            set(request['options']) not in [set(item) for item in allowed] or
            type(request['options']['inventory']) is not bool or
            json.dumps(request['options'],sort_keys=True) not in allowed_encoded or
            request['nonce'] in seen or len(seen) >= 2000):
            raise SystemExit(43)
        seen.add(request['nonce'])
        signal.setitimer(signal.ITIMER_REAL, min(REQUEST_SECONDS, expiry-time.monotonic()))
        row = ns['snapshot'](request['options'])
        result = json.dumps({'nonce':request['nonce'],'snapshot':row},allow_nan=False)
        if len(result.encode('utf-8')) > MAX_REPLY:
            raise SystemExit(44)
        print(result, flush=True)
        remaining = expiry-time.monotonic()
        if remaining <= 0:
            break
        signal.setitimer(signal.ITIMER_REAL, remaining)
finally:
    termios.tcsetattr(0,termios.TCSANOW,original)
'''


class GuestSession:
    def __init__(self, source, allowed, lifetime):
        self.pid = self.fd = None
        self.pending = bytearray()
        self.previous = None
        self.marker = 'NGFW_PROBE_READY_' + secrets.token_hex(16)
        self.program = guest_program(source, allowed, self.marker, lifetime)
        if len(self.program.encode('utf-8')) > 96000:
            raise ProbeError('deployed probe is too large for the bounded SSH bootstrap')

    def start(self):
        import pty
        remote = 'sudo -p ' + shlex.quote('[NGFW guest sudo] Password: ') + ' -- python3 -u -c ' + shlex.quote(self.program)
        argv = ['ssh', '-tt', '-o', 'StrictHostKeyChecking=yes', '-o', 'ForwardAgent=no',
                '-o', 'ClearAllForwardings=yes', '-o', 'ConnectTimeout=5', '-o', 'ConnectionAttempts=1',
                '-o', 'NumberOfPasswordPrompts=1', '-o', 'PubkeyAuthentication=no',
                '-o', 'PreferredAuthentications=keyboard-interactive,password',
                '-o', 'ServerAliveInterval=5', '-o', 'ServerAliveCountMax=1', '-o', 'LogLevel=ERROR',
                GUEST, remote]
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            try:
                os.execvp(argv[0], argv)
            except OSError:
                os._exit(127)

    def authenticate(self, seconds=120):
        """Only raw terminal forwarding: passwords are not recognized or retained."""
        import termios
        import tty
        if not sys.stdin.isatty():
            raise ProbeError('broker requires an interactive terminal')
        original = termios.tcgetattr(sys.stdin.fileno())
        deadline = time.monotonic() + seconds
        marker = self.marker.encode('ascii')
        output = bytearray()
        try:
            tty.setraw(sys.stdin.fileno())
            while time.monotonic() < deadline:
                ready, _, _ = select.select([self.fd, sys.stdin.fileno()], [], [], min(0.5, deadline-time.monotonic()))
                if self.fd in ready:
                    chunk = os.read(self.fd, 4096)
                    if not chunk:
                        raise ProbeError('SSH closed during authentication')
                    output.extend(chunk)
                    visible, retained, authenticated = auth_output(bytes(output), marker)
                    os.write(sys.stderr.fileno(), visible)
                    output = bytearray(retained)
                    if authenticated:
                        self.pending.extend(retained)
                        return
                if sys.stdin.fileno() in ready:
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data or b'\x03' in data or b'\x04' in data:
                        raise ProbeError('interactive authentication cancelled')
                    os.write(self.fd, data)
                    del data
            raise ProbeError('interactive authentication deadline expired')
        finally:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, original)

    def snapshot(self, nonce, options):
        packet = json.dumps({'nonce': nonce, 'options': options}, separators=(',', ':')).encode() + b'\n'
        os.write(self.fd, packet)
        deadline = time.monotonic() + REQUEST_SECONDS
        while b'\n' not in self.pending:
            left = deadline-time.monotonic()
            if left <= 0 or not select.select([self.fd], [], [], max(0, left))[0]:
                raise ProbeError('guest measurement deadline expired')
            chunk = os.read(self.fd, 65536)
            if not chunk:
                raise ProbeError('guest measurement session closed')
            self.pending.extend(chunk)
            if len(self.pending) > MAX_REPLY:
                raise ProbeError('guest measurement reply too large')
        line, _, rest = self.pending.partition(b'\n')
        self.pending = bytearray(rest)
        reply = json.loads(line)
        row = checked_reply(reply, nonce, self.previous)
        self.previous = row['monotonic']
        return reply

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if self.pid:
            for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                try:
                    waited, _ = os.waitpid(self.pid, os.WNOHANG)
                    if waited:
                        break
                    os.kill(self.pid, signum)
                except (ChildProcessError, ProcessLookupError):
                    break
                deadline = time.monotonic() + 0.5
                while time.monotonic() < deadline:
                    try:
                        waited, _ = os.waitpid(self.pid, os.WNOHANG)
                    except ChildProcessError:
                        waited = self.pid
                    if waited:
                        break
                    time.sleep(0.02)
                if waited:
                    break
            self.pid = None


class PrivateEndpoint:
    """Cleanup removes only paths this instance created with matching identities."""
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix='ngfw-probe-'))
        os.chmod(self.root, 0o700)
        self.identity = self.root.stat().st_dev, self.root.stat().st_ino
        self.owned = {}
        self.socket_path = self.root / 'probe.sock'
        self.argv_path = self.root / 'argv.json'
        self.listener = None

    def remember(self, path):
        info = path.lstat()
        self.owned[path.name] = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_ctime_ns)

    def start(self):
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket_path))
        os.chmod(self.socket_path, 0o600)
        self.remember(self.socket_path)
        self.listener.listen(1)
        argv = ['python3', str(Path(__file__).resolve()), 'client', '--socket', str(self.socket_path), 'python3', '-']
        fd = os.open(self.argv_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as dst:
            json.dump(argv, dst)
        self.remember(self.argv_path)

    def close(self):
        if self.listener is not None:
            self.listener.close()
            self.listener = None
        try:
            root = self.root.lstat()
            if not stat.S_ISDIR(root.st_mode) or (root.st_dev, root.st_ino) != self.identity:
                return
            for name, identity in self.owned.items():
                path = self.root / name
                try:
                    info = path.lstat()
                except FileNotFoundError:
                    continue
                if (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode), info.st_ctime_ns) == identity:
                    path.unlink()
            self.root.rmdir()
        except (FileNotFoundError, OSError):
            # Never recursively delete unexpected or replaced operator files.
            pass


def serve(endpoint, session, allowed, lifetime):
    expiry = time.monotonic() + lifetime
    seen = set()
    while time.monotonic() < expiry:
        if session.pending:
            raise ProbeError('unexpected guest output outside a measurement request')
        ready, _, _ = select.select([endpoint.listener, session.fd], [], [], max(0, min(0.5, expiry-time.monotonic())))
        if session.fd in ready:
            # A fixed guest loop is silent while idle. EOF or any other output is
            # a lost protocol/session, even when no runner request is in flight.
            chunk = os.read(session.fd, 4096)
            raise ProbeError('unexpected idle guest output' if chunk else 'guest session closed while idle')
        if endpoint.listener not in ready:
            continue
        conn, _ = endpoint.listener.accept()
        with conn:
            uid = struct.unpack('3i', conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
            if uid != os.getuid():
                continue
            nonce = None
            try:
                request = recv_line(conn, 2048, time.monotonic()+1)
                options = request_options(request, allowed)
                nonce = request['nonce']
                if nonce in seen or len(seen) >= 2000:
                    raise ProbeError('repeated request or session request limit')
                seen.add(nonce)
            except (OSError, ValueError, ProbeError):
                conn.sendall(b'{"error":"request rejected"}\n')
                continue
            # Any failure of the guest path invalidates the session, not a cached sample.
            reply = session.snapshot(nonce, options)
            conn.settimeout(1)
            conn.sendall(json.dumps(reply, allow_nan=False).encode('utf-8')+b'\n')


def client(socket_path, source):
    if len(source) > MAX_SOURCE:
        raise ProbeError('probe source too large')
    directory, endpoint = socket_path.parent.lstat(), socket_path.lstat()
    if (not stat.S_ISDIR(directory.st_mode) or not stat.S_ISSOCK(endpoint.st_mode) or
            stat.S_IMODE(directory.st_mode) != 0o700 or stat.S_IMODE(endpoint.st_mode) != 0o600 or
            directory.st_uid != os.getuid() or endpoint.st_uid != os.getuid()):
        raise ProbeError('probe socket is not private and owned by this operator')
    nonce = secrets.token_hex(16)
    request = {'nonce': nonce, 'source_sha256': hashlib.sha256(source).hexdigest()}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(REQUEST_SECONDS+1)
        conn.connect(str(socket_path))
        conn.sendall(json.dumps(request).encode('ascii')+b'\n')
        reply = recv_line(conn, MAX_REPLY, time.monotonic()+REQUEST_SECONDS+1)
    return checked_reply(reply, nonce)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    broker = sub.add_parser('broker')
    broker.add_argument('--lifetime', type=int, default=1800)
    selection = broker.add_mutually_exclusive_group()
    selection.add_argument('--process-name', action='append', default=None)
    selection.add_argument('--measurement-config', type=Path)
    consumer = sub.add_parser('client')
    consumer.add_argument('--socket', required=True, type=Path)
    consumer.add_argument('interpreter', choices=['python3'])
    consumer.add_argument('stdin_marker', choices=['-'])
    args = parser.parse_args(argv)
    endpoint = session = None
    old_handlers = {}
    try:
        if not sys.platform.startswith('linux'):
            raise ProbeError('interactive probe requires a Linux host')
        if args.action == 'client':
            row = client(args.socket, sys.stdin.buffer.read(MAX_SOURCE+1))
            print(json.dumps(row, allow_nan=False))
            return 0
        if os.geteuid() == 0:
            raise ProbeError('run broker as the ordinary host operator, never host sudo')
        if not 60 <= args.lifetime <= 1800:
            raise ProbeError('lifetime must be 60..1800 seconds')
        if not sys.stdin.isatty():
            raise ProbeError('broker requires an interactive terminal')
        config = load_measurement_config(args.measurement_config) if args.measurement_config else {}
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGHUP, signal.SIGTERM):
            old_handlers[signum] = signal.signal(signum, interrupt)
        source = Path(__file__).with_name('measure_probe.py').read_text(encoding='utf-8')
        allowed = source_allowlist(source, config.get('process_names', args.process_name),
                                   config.get('guest_busy_poll_baseline'), config.get('guest_process_targets'),
                                   config.get('guest_process_boot_id'))
        session = GuestSession(source, allowed, args.lifetime)
        session.start()
        session.authenticate()
        endpoint = PrivateEndpoint()
        endpoint.start()
        print('Read-only guest session ready. Private argv file: ' + str(endpoint.argv_path), file=sys.stderr, flush=True)
        print('Expires after %d seconds; Ctrl+C closes the guest session.' % args.lifetime, file=sys.stderr, flush=True)
        serve(endpoint, session, allowed, args.lifetime)
        return 0
    except ProbeError as exc:
        print('Interactive probe stopped: ' + str(exc) + '; no cached snapshot returned.', file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError, KeyboardInterrupt):
        print('Interactive probe stopped or unavailable; no cached snapshot returned.', file=sys.stderr, flush=True)
        return 1
    finally:
        try:
            if session is not None:
                session.close()
        finally:
            if endpoint is not None:
                endpoint.close()
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
