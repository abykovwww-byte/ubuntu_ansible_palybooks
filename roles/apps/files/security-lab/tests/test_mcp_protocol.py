"""Real STDIO SDK roundtrip with a SIMULATED host; not Codex Desktop acceptance."""

import asyncio
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from test_controller import scope_body


ROOT = Path(__file__).resolve().parents[1]


def test_stdio_fixture_and_confirmation_protocol(tmp_path):
    async def run():
        elicited = []
        async def reply(context, params):
            elicited.append(params.message)
            return types.ElicitResult(action="accept", content={"confirm": True})

        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "server.py"), "--data-dir", str(tmp_path), "--principal", "protocol-fixture"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, elicitation_callback=reply) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert "request_scope_confirmation" in names
                assert not {"approve_scope", "execute_shell", "resolve_confirmation"} & names
                tool = next(t for t in tools.tools if t.name == "request_scope_confirmation")
                assert set(tool.inputSchema["properties"]) == {"case_id", "manifest_id"}
                status = await session.call_tool("integration_status", {})
                assert not status.isError, status.content
                assert status.structuredContent["live_execution"] is False
                created = await session.call_tool("create_case", {"seed": "example.com"})
                assert not created.isError, created.content
                case_id = created.structuredContent["id"]
                fixture = await session.call_tool("collect_lab_fixture", {"case_id": case_id})
                assert len(fixture.structuredContent["surface"]) == 3
                import time
                now = int(time.time())
                body = scope_body()
                body["expires_at"] = now + 3600
                body["authorization"].update(valid_from=now-1, valid_until=now+7200)
                draft = await session.call_tool("propose_scope", {"case_id": case_id, "manifest": body})
                assert not draft.isError
                approved = await session.call_tool("request_scope_confirmation", {"case_id": case_id, "manifest_id": draft.structuredContent["id"]})
                assert approved.structuredContent["status"] == "approved"
                assert len(elicited) == 1 and "example.com" in elicited[0]
                assert draft.structuredContent["hash"] in elicited[0]
                resources = await session.read_resource(f"security-lab://case/{case_id}/report.html")
                assert "SYNTHETIC" in resources.contents[0].text
                forged = await session.call_tool("approve_scope", {"approved": True})
                assert forged.isError
    asyncio.run(run())


def test_no_elicitation_capability_does_not_approve(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "server.py"), "--data-dir", str(tmp_path), "--principal", "protocol-fixture"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                created = await session.call_tool("create_case", {"seed": "example.com"})
                assert not created.isError, created.content
                import time
                now = int(time.time())
                body = scope_body()
                body["expires_at"] = now + 3600
                body["authorization"].update(valid_from=now-1, valid_until=now+7200)
                draft = await session.call_tool("propose_scope", {"case_id": created.structuredContent["id"], "manifest": body})
                reply = await session.call_tool("request_scope_confirmation", {"case_id": created.structuredContent["id"], "manifest_id": draft.structuredContent["id"]})
                assert reply.structuredContent["approved"] is False
                assert reply.structuredContent["status"] == "confirmation_unavailable"
    asyncio.run(run())
