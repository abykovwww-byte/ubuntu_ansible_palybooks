"""Fake Docker/subprocess protocol tests; no actual worker/network qualification."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace

import pytest

from security_lab.contracts import Denied, Scope, digest
from security_lab.controller import Controller
from security_lab.direct_policy import compile_direct_execution
from security_lab import direct_worker as worker
from security_lab.dispatcher import Dispatcher
from security_lab.http_policy import Lease
from security_lab.tool_catalog import CurlCheck, build_check
from test_direct_readback import document
from test_dispatcher import NOW, SPEC, lab_scope

OWNER = "sl-direct-012345abcdef"
IMAGE = "sha256:" + "a" * 64


class DockerDouble:
    def __init__(self):
        self.calls = []
        self.network = {"Labels": {worker.LABEL: OWNER}, "Internal": True, "EnableIPv6": True,
                        "IPAM": {"Config": [{"Subnet": "172.31.64.0/24"}, {"Subnet": "fd00:64::/64"}]}}
        self.containers = {}
        self.leftovers = ""
        self.image = IMAGE
        self.backend = None
        self.membership_ms = None
        self.corrupt_table = None
        self.inspect_failure = False
        self.list_failure = False
        self.namespace_changed = False
        self.initial_policy_changed = False
        self.wait_failure = None
        self.owner_changes_after_wait = False

    def __call__(self, *args, input=None, check=True, timeout=5):
        self.calls.append((args, input, timeout))
        out, code = "", 0
        if args[:2] == ("network", "inspect"):
            out = json.dumps([self.network])
        elif args[:2] == ("container", "inspect"):
            if self.inspect_failure or args[2] not in self.containers:
                code = 1
            else:
                out = json.dumps([self.containers[args[2]]])
        elif args[:2] == ("container", "ls"):
            if self.list_failure:
                code = 1
            out = "\n".join(self.containers)
        elif args[:2] == ("network", "ls"):
            if self.list_failure:
                code = 1
            out = OWNER + "-net"
        elif args[0] == "ps":
            out = self.leftovers
        elif args[:2] == ("image", "inspect"):
            out = self.image
        elif args[0] == "run" and "--version" in args:
            out = "curl 8.99 fixture-fake-only\n"
        elif args[0] == "run":
            name = args[args.index("--name") + 1]
            mounts = [args[i + 1] for i, value in enumerate(args) if value == "--mount"]
            self.containers[name] = {
                "Config": {"Labels": {worker.LABEL: OWNER, worker.WORKER_LABEL: "true"}},
                "State": {"Running": True}, "NetworkSettings": {"Networks": {}},
                "HostConfig": {"ReadonlyRootfs": True},
                "Mounts": [{"RW": False, "Destination": next(p[4:] for p in value.split(",") if p.startswith("dst="))}
                           for value in mounts]}
            out = name
        elif args[:2] == ("network", "connect"):
            self.containers[args[-1]]["NetworkSettings"]["Networks"] = {OWNER + "-net": {}}
        elif args[:2] == ("network", "disconnect"):
            assert args[2] == "none"
        elif args[:2] == ("container", "rm"):
            self.containers.pop(args[-1], None)
        elif args[0] == "exec" and "/usr/bin/cat" in args:
            out = "Uid:\t1001\t1001\t1001\t1001\nCapEff:\t0000000000000000\n"
        elif args[0] == 'exec' and '/usr/bin/readlink' in args:
            attached = bool(self.containers[args[1]]['NetworkSettings']['Networks'])
            out = 'net:[5678]\n' if attached and self.namespace_changed else 'net:[1234]\n'
        elif args[0] == "exec" and "/usr/sbin/nft" in args:
            if input and "add element" in input:
                self.membership_ms = int(input.rsplit("timeout ", 1)[1].split("ms", 1)[0])
            if "-j" in args:
                table = document(self.backend.execution)
                for entry in table["nftables"]:
                    if "set" in entry and entry["set"].get("elem"):
                        if self.membership_ms is None:
                            entry['set']['elem'] = []
                            continue
                        elem = entry["set"]["elem"][0]["elem"]
                        elem["timeout"] = self.membership_ms // 1000
                        elem["expires"] = max(1, (self.membership_ms - 1) // 1000)
                if self.corrupt_table and self.membership_ms is not None:
                    self.corrupt_table(table)
                if self.initial_policy_changed and self.membership_ms is None and self.containers[args[1]]['NetworkSettings']['Networks']:
                    table['nftables'] = []
                out = json.dumps(table)
        elif args[0] == "wait":
            assert timeout == 1
            if self.wait_failure == 'timeout':
                raise subprocess.TimeoutExpired(['docker', *args], timeout)
            if self.wait_failure == 'error':
                code = 1
            else:
                self.containers[args[1]]['State']['Running'] = False
                out = '137\n' if self.wait_failure == 'nonzero-exit' else '0\n'
                if self.owner_changes_after_wait:
                    self.containers[args[1]]['Config']['Labels'][worker.LABEL] = 'another-owner'
        elif args[0] in {"kill", "logs"}:
            pass
        else:
            pytest.fail(f"Unexpected fake Docker protocol call: {args}")
        if check and code:
            raise Denied("Fake Docker daemon unavailable")
        return SimpleNamespace(returncode=code, stdout=out, stderr="fixture fake error" if code else "")


@pytest.fixture
def host(tmp_path, monkeypatch):
    # Only the direct_worker module sees Linux metadata. Host OS and asyncio
    # platform remain untouched; no real subprocess/network entry is permitted.
    monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="linux"))
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1001, raising=False)
    original_stat = Path.stat
    def owned_stat(path, *args, **kwargs):
        value = original_stat(path, *args, **kwargs)
        if path == tmp_path and kwargs.get("follow_symlinks", True):
            fields = list(value)
            fields[4] = 1001
            return os.stat_result(fields)
        return value
    monkeypatch.setattr(Path, "stat", owned_stat)
    monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: NOW))
    def prohibited(*args, **kwargs):
        pytest.fail("Fake direct-worker test attempted real subprocess/DNS")
    monkeypatch.setattr(worker.subprocess, "run", prohibited)
    monkeypatch.setattr(socket, "getaddrinfo", prohibited)
    fake = DockerDouble()
    monkeypatch.setattr(worker, "docker", fake)
    (tmp_path / "fixture").mkdir()
    cert = tmp_path / "fixture" / "cert.pem"
    cert.write_text("public fixture certificate placeholder", encoding="utf-8")
    def construct(**overrides):
        arguments = dict(image_digest=IMAGE, root=tmp_path, owner=OWNER,
                         network=OWNER + "-net", trusted_fixture_certificate=cert)
        arguments.update(overrides)
        backend = worker.DockerDirectBackend(**arguments)
        fake.backend = backend
        return backend
    return fake, construct, tmp_path


def binding(backend, seconds=20):
    scope = Scope.model_validate(lab_scope())
    plan = build_check(CurlCheck(url=SPEC["url"], seconds=seconds), scope, stage="checks", now=NOW)
    backend.bind_plan(plan)
    job = {"id": "a" * 32, "generation": 2, "scope_hash": digest(scope.model_dump(mode="json")), "deadline": NOW + seconds}
    policy = backend.policy(job, scope)
    lease = Lease(job_id=job["id"], generation=2, scope_hash=job["scope_hash"], sequence=1,
                  valid_until=NOW + min(20, seconds))
    return plan, policy, lease


def test_constructor_qualifies_only_fixed_host_inventory_and_private_cli_probe(host):
    fake, construct, _ = host
    backend = construct()
    assert backend.admission_ready() and backend.zero_network is False
    assert backend.image_digest == IMAGE
    version_probe = next(args for args, _, _ in fake.calls if "--version" in args)
    assert version_probe[version_probe.index("--network") + 1] == "none"
    assert version_probe[version_probe.index("--cap-drop") + 1] == "ALL"


@pytest.mark.parametrize("override", [{"image_digest": "latest"}, {"image_digest": "sha256:" + "x" * 64},
                                      {"owner": "other-owner"}, {"network": "other-net"}])
def test_constructor_rejects_nonimmutable_or_unowned_model_like_inputs(host, override):
    _, construct, _ = host
    with pytest.raises(Denied):
        construct(**override)


@pytest.mark.parametrize("defect", ["nonlinux", "root", "network-owner", "external", "ipv4-only", "subnet", "leftover", "image-mismatch", "wrong-certificate"])
def test_constructor_rejects_unqualified_host_network_image_or_leftovers(host, monkeypatch, defect):
    fake, construct, root = host
    override = {}
    if defect == "nonlinux":
        monkeypatch.setattr(worker, "sys", SimpleNamespace(platform="win32"))
    elif defect == "root":
        monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    elif defect == "network-owner":
        fake.network["Labels"][worker.LABEL] = "another-run"
    elif defect == "external":
        fake.network["Internal"] = False
    elif defect == "ipv4-only":
        fake.network["EnableIPv6"] = False
    elif defect == "subnet":
        fake.network["IPAM"]["Config"][0]["Subnet"] = "172.31.63.0/24"
    elif defect == "leftover":
        fake.leftovers = "worker-from-old-run"
    elif defect == "image-mismatch":
        fake.image = "sha256:" + "b" * 64
    else:
        wrong = root / "wrong.pem"
        wrong.write_text("wrong public file", encoding="utf-8")
        override["trusted_fixture_certificate"] = wrong
    with pytest.raises(Denied):
        construct(**override)


def test_install_uses_readonly_control_root_supervisor_and_unprivileged_tool_readback(host):
    fake, construct, _ = host
    backend = construct()
    plan, policy, lease = binding(backend)
    ack = asyncio.run(backend.install(policy, lease))
    assert ack["policy_hash"] == digest(policy.model_dump(mode="json"))
    assert backend.execution == compile_direct_execution(plan, policy, lease, now=NOW)
    run = next(args for args, _, _ in fake.calls if args[0] == "run" and "--name" in args)
    assert run[run.index("--network") + 1] == "none" and "--read-only" in run
    calls = [args for args, _, _ in fake.calls]
    initial = next(i for i, (args, body, _) in enumerate(fake.calls) if body and 'add table' in body)
    detached = next(i for i, args in enumerate(calls) if args[:2] == ('network', 'disconnect'))
    attached = next(i for i, args in enumerate(calls) if args[:2] == ('network', 'connect'))
    assert initial < detached < attached
    caps = [run[i + 1] for i, value in enumerate(run) if value == "--cap-add"]
    assert "NET_ADMIN" in caps and "KILL" in caps
    assert run[run.index("--entrypoint") + 1] == "/usr/local/bin/python"
    assert "/app/lab/direct_watchdog.py" in run
    mounts = [run[i + 1] for i, value in enumerate(run) if value == "--mount"]
    assert all(value.endswith(",readonly") for value in mounts)
    assert any("dst=/control,readonly" in value for value in mounts)
    assert (backend.control / "lease.json").read_text("utf-8") == lease.model_dump_json()
    assert "token" not in (backend.control / "lease.json").read_text("utf-8")
    status = next(args for args, _, _ in fake.calls if "/proc/self/status" in args)
    assert "--reuid=1001" in status and "--bounding-set=-all" in status


def test_daemon_failure_is_uncertainty_not_closed_worker(host):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    original = backend.worker
    fake.inspect_failure = fake.list_failure = True
    with pytest.raises(Denied):
        backend._close_native()
    assert backend.poisoned and backend.worker == original and original in fake.containers
    assert backend.closed_workers == []


def test_close_waits_for_pid1_drain_before_logs_and_namespace_removal(host):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    original = backend.worker
    before = len(fake.calls)
    asyncio.run(backend.close(lease.model_copy(update={'revoked': True, 'sequence': 2})))
    calls = fake.calls[before:]
    commands = [args for args, _, _ in calls]
    terminated = next(i for i, args in enumerate(commands) if args[0] == 'kill')
    waited = next(i for i, args in enumerate(commands) if args[0] == 'wait')
    logged = next(i for i, args in enumerate(commands) if args[0] == 'logs')
    removed = next(i for i, args in enumerate(commands) if args[:2] == ('container', 'rm'))
    assert terminated < waited < logged < removed
    assert commands[waited] == ('wait', original) and calls[waited][2] == 1
    assert any(args[:2] == ('container', 'inspect') for args in commands[logged + 1:removed])
    assert not fake.containers and backend.worker is None
    assert backend.closed_workers == [original] and not backend.poisoned


@pytest.mark.parametrize('failure', ['timeout', 'error', 'nonzero-exit'])
def test_wait_failure_forces_proven_removal_but_never_reports_graceful_close(host, failure):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    original = backend.worker
    fake.wait_failure = failure
    with pytest.raises(Denied, match='not confirmed before forced cleanup'):
        asyncio.run(backend.close(lease.model_copy(update={'revoked': True, 'sequence': 2})))
    assert backend.poisoned and not backend.admission_ready()
    assert backend.worker is None and original not in fake.containers
    assert backend.closed_workers == [original]  # Inventory-confirmed removal only.
    assert any(args[:2] == ('container', 'rm') for args, _, _ in fake.calls)


def test_close_rechecks_ownership_after_wait_before_forced_remove(host):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    original = backend.worker
    fake.owner_changes_after_wait = True
    before = len(fake.calls)
    with pytest.raises(Denied, match='not owned'):
        asyncio.run(backend.close(lease.model_copy(update={'revoked': True, 'sequence': 2})))
    assert backend.poisoned and backend.worker == original and original in fake.containers
    assert backend.closed_workers == []
    assert not any(args[:2] == ('container', 'rm') for args, _, _ in fake.calls[before:])


def test_failed_inspect_with_successful_inventory_still_present_cannot_claim_absence(host):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    fake.inspect_failure = True
    with pytest.raises(Denied, match="still exists"):
        backend._close_native()
    assert backend.worker in fake.containers and backend.poisoned


@pytest.mark.parametrize("sequence", [1, 3, None])
def test_renew_must_advance_exact_sequence_and_expiry_cannot_resurrect(host, monkeypatch, sequence):
    _, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend, seconds=40)
    asyncio.run(backend.install(policy, lease))
    if sequence is not None:
        with pytest.raises(Denied, match="sequence"):
            asyncio.run(backend.renew(lease.model_copy(update={"sequence": sequence, "valid_until": NOW + 25})))
    else:
        monkeypatch.setattr(worker, "time", SimpleNamespace(time=lambda: NOW + 19))
        with pytest.raises(Denied, match="Expired"):
            asyncio.run(backend.renew(lease.model_copy(update={"sequence": 2, "valid_until": NOW + 25})))
    assert backend.worker is None  # wrapper closes on every renewal failure


def test_forged_plan_fails_before_any_container_is_started(host):
    fake, construct, _ = host
    backend = construct()
    plan, policy, lease = binding(backend)
    backend.command = plan.model_copy(update={"argv": plan.argv[:-2] + ("--insecure",) + plan.argv[-2:]})
    before = len(fake.calls)
    with pytest.raises(Denied):
        asyncio.run(backend.install(policy, lease))
    assert not fake.containers and len(fake.calls) == before


def test_run_uses_exact_pinned_curl_under_uid1001_empty_environment_without_tool_caps(host, monkeypatch):
    _, construct, _ = host
    backend = construct()
    plan, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    calls = []
    class Stream:
        def __init__(self, value):
            self.value = value
        async def read(self, limit):
            value, self.value = self.value, b''
            return value
    class Process:
        returncode = 0
        stdout = Stream(b'HTTP/1.1 200 OK\r\n\r\nfixture fake process')
        stderr = Stream(b'')
        async def wait(self):
            return self.returncode
    async def spawn(*args, **kwargs):
        calls.append(args)
        return Process()
    monkeypatch.setattr(worker.asyncio, 'create_subprocess_exec', spawn)
    result = asyncio.run(backend.run(plan))
    assert result.exit_code == 0 and 'fixture fake process' in result.stdout
    args = calls[0]
    assert args[:3] == ('/usr/bin/docker', 'exec', backend.worker)
    assert '--reuid=1001' in args and '--regid=1001' in args and '--bounding-set=-all' in args
    env_index = args.index('/usr/bin/env')
    assert args[env_index + 1:env_index + 5] == ('-i', 'PATH=/usr/bin:/bin', 'HOME=/nonexistent', 'LC_ALL=C')
    assert args[env_index + 5:] == backend.execution.argv
    assert '--noproxy' in args and args[args.index('--noproxy') + 1] == '*'
    assert '--resolve' in args and args[args.index('--resolve') + 1] == 'fixture.example.test:8443:172.31.64.3'
    assert '--insecure' not in args and '--proxy' not in args and '--cacert' not in args
    assert backend.execution_receipts[0]['tls_interception'] is False


def test_run_cannot_change_installed_plan_or_enable_extra_flags(host, monkeypatch):
    _, construct, _ = host
    backend = construct()
    plan, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    async def prohibited(*args, **kwargs):
        pytest.fail('Mismatched plan must not spawn a curl process')
    monkeypatch.setattr(worker.asyncio, 'create_subprocess_exec', prohibited)
    modified = plan.model_copy(update={'argv': plan.argv[:-2] + ('--insecure',) + plan.argv[-2:]})
    with pytest.raises(Denied, match='exact plan'):
        asyncio.run(backend.run(modified))


def test_close_binding_failure_is_not_a_worker_closure_receipt(host):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    asyncio.run(backend.install(policy, lease))
    with pytest.raises(Denied, match='binding mismatch'):
        asyncio.run(backend.close(lease.model_copy(update={'generation': 3, 'revoked': True})))
    assert backend.poisoned and backend.worker in fake.containers and backend.closed_workers == []


@pytest.mark.parametrize('defect', ['namespace_changed', 'initial_policy_changed'])
def test_attachment_cannot_replace_namespace_or_initial_drop_policy(host, defect):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    setattr(fake, defect, True)
    with pytest.raises(Denied, match='changed namespace or initial policy'):
        asyncio.run(backend.install(policy, lease))
    assert backend.poisoned and backend.worker is None and not backend.acks


@pytest.mark.parametrize('defect', ['missing-timeout', 'blanket-accept'])
def test_installer_refuses_static_or_broad_nft_readback_and_closes_worker(host, defect):
    fake, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    def corrupt(table):
        if defect == 'missing-timeout':
            obj = next(entry['set'] for entry in table['nftables'] if 'set' in entry and entry['set'].get('elem'))
            del obj['elem'][0]['elem']['timeout']
        else:
            table['nftables'].append({'rule': {'family': 'inet', 'table': 'security_lab_direct',
                                             'chain': 'output', 'expr': [{'accept': None}]}})
    fake.corrupt_table = corrupt
    with pytest.raises(Denied):
        asyncio.run(backend.install(policy, lease))
    assert backend.poisoned and backend.worker is None and backend.acks == [] and not fake.containers


def impersonating_subclass():
    class Impersonator(worker.DockerDirectBackend):
        def admission_ready(self):
            return True
    return object.__new__(Impersonator)


@pytest.mark.parametrize("backend_factory", [
    lambda: SimpleNamespace(lab_only=True, zero_network=False, admission_ready=lambda: True),
    impersonating_subclass,
])
def test_direct_admission_cannot_be_claimed_by_flags_or_subclass(tmp_path, backend_factory):
    controller = Controller(tmp_path, "fixture", clock=lambda: NOW)
    try:
        case = controller.create_case("fixture.example.test")["id"]
        manifest = controller.propose(case, "scope", lab_scope())
        controller.activate_from_task(case, manifest["id"], "scope")
        with pytest.raises(Denied, match="not yet qualified"):
            asyncio.run(Dispatcher(controller, backend_factory()).execute(case, SPEC))
        assert controller.get_case(case)["jobs"] == [] and controller.get_case(case)["spent"] == 0
    finally:
        controller.close()


def test_exact_direct_backend_requires_host_resolver_before_job(host):
    _, construct, root = host
    backend = construct()
    controller = Controller(root / "controller", "fixture", clock=lambda: NOW)
    try:
        case = controller.create_case("fixture.example.test")["id"]
        manifest = controller.propose(case, "scope", lab_scope())
        controller.activate_from_task(case, manifest["id"], "scope")
        with pytest.raises(Denied, match="host DNS"):
            asyncio.run(Dispatcher(controller, backend).execute(case, SPEC))
        assert controller.get_case(case)["jobs"] == [] and controller.get_case(case)["spent"] == 0
    finally:
        controller.close()


@pytest.mark.parametrize("method", ["install", "renew"])
def test_repeated_cancellation_joins_native_operation_before_cleanup(host, monkeypatch, method):
    _, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    order = []
    def native(*args):
        entered.set()
        assert release.wait(3)
        order.append("native_completed")
        completed.set()
        return {}
    def closed():
        assert completed.is_set(), "Cleanup started before the owned native operation finished"
        order.append("closed")
    monkeypatch.setattr(backend, "_" + method, native)
    monkeypatch.setattr(backend, "_close_native", closed)
    async def scenario():
        args = (policy, lease) if method == "install" else (lease,)
        task = asyncio.create_task(getattr(backend, method)(*args))
        try:
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(.002)
            assert entered.is_set()
            task.cancel()
            await asyncio.sleep(.005)
            task.cancel()
            await asyncio.sleep(.005)
            assert not task.done() and not completed.is_set()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert order == ["native_completed", "closed"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize('method', ['install', 'renew'])
def test_additional_cancellation_cannot_abandon_native_cleanup_thread(host, monkeypatch, method):
    _, construct, _ = host
    backend = construct()
    _, policy, lease = binding(backend)
    native_entered, native_release = threading.Event(), threading.Event()
    cleanup_entered, cleanup_release, cleanup_complete = threading.Event(), threading.Event(), threading.Event()
    def native(*args):
        native_entered.set()
        assert native_release.wait(3)
        return {}
    def closed():
        cleanup_entered.set()
        assert cleanup_release.wait(3)
        cleanup_complete.set()
    monkeypatch.setattr(backend, '_' + method, native)
    monkeypatch.setattr(backend, '_close_native', closed)
    async def reached(event):
        for _ in range(100):
            if event.is_set():
                return
            await asyncio.sleep(.002)
        pytest.fail('Native lifecycle stage did not start')
    async def scenario():
        args = (policy, lease) if method == 'install' else (lease,)
        task = asyncio.create_task(getattr(backend, method)(*args))
        try:
            await reached(native_entered)
            task.cancel()
            native_release.set()
            await reached(cleanup_entered)
            task.cancel()
            await asyncio.sleep(.005)
            assert not task.done(), 'Cancelled request escaped while native cleanup still owned a live worker'
            assert not cleanup_complete.is_set()
            cleanup_release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert cleanup_complete.is_set()
        finally:
            native_release.set()
            cleanup_release.set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
