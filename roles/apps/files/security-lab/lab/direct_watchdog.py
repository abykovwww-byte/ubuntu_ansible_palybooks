"""Trusted PID1 watchdog for the owned direct-worker laboratory container.

Only public lease identity/timing is read. This entrypoint is host-configured,
not an operator or MCP interface. It must run as root in a private PID namespace.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import re
import signal
import stat
import time


LEASE_PATH = Path("/control/lease.json")
MAX_LEASE_BYTES = 4096
WORKER_UID = 1001
LEASE_KEYS = frozenset({"job_id", "generation", "scope_hash", "sequence", "valid_until", "revoked"})


@dataclass(frozen=True)
class WatchDecision:
    ready: bool
    stop_reason: str | None = None


def _finite_number(value):
    try:
        return type(value) in {int, float} and math.isfinite(value)
    except OverflowError:
        return False


class LeaseWatch:
    """Irreversible state machine with monotonic lease and startup deadlines."""

    def __init__(self, job_id, generation, scope_hash, deadline, *, wall_clock=time.time,
                 monotonic=time.monotonic, startup_seconds=5, margin_seconds=1):
        if (not isinstance(job_id, str) or re.fullmatch(r"[a-f0-9]{32}", job_id) is None
                or type(generation) is not int or generation < 1
                or not isinstance(scope_hash, str) or re.fullmatch(r"[a-f0-9]{64}", scope_hash) is None
                or not _finite_number(deadline) or not _finite_number(startup_seconds)
                or not 0 < startup_seconds <= 5 or not _finite_number(margin_seconds)
                or not 0 < margin_seconds <= 6):
            raise ValueError("Invalid watchdog binding")
        self.wall_clock, self.monotonic = wall_clock, monotonic
        wall, mono = wall_clock(), monotonic()
        if not _finite_number(wall) or not _finite_number(mono) or deadline <= wall:
            raise ValueError("Invalid watchdog clock/deadline")
        self.job_id, self.generation, self.scope_hash, self.deadline = job_id, generation, scope_hash, deadline
        self.startup_until = mono + startup_seconds
        self.hard_until = mono + (deadline - wall)
        self.margin = margin_seconds
        self.last_wall, self.last_mono = wall, mono
        self.sequence = 0
        self.lease_until = None
        self.payload = None
        self.stop_reason = None

    def _stop(self, reason):
        self.stop_reason = reason
        return WatchDecision(False, reason)

    def observe(self, payload):
        if self.stop_reason is not None:
            return WatchDecision(False, self.stop_reason)
        wall, mono = self.wall_clock(), self.monotonic()
        if not _finite_number(wall) or not _finite_number(mono):
            return self._stop("invalid_clock")
        if wall < self.last_wall or mono < self.last_mono:
            return self._stop("clock_rollback")
        self.last_wall, self.last_mono = wall, mono
        if mono >= self.hard_until - self.margin or wall >= self.deadline - self.margin:
            return self._stop("deadline")
        # Check the previous lease before considering a replacement. A late
        # renewal cannot resurrect access after the shutdown margin was reached.
        if self.lease_until is not None and mono >= self.lease_until - self.margin:
            return self._stop("lease_expiring")
        if payload is None:
            if self.sequence:
                return self._stop("missing_lease")
            if mono >= self.startup_until:
                return self._stop("startup_timeout")
            return WatchDecision(False)
        if type(payload) is not dict or set(payload) != LEASE_KEYS:
            return self._stop("invalid_lease")
        if (payload["job_id"] != self.job_id or type(payload["generation"]) is not int
                or payload["generation"] != self.generation or payload["scope_hash"] != self.scope_hash):
            return self._stop("foreign_lease")
        if type(payload["revoked"]) is not bool:
            return self._stop("invalid_lease")
        if payload["revoked"]:
            return self._stop("revoked")
        sequence, valid_until = payload["sequence"], payload["valid_until"]
        if (type(sequence) is not int or sequence < 1 or not _finite_number(valid_until)
                or valid_until > self.deadline or valid_until - wall > 30):
            return self._stop("invalid_lease")
        if valid_until <= wall + self.margin:
            return self._stop("lease_expiring")
        if sequence < self.sequence or (sequence == self.sequence and payload != self.payload):
            return self._stop("lease_replay")
        if sequence > self.sequence:
            if self.payload is not None and valid_until < self.payload["valid_until"]:
                return self._stop("lease_replay")
            self.sequence = sequence
            self.payload = dict(payload)
            self.lease_until = min(mono + valid_until - wall, self.hard_until)
        # Identical repeated reads keep the original monotonic expiry.
        return WatchDecision(True)


def read_lease():
    """Read the fixed protected path, rejecting partial/oversized/symlink data."""
    try:
        descriptor = os.open(LEASE_PATH, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        return {}
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LEASE_BYTES:
            return {}
        with os.fdopen(descriptor, "rb") as source:
            descriptor = None
            raw = source.read(MAX_LEASE_BYTES + 1)
        if len(raw) > MAX_LEASE_BYTES:
            return {}
        return json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _is_worker_process(pid):
    """Match only the exact unprivileged UID owned by this worker launcher.

    The dedicated PID namespace contains the trusted root supervisor/installer
    and host-launched curl under UID1001. Reading another UID's /proc/exe link
    would require ptrace access, which this supervisor deliberately lacks.
    Status UID fields are kernel-generated and readable without that privilege.
    """
    try:
        status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        uid_line = next(line for line in status.splitlines() if line.startswith("Uid:"))
        if [int(value) for value in uid_line.split()[1:]] != [WORKER_UID] * 4:
            return False
        return True
    except (OSError, ValueError, StopIteration):
        return False


def kill_worker_curl():
    """Stop host-launched UID1001 processes before PID1 tears down this job."""
    # Refuse execution on the host even if someone invokes this developer file.
    if os.getpid() != 1 or not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise RuntimeError("Watchdog requires root PID1 in its owned container")
    killed = 0
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal() or int(entry.name) == 1:
            continue
        pid = int(entry.name)
        if _is_worker_process(pid):
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except ProcessLookupError:
                pass
    return killed


def main(argv=None):
    parser = argparse.ArgumentParser(description="Host-configured owned-lab PID1 lease watchdog")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--scope-hash", required=True)
    parser.add_argument("--deadline", type=float, required=True)
    args = parser.parse_args(argv)
    if os.getpid() != 1 or not hasattr(os, "geteuid") or os.geteuid() != 0:
        print(json.dumps({"reason": "invalid_runtime"}))
        return 78
    try:
        # The nft installer reserves five seconds for native API latency.
        # Close sockets earlier so FIN/RST still has kernel permission.
        watch = LeaseWatch(args.job_id, args.generation, args.scope_hash, args.deadline, margin_seconds=6)
    except ValueError:
        print(json.dumps({"reason": "invalid_binding"}))
        return 78
    def stop_requested(signum, frame):
        watch._stop("shutdown_requested")
    signal.signal(signal.SIGTERM, stop_requested)
    signal.signal(signal.SIGINT, stop_requested)
    while True:
        if Path('/control/stop').exists():
            watch._stop('shutdown_requested')
        decision = watch.observe(read_lease())
        if decision.stop_reason:
            killed = kill_worker_curl()
            # Keep this namespace/interface alive briefly after closing the
            # worker's sockets. Immediate PID1 exit can discard queued FIN/RST
            # during Docker network teardown. No tool survives this drain; its
            # half-second duration fits inside the one-second early-kill margin.
            time.sleep(.5)
            print(json.dumps({"reason": decision.stop_reason, "killed": killed,
                              "drain_seconds": .5}), flush=True)
            return 0
        time.sleep(.1)


if __name__ == "__main__":
    raise SystemExit(main())
