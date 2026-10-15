"""Real Windows/POSIX STDIO bridges -> loopback HTTP -> one daemon."""

import asyncio
import json
from pathlib import Path
import socket
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from client_proxy import health, main


ROOT = Path(__file__).resolve().parents[1]


def test_two_real_stdio_bridges_start_once_and_share_durable_cases(tmp_path, monkeypatch):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    credentials = tmp_path / "credentials.json"
    credentials.write_text(json.dumps({"codex": "c" * 48, "opencode": "o" * 48}), encoding="utf-8")
    settings = {"data_dir": str(tmp_path / "cases"), "principal": "operator", "port": port,
        "service_id": "real-proxy-test", "credentials_file": str(credentials)}
    path = tmp_path / "service.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    created = []

    async def client(name):
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "client_proxy.py"),
            "--settings", str(path), "--client", name], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                status = await session.call_tool("integration_status", {})
                assert not status.isError and status.structuredContent["client"] == name
                result = await session.call_tool("create_case", {"seed": name + ".example.test"})
                assert not result.isError
                created.append(result.structuredContent["id"])

    async def verify():
        await asyncio.gather(client("codex"), client("opencode"))
        # Both STDIO processes have exited; the daemon owns the store and lives on.
        assert health(settings, "codex") and health(settings, "opencode")
        params = StdioServerParameters(command=sys.executable, args=[str(ROOT / "client_proxy.py"),
            "--settings", str(path), "--client", "opencode"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                cases = (await session.call_tool("list_cases", {})).structuredContent
                assert {c["id"] for c in cases["cases"]} == set(created)
    try:
        asyncio.run(verify())
    finally:
        if health(settings, "codex"):
            monkeypatch.setattr(sys, "argv", ["client_proxy.py", "--settings", str(path),
                "--client", "codex", "--service", "stop"])
            assert main() == 0
        for _ in range(100):
            if not health(settings, "codex"):
                break
            time.sleep(.05)
        assert not health(settings, "codex")
