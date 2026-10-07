"""Real STDIO SDK roundtrip for delegated chat tasks; no confirmation callback."""

import asyncio
import json
from pathlib import Path
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from test_controller import scope_body
from security_lab.controller import Controller


ROOT = Path(__file__).resolve().parents[1]


def test_discovery_and_status_do_not_compete_with_active_controller(tmp_path):
    """A separate discovery process must initialize while the owner is active."""
    owner = Controller(tmp_path, "protocol-fixture")
    owner_open = True
    case_id = owner.create_case("example.com")["id"]

    async def run():
        nonlocal owner_open
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "server.py"), "--data-dir", str(tmp_path), "--principal", "protocol-fixture"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "activate_scope_from_task" in {t.name for t in tools.tools}
                assert {str(r.uri) for r in (await session.list_resources()).resources} == {'security-lab://profiles/osint-domain'}
                assert len((await session.list_resource_templates()).resourceTemplates) == 1
                status = await session.call_tool("integration_status", {})
                assert not status.isError
                assert status.structuredContent["mcp_lifecycle_revision"] == 2
                assert status.structuredContent["live_execution"] is False
                denied = await session.call_tool("get_case", {"case_id": case_id})
                assert denied.isError
                assert "another active session" in denied.content[0].text
                assert owner.get_case(case_id)["jobs"] == []
                # A failed ownership claim must close its lock handle and leave
                # the MCP session usable after the real owner releases it.
                owner.close()
                owner_open = False
                recovered = await session.call_tool("get_case", {"case_id": case_id})
                assert not recovered.isError, recovered.content
                assert recovered.structuredContent["id"] == case_id
    try:
        asyncio.run(run())
    finally:
        if owner_open:
            owner.close()


def test_discovery_only_creates_no_controller_store(tmp_path):
    async def run():
        directory = tmp_path / "unused"
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "server.py"), "--data-dir", str(directory), "--principal", "protocol-fixture"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                await session.list_tools()
                profile = await session.read_resource('security-lab://profiles/osint-domain')
                assert 'Planner' in profile.contents[0].text
                assert not (await session.call_tool("integration_status", {})).isError
                assert not directory.exists()
        assert not directory.exists()
    asyncio.run(run())


def test_stdio_offline_osint_uses_parsers_and_persists_surface_receipts(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / 'server.py'), '--data-dir', str(tmp_path), '--principal', 'protocol-fixture'], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {t.name for t in (await session.list_tools()).tools}
                assert {'start_public_discovery', 'collect_osint_fixture', 'get_discovery_profile', 'get_source_evidence'} <= tools
                created = await session.call_tool('create_case', {'seed': 'fixture.example.test'})
                case_id = created.structuredContent['id']
                collected = await session.call_tool('collect_osint_fixture', {'case_id': case_id})
                assert not collected.isError, collected.content
                data = collected.structuredContent
                assert data['state'] == 'discovery_review' and data['jobs'] == [] and data['manifests'] == []
                assert len(data['links']) > 10
                assert len(data['source_receipts']) > 20
                assert all(r['synthetic'] for r in data['source_receipts'])
                receipt_id = data['source_receipts'][0]['id']
                evidence = await session.call_tool('get_source_evidence', {'case_id': case_id, 'receipt_id': receipt_id, 'offset': 0})
                assert evidence.structuredContent['synthetic'] == 1
                report = await session.read_resource(f'security-lab://case/{case_id}/report.html')
                assert 'SYNTHETIC' in report.contents[0].text
                assert 'not_implemented' in report.contents[0].text
    asyncio.run(run())


def test_stdio_fixture_and_delegated_chat_activation(tmp_path):
    async def run():
        params = StdioServerParameters(command=sys.executable,
            args=[str(ROOT / "server.py"), "--data-dir", str(tmp_path), "--principal", "protocol-fixture"], cwd=str(ROOT))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert {"activate_scope_from_task", "activate_actions_from_task"} <= names
                assert not {"approve_scope", "execute_shell", "resolve_confirmation",
                            "request_scope_confirmation", "request_action_confirmation"} & names
                tool = next(t for t in tools.tools if t.name == "activate_scope_from_task")
                assert set(tool.inputSchema["properties"]) == {"case_id", "manifest_id"}
                status = await session.call_tool("integration_status", {})
                assert not status.isError, status.content
                assert status.structuredContent["live_execution"] is False
                assert status.structuredContent["chat_authorization"] == "codex_operator_delegation"
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
                approved = await session.call_tool("activate_scope_from_task", {"case_id": case_id, "manifest_id": draft.structuredContent["id"]})
                assert approved.structuredContent["status"] == "approved"
                assert approved.structuredContent["approved_by"] == "delegated:codex:protocol-fixture"
                assert approved.structuredContent["authorization_source"] == "codex_operator_delegation"
                report = (await session.call_tool("get_report", {"case_id": case_id})).structuredContent
                receipt = json.loads(report["audit"][-1]["details"])
                assert receipt["hash"] == draft.structuredContent["hash"]
                assert receipt["independent_human_receipt"] is False
                assert report["jobs"] == []
                resources = await session.read_resource(f"security-lab://case/{case_id}/report.html")
                assert "SYNTHETIC" in resources.contents[0].text
                forged = await session.call_tool("approve_scope", {"approved": True})
                assert forged.isError
    asyncio.run(run())
