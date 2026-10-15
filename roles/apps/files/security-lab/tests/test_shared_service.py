"""Two independent MCP SDK clients, one process-owned Controller and job pool."""

import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from security_lab.controller import Controller
from security_lab.shared_service import build_shared_app
from test_controller import scope_body
from test_compose_service import Runtime
from test_native_mcp import PassiveRuntime


URL = "http://127.0.0.1:39118/mcp"
TOKENS = {"codex": "c" * 48, "opencode": "o" * 48}


def make_app(tmp_path, runtime=None):
    return build_shared_app({"data_dir": str(tmp_path / "cases"), "principal": "operator",
        "port": 39118, "service_id": "test-service"}, TOKENS, pentest_runtime=runtime)


@asynccontextmanager
async def session(app, name):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
            headers={"Authorization": "Bearer " + TOKENS[name]}) as client:
        async with streamable_http_client(URL, http_client=client) as (read, write, _):
            async with ClientSession(read, write) as sdk:
                await sdk.initialize()
                yield sdk


def data(result):
    assert not result.isError, result.content
    return result.structuredContent


def test_two_clients_share_history_identity_conflicts_and_disconnect(tmp_path):
    # Historical approvals remain byte-for-byte unchanged after the switch.
    owner = Controller(tmp_path / "cases", "operator")
    historical = owner.create_case("historical.example.test")["id"]
    now = int(time.time())
    old_scope = scope_body(expires_at=now + 600)
    old_scope["authorization"].update(valid_from=now - 1, valid_until=now + 1200)
    old_draft = owner.propose(historical, "scope", old_scope)
    owner.activate_from_task(historical, old_draft["id"], "scope")
    old_history = owner.export(historical)
    owner.close()
    app = make_app(tmp_path)

    async def run():
        async with app.inner.router.lifespan_context(app.inner):
            async with session(app, "opencode") as opencode:
                async with session(app, "codex") as codex:
                    for client, name in [(codex, "codex"), (opencode, "opencode")]:
                        status = data(await client.call_tool("integration_status", {}))
                        assert status["shared_controller"] is True and status["client"] == name
                    listed = data(await opencode.call_tool("list_cases", {"limit": 1}))
                    assert listed["cases"][0]["id"] == historical
                    case = data(await codex.call_tool("create_case", {"seed": "example.com"}))
                    case_id = case["id"]
                    view = data(await opencode.call_tool("get_case", {"case_id": case_id}))
                    assert view == case
                    now = int(time.time())
                    manifest = scope_body(expires_at=now + 600)
                    manifest["authorization"].update(valid_from=now - 1, valid_until=now + 1200)
                    proposals = await asyncio.gather(*[
                        client.call_tool("propose_scope", {"case_id": case_id, "manifest": manifest,
                            "expected_revision": case["revision"]}) for client in (codex, opencode)])
                    assert sum(not result.isError for result in proposals) == 1
                    assert "revision conflict" in next(r for r in proposals if r.isError).content[0].text
                    draft = data(next(r for r in proposals if not r.isError))
                    approved = data(await opencode.call_tool("activate_scope_from_task",
                        {"case_id": case_id, "manifest_id": draft["id"]}))
                    assert approved["approved_by"] == "delegated:opencode:operator"
                    assert approved["authorization_source"] == "opencode_operator_delegation"
                    denied = await codex.call_tool("activate_scope_from_task",
                        {"case_id": case_id, "manifest_id": draft["id"]})
                    assert denied.isError
                    report = data(await codex.call_tool("get_report", {"case_id": case_id}))
                    assert json.loads(report["audit"][-1]["details"])["client"] == "opencode"
                    assert len(report["manifests"]) == 1
                    missing = await codex.call_tool("propose_scope", {"case_id": case_id, "manifest": manifest})
                    assert missing.isError
                # Disconnecting Codex must not close/reinitialize the Controller.
                assert data(await opencode.call_tool("get_case", {"case_id": case_id}))["state"] == "scope_approved"
                assert data(await opencode.call_tool("list_cases", {"limit": 1}))["next_offset"] == 1
                assert data(await opencode.call_tool("list_cases", {"offset": 1}))["cases"][0]["id"] == historical
                profile = await opencode.read_resource("security-lab://profiles/osint-domain")
                assert "Planner" in profile.contents[0].text
    asyncio.run(run())
    reopened = Controller(tmp_path / "cases", "operator")
    assert reopened.get_case(historical)["seed"] == "historical.example.test"
    assert reopened.export(historical)["audit"] == old_history["audit"]
    assert reopened.get_case(historical)["manifests"] == old_history["manifests"]
    reopened.close()


