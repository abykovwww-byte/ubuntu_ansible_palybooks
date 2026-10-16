"""STDIO compatibility bridge; no Controller or engagement tasks live here.

Both clients retain their normal local MCP registration. The bridge forwards
JSON-RPC to the authenticated shared service, starting it on demand. It never
retries a tool request and never shuts down the service on client disconnect.
"""

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import httpx

from security_lab.shared_service import load_credentials, load_settings


def headers(settings, client):
    return {"Authorization": "Bearer " + load_credentials(settings)[client],
            "Accept": "application/json, text/event-stream"}


def health(settings, client):
    try:
        response = httpx.get(f"http://127.0.0.1:{settings['port']}/health",
            headers=headers(settings, client), timeout=1, trust_env=False)
        return response.status_code == 200 and response.json().get("service_id") == settings["service_id"]
    except (httpx.HTTPError, ValueError):
        return False


class StartupLock:
    def __init__(self, path):
        self.file = path.open("a+b")

    def __enter__(self):
        if os.name == "nt":
            import msvcrt
            self.file.seek(0, os.SEEK_END)
            if self.file.tell() == 0:
                self.file.write(b"0")
                self.file.flush()
            for _ in range(400):
                try:
                    self.file.seek(0)
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                    return self
                except OSError:
                    time.sleep(.1)
            self.file.close()
            raise RuntimeError("Shared service startup is busy")
        import fcntl
        fcntl.flock(self.file, fcntl.LOCK_EX)
        return self

    def __exit__(self, *args):
        self.file.close()


def ssh_executable():
    windows = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/OpenSSH/ssh.exe"
    return str(windows) if os.name == "nt" and windows.exists() else "ssh"


def ssh_tunnel_command(settings):
    """Pure argv for the remote loopback forward; None for a local service.

    The server binds its service to 127.0.0.1, so a local forward keeps both the
    Host-header check and the loopback-only listener intact. The campaign loop
    lives in the server process, so closing this bridge never stops research.
    """
    host = settings.get("remote_ssh")
    if not host:
        return None
    port = settings["port"]
    command = [ssh_executable(), "-N", "-o", "BatchMode=yes", "-o", "ExitOnForwardFailure=yes",
               "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
               "-L", f"127.0.0.1:{port}:127.0.0.1:{port}"]
    identity = settings.get("remote_ssh_identity")
    if identity:
        command += ["-i", identity]
    return command + [host]


def ensure_service(settings_path, client):
    settings = load_settings(settings_path)
    if health(settings, client):
        return settings
    with StartupLock(settings_path.with_suffix(".startup.lock")):
        if health(settings, client):
            return settings
        tunnel = ssh_tunnel_command(settings)
        if tunnel is not None:
            # Remote service is started by the server's own lifecycle (systemd);
            # the bridge only owns a forwarded loopback port.
            process = subprocess.Popen(tunnel, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(200):
                if health(settings, client):
                    return settings
                if process.poll() is not None:
                    break
                time.sleep(.1)
            raise RuntimeError("Remote shared service tunnel did not become ready; check the server service and SSH identity")
        # Bind probe prevents accidentally talking to/replacing an unrelated
        # listener. The shared Controller lock independently fences all owners.
        import socket
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", settings["port"]))
        executable = Path(sys.executable)
        command = [str(executable), str(Path(__file__).with_name("shared_server.py")),
                   "--settings", str(settings_path)]
        process = None
        if os.name == "nt":
            # Windows MCP hosts terminate the entire bridge process tree, even
            # DETACHED_PROCESS descendants. WMI launches the daemon outside that
            # tree under the same user, without an admin service or scheduled task.
            command[0] = str(executable.with_name("pythonw.exe"))
            quoted = subprocess.list2cmdline(command).replace("'", "''")
            cwd = str(Path(__file__).parent).replace("'", "''")
            script = ("$ErrorActionPreference = 'Stop'; "
                "Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
                "@{CommandLine='" + quoted + "';CurrentDirectory='" + cwd + "'} | ConvertTo-Json -Compress")
            powershell = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            launched = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script.encode("utf-16-le")).decode("ascii")], capture_output=True, check=True, timeout=15)
            if json.loads(launched.stdout.decode("utf-8-sig"))["ReturnValue"] != 0:
                raise RuntimeError("Windows could not launch the user-owned shared service")
        else:
            with settings_path.with_suffix(".service.log").open("ab") as log:
                process = subprocess.Popen(command, cwd=str(Path(__file__).parent),
                    stdin=subprocess.DEVNULL, stdout=log, stderr=log, close_fds=True, start_new_session=True)
        for _ in range(200):
            if health(settings, client):
                return settings
            if process is not None and process.poll() is not None:
                break
            time.sleep(.1)
        raise RuntimeError("Shared service did not start; check its private service log (legacy Controller may still own the store)")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--settings", type=Path, required=True)
    parser.add_argument("--client", choices=["codex", "opencode"], required=True)
    parser.add_argument("--service", choices=["start", "status", "stop"])
    args = parser.parse_args()
    if args.service in {"status", "stop"}:
        settings = load_settings(args.settings)
        if args.service == "status":
            ready = health(settings, args.client)
            print(json.dumps({"ready": ready, "shared_controller": True}))
            return 0 if ready else 1
        response = httpx.post(f"http://127.0.0.1:{settings['port']}/shutdown",
            headers=headers(settings, args.client), timeout=10, trust_env=False)
        print(response.text)
        return 0 if response.status_code == 200 else 1
    settings = ensure_service(args.settings, args.client)
    if args.service == "start":
        print(json.dumps({"ready": True, "shared_controller": True}))
        return 0
    output_lock = threading.Lock()
    with httpx.Client(headers=headers(settings, args.client), timeout=120, trust_env=False) as client:
        def forward(line):
            message = None
            try:
                message = json.loads(line)
                response = client.post(f"http://127.0.0.1:{settings['port']}/mcp", json=message)
                response.raise_for_status()
                if "id" not in message:
                    return
                answer = response.json()
            except Exception:
                # A failed/ambiguous tool request is never replayed. Do not leak
                # headers, payloads, credentials or raw transport exceptions.
                if not isinstance(message, dict) or "id" not in message:
                    return
                answer = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32000,
                    "message": "Shared service request failed; outcome may be unknown. Read case/job status before another command."}}
            with output_lock:
                sys.stdout.buffer.write((json.dumps(answer, ensure_ascii=False) + "\n").encode("utf-8"))
                sys.stdout.buffer.flush()
        with ThreadPoolExecutor(max_workers=8) as pool:
            for line in sys.stdin.buffer:
                if line.strip():
                    pool.submit(forward, line)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("Security Lab bridge unavailable: " + type(exc).__name__, file=sys.stderr)
        sys.exit(1)
