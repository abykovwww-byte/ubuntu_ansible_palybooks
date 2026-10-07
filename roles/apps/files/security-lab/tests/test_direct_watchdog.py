"""Pure fake-clock watchdog checks; no Docker, PID signal or network activity."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SPEC = importlib.util.spec_from_file_location("lab_direct_watchdog", Path(__file__).parents[1] / "lab" / "direct_watchdog.py")
watchdog = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watchdog
SPEC.loader.exec_module(watchdog)

JOB, HASH = "a" * 32, "b" * 64


class Clock:
    wall, mono = 1000.0, 20.0
    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def fixture():
    clock = Clock()
    watch = watchdog.LeaseWatch(JOB, 7, HASH, 1100, wall_clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    return watch, clock


def lease(**changes):
    return {"job_id": JOB, "generation": 7, "scope_hash": HASH, "sequence": 1,
            "valid_until": 1030.0, "revoked": False, **changes}


def test_initial_wait_is_bounded_and_never_restarts():
    watch, clock = fixture()
    assert watch.observe(None) == watchdog.WatchDecision(False)
    clock.advance(4.9)
    assert watch.observe(None).stop_reason is None
    clock.advance(.1)
    assert watch.observe(None).stop_reason == "startup_timeout"
    assert watch.observe(lease(valid_until=1035)).stop_reason == "startup_timeout"


def test_existing_first_valid_lease_ready_and_same_poll_does_not_extend():
    watch, clock = fixture()
    assert watch.observe(lease()).ready
    original = watch.lease_until
    clock.advance(10)
    assert watch.observe(lease()).ready and watch.lease_until == original
    clock.advance(19)
    assert watch.observe(lease()).stop_reason == "lease_expiring"


def test_timely_renewal_extends_only_to_new_deadline():
    watch, clock = fixture()
    watch.observe(lease())
    clock.advance(10)
    assert watch.observe(lease(sequence=2, valid_until=1040)).ready
    assert watch.lease_until == 60
    clock.advance(29)
    assert watch.observe(lease(sequence=3, valid_until=1069)).stop_reason == "lease_expiring"


@pytest.mark.parametrize("mutation", [
    {"sequence": True}, {"sequence": 0}, {"sequence": 1.0}, {"generation": True},
    {"generation": 8}, {"job_id": "c" * 32}, {"scope_hash": "c" * 64},
    {"revoked": 0}, {"valid_until": True}, {"valid_until": float("nan")},
    {"valid_until": float("inf")}, {"valid_until": 10 ** 400},
    {"valid_until": 1031}, {"valid_until": 999}, {"token": "must-not-be-present"},
])
def test_invalid_or_foreign_lease_stops_irreversibly(mutation):
    watch, _ = fixture()
    first = watch.observe(lease(**mutation))
    assert first.stop_reason in {"invalid_lease", "foreign_lease", "lease_expiring"}
    assert not first.ready
    assert watch.observe(lease()).stop_reason == first.stop_reason


@pytest.mark.parametrize("payload", [{}, [], "lease", 0])
def test_bad_file_payload_denied_immediately_even_during_initial_wait(payload):
    watch, _ = fixture()
    assert watch.observe(payload).stop_reason == "invalid_lease"


def test_missing_after_ready_cannot_enter_startup_wait():
    watch, _ = fixture()
    assert watch.observe(lease()).ready
    assert watch.observe(None).stop_reason == "missing_lease"
    assert watch.observe(lease(sequence=2)).stop_reason == "missing_lease"


def test_revocation_before_and_after_ready_is_terminal():
    for initialized in (False, True):
        watch, _ = fixture()
        if initialized:
            watch.observe(lease())
        assert watch.observe(lease(revoked=True)).stop_reason == "revoked"
        assert watch.observe(lease(sequence=2)).stop_reason == "revoked"


def test_lower_sequence_changed_same_sequence_and_shorter_new_sequence_denied():
    for replacement in (lease(sequence=1), lease(sequence=2, valid_until=1029), lease(sequence=3, valid_until=1028)):
        watch, _ = fixture()
        assert watch.observe(lease(sequence=2)).ready
        assert watch.observe(replacement).stop_reason == "lease_replay"


def test_wall_rollback_cannot_extend_existing_monotonic_lease():
    watch, clock = fixture()
    watch.observe(lease())
    original = watch.lease_until
    clock.mono += 10
    clock.wall -= 20
    assert watch.observe(lease(sequence=2, valid_until=1010)).stop_reason == "clock_rollback"
    assert watch.lease_until == original
    clock.wall = 1010
    assert watch.observe(lease(sequence=3, valid_until=1040)).stop_reason == "clock_rollback"


def test_monotonic_rollback_invalid_clock_and_hard_deadline():
    for clock_change, reason in (({"mono": 19}, "clock_rollback"), ({"wall": float("nan")}, "invalid_clock"),
                                 ({"wall": 1099, "mono": 119}, "deadline")):
        watch, clock = fixture()
        for key, value in clock_change.items():
            setattr(clock, key, value)
        assert watch.observe(lease()).stop_reason == reason


def test_deadline_caps_lease_even_within_thirty_seconds():
    clock = Clock()
    watch = watchdog.LeaseWatch(JOB, 7, HASH, 1015, wall_clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    assert watch.observe(lease(valid_until=1016)).stop_reason == "invalid_lease"


@pytest.mark.parametrize("binding", [
    {"generation": True}, {"deadline": float("inf")}, {"deadline": 999},
    {"job_id": "../job"}, {"scope_hash": "bad"}, {"startup_seconds": 6}, {"margin_seconds": 0},
    {"margin_seconds": 6.0001}, {"margin_seconds": 7}, {"margin_seconds": True},
])
def test_invalid_binding_not_accepted(binding):
    clock = Clock()
    args = {"job_id": JOB, "generation": 7, "scope_hash": HASH, "deadline": 1100,
            "wall_clock": lambda: clock.wall, "monotonic": lambda: clock.mono, **binding}
    with pytest.raises(ValueError):
        watchdog.LeaseWatch(**args)


def test_read_fixed_lease_path_rejects_missing_malformed_and_oversized(tmp_path, monkeypatch):
    path = tmp_path / "lease.json"
    monkeypatch.setattr(watchdog, "LEASE_PATH", path)
    assert watchdog.read_lease() is None
    path.write_text(json.dumps(lease()), encoding="utf-8")
    assert watchdog.read_lease() == lease()
    path.write_text("{partial", encoding="utf-8")
    assert watchdog.read_lease() == {}
    path.write_bytes(b" " * 4097)
    assert watchdog.read_lease() == {}


def test_watchdog_refuses_host_process_killing(monkeypatch):
    monkeypatch.setattr(watchdog.os, "getpid", lambda: 42)
    with pytest.raises(RuntimeError, match="root PID1"):
        watchdog.kill_worker_curl()


def test_six_second_margin_closes_before_installer_reserved_kernel_expiry():
    clock = Clock()
    watch = watchdog.LeaseWatch(JOB, 7, HASH, 1100, wall_clock=lambda: clock.wall,
                               monotonic=lambda: clock.mono, margin_seconds=6)
    assert watch.observe(lease()).ready
    clock.advance(23.99)
    assert watch.observe(lease()).ready
    clock.advance(.01)
    assert watch.observe(lease()).stop_reason == 'lease_expiring'
    assert clock.wall == 1024 and clock.wall < 1030 - 5
    assert watch.observe(lease(sequence=2, valid_until=1050)).stop_reason == 'lease_expiring'


def test_six_second_margin_caps_hard_deadline_and_short_lease_without_starting():
    clock = Clock()
    watch = watchdog.LeaseWatch(JOB, 7, HASH, 1010, wall_clock=lambda: clock.wall,
                               monotonic=lambda: clock.mono, margin_seconds=6)
    assert watch.observe(lease(valid_until=1010)).ready
    clock.advance(4)
    assert watch.observe(lease(valid_until=1010)).stop_reason == 'deadline'
    clock = Clock()
    watch = watchdog.LeaseWatch(JOB, 7, HASH, 1100, wall_clock=lambda: clock.wall,
                               monotonic=lambda: clock.mono, margin_seconds=6)
    assert watch.observe(lease(valid_until=1006)).stop_reason == 'lease_expiring'


@pytest.mark.parametrize('uid_line,expected', [
    ('Uid:\t1001\t1001\t1001\t1001', True),
    ('Uid:\t0\t0\t0\t0', False), ('Uid:\t1000\t1000\t1000\t1000', False),
    ('Uid:\t1001\t0\t1001\t1001', False), ('Uid:\t1001\t1001\t1001', False),
    ('Uid:\t1001\t1001\t1001\t1001\t1001', False),
    ('Uid:\tbad\t1001\t1001\t1001', False), ('Name:\tcurl', False),
])
def test_worker_identification_requires_exact_four_uid_fields_without_proc_exe(monkeypatch, uid_line, expected):
    monkeypatch.setattr(watchdog.Path, 'read_text', lambda *args, **kwargs: uid_line + '\nCapEff:\t0000000000000000\n')
    def denied_exe(*args, **kwargs):
        pytest.fail('Root supervisor must not need cross-UID /proc/exe or SYS_PTRACE')
    monkeypatch.setattr(watchdog.os, 'readlink', denied_exe)
    assert watchdog._is_worker_process(42) is expected


def test_disappeared_process_is_not_matched(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError()
    monkeypatch.setattr(watchdog.Path, 'read_text', missing)
    assert watchdog._is_worker_process(42) is False


def test_private_root_pid1_kills_only_exact_worker_uid_and_tolerates_exit_race(monkeypatch):
    monkeypatch.setattr(watchdog.os, 'getpid', lambda: 1)
    monkeypatch.setattr(watchdog.os, 'geteuid', lambda: 0, raising=False)
    monkeypatch.setattr(watchdog.signal, 'SIGKILL', 9, raising=False)
    monkeypatch.setattr(watchdog.Path, 'iterdir', lambda self: [Path('/proc') / value for value in ('1', '42', '43', '44', '45', 'self')])
    def status(path, *args, **kwargs):
        uid = 1001 if path.parent.name in {'42', '43'} else 0 if path.parent.name == '44' else 1000
        return 'Uid:\t' + '\t'.join([str(uid)] * 4) + '\n'
    monkeypatch.setattr(watchdog.Path, 'read_text', status)
    def denied_exe(*args, **kwargs):
        raise PermissionError('Cross-UID proc exe denied without ptrace')
    monkeypatch.setattr(watchdog.os, 'readlink', denied_exe)
    signals = []
    def kill(pid, signal):
        signals.append((pid, signal))
        if pid == 43:
            raise ProcessLookupError()
    monkeypatch.setattr(watchdog.os, 'kill', kill)
    assert watchdog.kill_worker_curl() == 1
    assert signals == [(42, watchdog.signal.SIGKILL), (43, watchdog.signal.SIGKILL)]


def test_nonroot_pid1_cannot_signal_worker(monkeypatch):
    monkeypatch.setattr(watchdog.os, 'getpid', lambda: 1)
    monkeypatch.setattr(watchdog.os, 'geteuid', lambda: 1001, raising=False)
    with pytest.raises(RuntimeError, match='root PID1'):
        watchdog.kill_worker_curl()


def test_actual_main_accepts_six_second_margin_and_reaches_lease_shutdown(monkeypatch, capsys):
    clock = Clock()
    monkeypatch.setattr(watchdog.os, 'getpid', lambda: 1)
    monkeypatch.setattr(watchdog.os, 'geteuid', lambda: 0, raising=False)
    original_watch = watchdog.LeaseWatch
    margins = []
    def configured(*args, **kwargs):
        margins.append(kwargs['margin_seconds'])
        return original_watch(*args, wall_clock=lambda: clock.wall, monotonic=lambda: clock.mono, **kwargs)
    monkeypatch.setattr(watchdog, 'LeaseWatch', configured)
    monkeypatch.setattr(watchdog.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(watchdog.Path, 'exists', lambda path: False)
    monkeypatch.setattr(watchdog, 'read_lease', lambda: lease())
    sleeps = []
    def sleep(seconds):
        sleeps.append(seconds)
        clock.advance(1 if seconds == .1 else seconds)
    monkeypatch.setattr(watchdog.time, 'sleep', sleep)
    calls = []
    monkeypatch.setattr(watchdog, 'kill_worker_curl', lambda: calls.append(clock.wall) or 1)
    assert watchdog.main(['--job-id', JOB, '--generation', '7', '--scope-hash', HASH, '--deadline', '1100']) == 0
    assert margins == [6] and calls == [1024]
    assert sleeps[-1] == .5 and clock.wall == 1024.5 and clock.wall < 1030 - 5
    assert json.loads(capsys.readouterr().out) == {'reason': 'lease_expiring', 'killed': 1, 'drain_seconds': .5}