def test_disconnect_does_not_cancel_work_other_client_can_stop(tmp_path):
    runtime = PassiveRuntime(blocked=True)
    app = make_app(tmp_path, runtime)
    async def run():
        async with app.inner.router.lifespan_context(app.inner):
            async with session(app, "opencode") as opencode:
                async with session(app, "codex") as codex:
                    case_id = data(await codex.call_tool("create_case", {"seed": "example.test"}))["id"]
                    data(await codex.call_tool("start_subfinder_discovery", {"case_id": case_id}))
                    for _ in range(30):
                        if runtime.calls:
                            break
                        await asyncio.sleep(.01)
                assert runtime.calls and runtime.closed == []
                duplicate = await opencode.call_tool("start_subfinder_discovery", {"case_id": case_id})
                assert duplicate.isError and len(runtime.calls) == 1
                stopped = data(await opencode.call_tool("stop_case", {"case_id": case_id}))
                assert stopped["state"] == "paused" and stopped["discovery_access_closed"]
                assert len(runtime.closed) == 1
    asyncio.run(run())


def test_shutdown_refuses_active_work_and_is_only_host_lifecycle(tmp_path):
    shutdown = []
    app = build_shared_app({"data_dir": str(tmp_path / "cases"), "principal": "operator",
        "port": 39118, "service_id": "test-service"}, TOKENS,
        pentest_runtime=PassiveRuntime(blocked=True), shutdown=lambda: shutdown.append(True))
    async def run():
        async with app.inner.router.lifespan_context(app.inner):
            async with session(app, "opencode") as sdk:
                tools = {t.name for t in (await sdk.list_tools()).tools}
                assert "shutdown" not in tools
                case_id = data(await sdk.call_tool("create_case", {"seed": "example.test"}))["id"]
                data(await sdk.call_tool("start_subfinder_discovery", {"case_id": case_id}))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                        headers={"Authorization": "Bearer " + TOKENS["codex"]}) as host:
                    assert (await host.post(URL.replace("/mcp", "/shutdown"))).status_code == 409
                    assert shutdown == []
                    data(await sdk.call_tool("stop_case", {"case_id": case_id}))
                    assert (await host.post(URL.replace("/mcp", "/shutdown"))).status_code == 200
                    assert shutdown == [True]
    asyncio.run(run())


def test_native_job_shared_admission_and_cross_client_stop(tmp_path):
    runtime = Runtime(running=True)
    app = make_app(tmp_path, runtime)
    async def run():
        async with app.inner.router.lifespan_context(app.inner):
            async with session(app, "codex") as codex, session(app, "opencode") as opencode:
                case = data(await codex.call_tool("create_case", {"seed": "example.com"}))
                case_id = case["id"]
                now = int(time.time())
                body = scope_body(targets=[{"host": "example.com"}], expires_at=now + 600)
                body["authorization"].update(valid_from=now - 1, valid_until=now + 1200)
                draft = data(await codex.call_tool("propose_scope", {"case_id": case_id, "manifest": body,
                    "expected_revision": case["revision"]}))
                data(await codex.call_tool("activate_scope_from_task", {"case_id": case_id, "manifest_id": draft["id"]}))
                case = data(await opencode.call_tool("get_case", {"case_id": case_id}))
                starts = await asyncio.gather(*[client.call_tool("start_pentestgpt_run",
                    {"case_id": case_id, "expected_revision": case["revision"]}) for client in (codex, opencode)])
                assert sum(not r.isError for r in starts) == 1
                job_id = data(next(r for r in starts if not r.isError))["id"]
                stopped = data(await opencode.call_tool("stop_case", {"case_id": case_id}))
                assert stopped["state"] == "paused"
                status = data(await codex.call_tool("get_job_status", {"case_id": case_id, "job_id": job_id}))
                assert status["evidence_id"] and status["outcome"] == "interrupted"
                assert len([c for c in runtime.calls if c[0] == "start"]) == 1
                assert len([c for c in runtime.calls if c[0] == "stop"]) == 1
    asyncio.run(run())


@pytest.mark.parametrize("extra,path,status", [
    ({"Authorization": "Bearer wrong"}, "/mcp", 401),
    ({"Host": "outside.example.test"}, "/mcp", 401),
    ({"Origin": "http://127.0.0.1:39118"}, "/mcp", 401),
    ({}, "/mcp?token=bad", 404), ({}, "/m%63p", 404), ({}, "/mcp/", 404)])
def test_transport_rejects_bad_credentials_host_origin_and_route(tmp_path, extra, path, status):
    app = make_app(tmp_path)
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app)) as client:
            response = await client.post("http://127.0.0.1:39118" + path,
                headers={"Authorization": "Bearer " + TOKENS["codex"], **extra}, json={})
            assert response.status_code == status
            assert all(token not in response.text for token in TOKENS.values())
        assert not (tmp_path / "cases").exists()
    asyncio.run(run())
