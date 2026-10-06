"""Real Docker/nft/HTTPS acceptance in private, synthetic networks only.

This script has no MCP endpoint and must never run against a supplied domain.
It creates exactly its own named networks/containers and removes them on exit.
It does not alter host firewall tables, Docker rules or an existing container.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from security_lab.contracts import Scope, digest
from security_lab.http_policy import HTTPJobPolicy, Lease
from security_lab.nft_policy import render_worker_firewall, revoke_job_firewall


def run(*args, check=True, input=None):
    result = subprocess.run(list(args), input=input, text=True, capture_output=True, timeout=90)
    if check and result.returncode:
        raise RuntimeError(f"{args[:3]} failed: {result.stdout[-2000:]} {result.stderr[-3000:]}")
    return result


def wait_for(predicate, seconds=20):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("Fixture startup/receipt timeout")


def qualify(image, output):
    prefix = "sl-lab-" + uuid.uuid4().hex[:8]
    worker_net, upstream_net = prefix + "-worker", prefix + "-upstream"
    gateway, worker, upstream = prefix + "-gateway", prefix + "-worker", prefix + "-upstream"
    containers, networks, receipts = [], [], []
    root = Path(tempfile.mkdtemp(prefix=prefix))
    job, fixture, client = root / "job", root / "fixture", root / "client"
    for p in (job, fixture, client):
        p.mkdir()
    fixture.chmod(0o777)
    root.chmod(0o755)
    (job / "ca").mkdir()
    run("sudo", "chown", "1000:1000", str(job / "ca"))

    def exec_worker(*args, check=True):
        return run("docker", "exec", worker, "setpriv", "--reuid=1000", "--regid=1000", "--clear-groups",
                   "--bounding-set=-all", *args, check=check)

    def curl(path="/public/info", host="fixture.example.test", *extra, check=True):
        return exec_worker("curl", "--silent", "--show-error", "--noproxy", "", "--proxy",
                           "http://172.30.51.2:8080", "--cacert", "/client/ca.pem",
                           "--max-time", "5", "--output", "/dev/null", "--write-out", "%{http_code}",
                           *extra, f"https://{host}:8443{path}", check=check)

    def received():
        file = fixture / "received.jsonl"
        return [json.loads(v) for v in file.read_text("utf-8").splitlines()] if file.exists() else []

    def receipt(name, **details):
        receipts.append({"test": name, "passed": True, **details})

    try:
        for name, subnet, subnet6 in ((worker_net, "172.30.51.0/24", "fd00:51::/64"),
                                     (upstream_net, "172.30.52.0/24", "fd00:52::/64")):
            run("docker", "network", "create", "--internal", "--ipv6", "--subnet", subnet, "--subnet", subnet6, name)
            networks.append(name)
        run("docker", "run", "-d", "--name", upstream, "--network", upstream_net,
            "--ip", "172.30.52.3", "--ip6", "fd00:52::3", "--cap-drop", "ALL",
            "--mount", f"type=bind,src={fixture},dst=/fixture", image, "python", "/app/lab/fixture_server.py")
        containers.append(upstream)
        wait_for(lambda: (fixture / "cert.pem").exists())
        shutil.copyfile(fixture / "cert.pem", job / "upstream-ca.pem")
        now = int(time.time())
        scope = Scope.model_validate({
            "targets": [{"host": h, "port": 8443, "paths": ["/public"], "methods": ["GET"],
                         "excluded_paths": ["/public/admin"]} for h in ("fixture.example.test", "fixture6.example.test")],
            "authorization": {"reference": "lab:isolated-docker-fixture", "owner": "CI",
                              "basis": "Synthetic owned containers only; no Internet traffic",
                              "hosts": ["fixture.example.test", "fixture6.example.test"],
                              "valid_from": now-1, "valid_until": now+600},
            "expires_at": now+600, "budget": {"seconds": 600, "rps": 10, "concurrency": 2, "jobs": 10}})
        policy = HTTPJobPolicy(job_id=uuid.uuid4().hex, generation=1,
                               scope_hash=digest(scope.model_dump(mode="json")), scope=scope,
                               upstreams=[{"host": "fixture.example.test", "port": 8443, "addresses": ["172.30.52.3"]},
                                          {"host": "fixture6.example.test", "port": 8443, "addresses": ["fd00:52::3"]}],
                               worker_addresses=["172.30.51.3", "fd00:51::3"],
                               lab_private_addresses=["172.30.52.3", "fd00:52::3"], deadline=float(now+600))
        (job / "policy.json").write_text(policy.model_dump_json(), "utf-8")

        def start_gateway(seconds=30):
            lease = Lease(job_id=policy.job_id, generation=1, scope_hash=policy.scope_hash,
                          sequence=1, valid_until=time.time()+seconds)
            (job / "lease.json").write_text(lease.model_dump_json(), "utf-8")
            run("docker", "run", "-d", "--name", gateway, "--network", worker_net,
                "--ip", "172.30.51.2", "--ip6", "fd00:51::2", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
                "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "SETPCAP",
                "--sysctl", "net.ipv4.ip_forward=1", "--sysctl", "net.ipv6.conf.all.forwarding=1",
                "--add-host", "fixture.example.test:172.30.52.3", "--add-host", "fixture6.example.test:fd00:52::3",
                "--mount", f"type=bind,src={job},dst=/job", image, "sleep", "600")
            if gateway not in containers:
                containers.append(gateway)
            run("docker", "network", "connect", "--ip", "172.30.52.2", "--ip6", "fd00:52::2", upstream_net, gateway)
            run("docker", "exec", "-d", gateway, "python", "/app/lab/gateway.py")
            wait_for(lambda: (job / "ca" / "mitmproxy-ca-cert.pem").exists())
            shutil.copyfile(job / "ca" / "mitmproxy-ca-cert.pem", client / "ca.pem")
            wait_for(lambda: "8080" in run("docker", "exec", gateway, "ss", "-ltn", check=False).stdout)

        start_gateway()
        run("docker", "run", "-d", "--name", worker, "--network", worker_net,
            "--ip", "172.30.51.3", "--ip6", "fd00:51::3", "--cap-drop", "ALL", "--cap-add", "NET_ADMIN",
            "--cap-add", "SETUID", "--cap-add", "SETGID", "--cap-add", "SETPCAP",
            "--dns", "172.30.51.2", "--mount", f"type=bind,src={client},dst=/client,readonly", image, "sleep", "600")
        containers.append(worker)
        run("docker", "exec", worker, "ip", "route", "replace", "default", "via", "172.30.51.2")
        run("docker", "exec", worker, "ip", "-6", "route", "replace", "default", "via", "fd00:51::2")
        run("docker", "exec", "-i", worker, "nft", "-f", "-", input=render_worker_firewall("172.30.51.2"))
        assert "CapEff:\t0000000000000000" in exec_worker("cat", "/proc/self/status").stdout
        receipt("worker_unprivileged_capabilities_zero")
        assert curl().stdout == "200"
        time.sleep(0.12)
        assert curl(host="fixture6.example.test").stdout == "200"
        assert any("fd00:52" in r["peer"] for r in received())
        receipt("curl_https_allowed_ipv4_and_ipv6", upstream_received=len(received()))
        time.sleep(0.12)
        assert curl("/public/h2", "fixture.example.test", "--http2").stdout == "200"
        receipt("curl_https_http2_allowed")

        for name, path, host, extra in [
            ("excluded_path", "/public/admin", "fixture.example.test", []),
            ("path_prefix_lookalike", "/publicity", "fixture.example.test", []),
            ("encoded_path", "/public/%2e%2e/admin", "fixture.example.test", ["--path-as-is"]),
            ("raw_dot_path", "/public/../admin", "fixture.example.test", ["--path-as-is"]),
            ("wrong_method", "/public/info", "fixture.example.test", ["-X", "POST"]),
            ("wrong_host", "/public/info", "fixture.example.test", ["-H", "Host: outside.example.test:8443"]),
            ("suffix_lookalike", "/public/info", "fixture.example.test.outside.test", []),
        ]:
            before = len(received())
            result = curl(path, host, *extra, check=False)
            assert result.stdout != "200", (name, result)
            assert len(received()) == before, name
            receipt(name, worker_status=result.stdout, upstream_new_requests=0)
        time.sleep(0.12)
        before = len(received())
        result = curl("/public/redirect", "fixture.example.test", "--location", check=False)
        assert result.stdout != "200" and len(received()) == before+1
        receipt("redirect_rechecked", upstream_new_requests=1)

        # Verify worker output rule, then remove only its own table in this
        # adversarial lab step to test the gateway FORWARD chain independently.
        for address in ("172.30.52.3", "[fd00:52::3]"):
            result = exec_worker("curl", "--noproxy", "*", "-sk", "--max-time", "1",
                                 f"https://{address}:8443/public/info", check=False)
            assert result.returncode != 0
        receipt("worker_direct_tcp_ipv4_ipv6_denied")
        run("docker", "exec", worker, "nft", "delete", "table", "inet", "sl_worker")
        before = len(received())
        for address in ("172.30.52.3", "[fd00:52::3]"):
            result = exec_worker("curl", "--noproxy", "*", "-sk", "--max-time", "1",
                                 f"https://{address}:8443/public/info", check=False)
            assert result.returncode != 0
        assert len(received()) == before
        forward = run("docker", "exec", gateway, "nft", "list", "chain", "inet", "sl_job", "forward").stdout
        assert "packets 0 bytes 0" not in forward
        receipt("gateway_forward_denied_even_without_worker_firewall", counters=forward)
        run("docker", "exec", "-i", worker, "nft", "-f", "-", input=render_worker_firewall("172.30.51.2"))
        result = run("docker", "exec", gateway, "curl", "--noproxy", "*", "-sk", "--max-time", "1",
                     "https://172.30.52.4:8443/public/info", check=False)
        assert result.returncode != 0
        receipt("gateway_unpinned_upstream_denied")

        # Start a real streamed response, then revoke firewall and proxy lease.
        stream = subprocess.Popen(["docker", "exec", worker, "setpriv", "--reuid=1000", "--regid=1000",
            "--clear-groups", "--bounding-set=-all", "curl", "--silent", "--show-error", "--noproxy", "",
            "--proxy", "http://172.30.51.2:8080", "--cacert", "/client/ca.pem", "--max-time", "20",
            "--output", "/tmp/stream", "https://fixture.example.test:8443/public/stream"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        wait_for(lambda: "/public/stream" in {v["path"] for v in received()})
        time.sleep(0.3)
        start = time.monotonic()
        run("docker", "exec", "-i", gateway, "nft", "-f", "-", input=revoke_job_firewall())
        revoked = Lease(job_id=policy.job_id, generation=1, scope_hash=policy.scope_hash,
                        sequence=2, valid_until=time.time()+10, revoked=True)
        temp = job / "lease.next"
        temp.write_text(revoked.model_dump_json(), "utf-8")
        temp.replace(job / "lease.json")
        stream.communicate(timeout=10)
        elapsed = time.monotonic()-start
        assert elapsed < 10 and stream.returncode != 0
        receipt("revoked_existing_https_stream_closed", seconds=round(elapsed, 3))

        # A fresh short lease without renewal must close an existing stream.
        run("docker", "rm", "-f", gateway)
        start_gateway(seconds=4)
        time.sleep(0.12)
        start = time.monotonic()
        result = curl("/public/stream", check=False)
        elapsed = time.monotonic()-start
        assert result.returncode != 0 and elapsed < 5
        sets = run("docker", "exec", gateway, "nft", "list", "set", "inet", "sl_job", "worker4").stdout
        assert "172.30.51.3" not in sets
        receipt("controller_loss_lease_expires_and_stream_closes", seconds=round(elapsed, 3))
        run("docker", "rm", "-f", gateway)
        start_gateway()
        before = len(received())
        raw_probe = '''import socket,ssl
s=socket.create_connection(("172.30.51.2",8080),timeout=3)
s.sendall(b"CONNECT fixture.example.test:8443 HTTP/1.1\\r\\nHost: fixture.example.test:8443\\r\\n\\r\\n")
reply=s.recv(4096)
assert b"200" in reply, reply
try:
 c=ssl.create_default_context(cafile="/client/ca.pem").wrap_socket(s,server_hostname="outside.example.test")
 c.sendall(b"GET /public/info HTTP/1.1\\r\\nHost: fixture.example.test:8443\\r\\n\\r\\n")
 assert not c.recv(4096)
except (ssl.SSLError,ConnectionError,OSError):
 pass
'''
        exec_worker("python", "-c", raw_probe)
        assert len(received()) == before
        receipt("connect_sni_mismatch_denied_before_upstream", upstream_new_requests=0)
    finally:
        for container in reversed(containers):
            logs = run("docker", "logs", container, check=False)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.with_name(container + ".log").write_text(logs.stdout + logs.stderr, "utf-8")
            run("docker", "rm", "-f", container, check=False)
        for network in reversed(networks):
            run("docker", "network", "rm", network, check=False)
        # No recursive filesystem deletion; CI runner removes its temp directory.
        output.write_text(json.dumps({"synthetic": True, "private_lab_only": True,
                                      "receipts": receipts}, indent=2), "utf-8")
    return receipts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="security-lab-network:ci")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = qualify(args.image, args.output)
    print(f"Network lab: {len(result)} receipts passed; no live target was contacted")
