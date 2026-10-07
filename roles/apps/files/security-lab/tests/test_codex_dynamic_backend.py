"""Pinned dynamic protocol fake tests; no provider or live qualification claims."""
import asyncio
import copy
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from security_lab.codex_dynamic_backend import (
    CodexDynamicBackend, DynamicToolSession, build_dynamic_codex_profile, dynamic_tool_specs,
)
from security_lab.codex_tool_profile import NEGATIVE_PROBES
from security_lab.contracts import Denied


def profile():
    synthetic = {"models": [{"slug": "fixture", "display_name": "Synthetic fixture",
        "supported_reasoning_levels": [], "shell_type": "unified_exec", "visibility": "list",
        "supported_in_api": True, "priority": 1, "support_verbosity": False,
        "truncation_policy": {"mode": "tokens", "limit": 1000}, "experimental_supported_tools": []}]}
    return build_dynamic_codex_profile(synthetic, model="fixture", catalog_path="/host/catalog.json")


def request(name="get_context", arguments=None, **changes):
    params = {"threadId": "thread", "turnId": "turn", "callId": "call-1", "namespace": None,
              "tool": "mcp__pentest_checks__" + name, "arguments": arguments or {}}
    params.update(changes)
    return params


def session(forward=None, *, emit=None, max_calls=24):
    calls = []
    async def default_forward(name, arguments):
        calls.append((name, arguments))
        return {"source": "synthetic-protocol-fixture", "value": name}
    bound = DynamicToolSession(profile(), forward or default_forward, emit=emit, max_calls=max_calls)
    bound.bind("thread", "turn")
    return bound, calls


def test_no_mcp_registration_profile_disables_other_default_native_routes():
    candidate = profile()
    assert "mcp_servers={}" in candidate.config_overrides
    assert not any(value.startswith("mcp_servers.") for value in candidate.config_overrides)
    for key in ("view_image", "code_mode_host", "browser_use", "tool_suggest", "sleep_tool"):
        assert f"features.{key}=false" in candidate.config_overrides
    specs = dynamic_tool_specs(candidate)
    assert len(specs) == 5 and {item["name"] for item in specs} == set(candidate.expected_tools)
    assert all(item["type"] == "function" and item["deferLoading"] is False for item in specs)
    assert all(item["inputSchema"]["additionalProperties"] is False for item in specs)


def test_exact_pinned_call_response_and_grounded_events_preserve_host_payload():
    events = []
    bound, calls = session(emit=events.append)
    result = asyncio.run(bound.handle("item/tool/call", request()))
    assert calls == [("get_context", {})]
    assert result == {"contentItems": [{"type": "inputText", "text":
        '{"source":"synthetic-protocol-fixture","value":"get_context"}'}], "success": True}
    assert events[0]["type"] == "tool_call" and events[0]["call_id"] == "call-1"
    assert events[1]["type"] == "tool_result" and not events[1]["is_error"]
    assert json.loads(events[1]["output"]) == {"source": "synthetic-protocol-fixture", "value": "get_context"}


@pytest.mark.parametrize("changes", [
    {"threadId": "foreign"}, {"turnId": "foreign"}, {"namespace": "another"},
    {"tool": "execute_shell"}, {"tool": "mcp__another__get_context"}, {"callId": ""},
    {"extra": "case-override"}, {"arguments": {"case_id": "another"}}, {"arguments": "{}"},
])
def test_unknown_foreign_or_extra_request_fields_cannot_reach_forwarder(changes):
    bound, calls = session()
    with pytest.raises(Denied):
        asyncio.run(bound.handle("item/tool/call", request(**changes)))
    assert calls == []


@pytest.mark.parametrize("method", ["item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "mcpServer/elicitation/request", "account/chatgptAuthTokens/refresh", "execute_shell"])
def test_no_native_approval_or_other_server_request_is_forwarded(method):
    bound, calls = session()
    with pytest.raises(Denied):
        asyncio.run(bound.handle(method, request()))
    assert calls == []


def test_context_first_call_ids_and_budget_prevent_job_replay():
    bound, calls = session(max_calls=2)
    with pytest.raises(Denied, match="context"):
        asyncio.run(bound.handle("item/tool/call", request("execute_tool_job", {"specification": {}})))
    asyncio.run(bound.handle("item/tool/call", request()))
    job = request("execute_tool_job", {"specification": {"profile": "curl:headers"}}, callId="call-2")
    asyncio.run(bound.handle("item/tool/call", job))
    with pytest.raises(Denied, match="replayed"):
        asyncio.run(bound.handle("item/tool/call", job))
    with pytest.raises(Denied, match="budget"):
        asyncio.run(bound.handle("item/tool/call", request("list_tools", callId="call-3")))
    assert len(calls) == 2


@pytest.mark.parametrize("name,args", [("get_evidence", {"evidence_id": "e", "offset": True}),
    ("get_evidence", {"evidence_id": "e", "offset": -1}), ("get_job_status", {"job_id": "j", "case_id": "other"}),
    ("execute_tool_job", {"specification": {}, "scope_hash": "other"}), ("get_evidence", {"evidence_id": ""})])
def test_argument_boundaries_reject_before_forwarding(name, args):
    bound, calls = session()
    asyncio.run(bound.handle("item/tool/call", request()))
    with pytest.raises(Denied):
        asyncio.run(bound.handle("item/tool/call", request(name, args, callId="call-2")))
    assert len(calls) == 1


def test_transport_failure_never_echoes_credentials_or_sets_context_read():
    async def failing(name, arguments):
        raise RuntimeError("Authorization: Bearer fixture-secret http://private-host")
    events = []
    bound, _ = session(failing, emit=events.append)
    result = asyncio.run(bound.handle("item/tool/call", request()))
    assert result["success"] is False and not bound.context_read
    assert "fixture-secret" not in json.dumps(result) and "private-host" not in json.dumps(events)


@pytest.mark.parametrize("result", [{"not_json": float("nan")}, {"over_budget": "x" * 262145}])
def test_nonfinite_or_oversized_responses_do_not_become_successful_evidence(result):
    async def invalid(name, args):
        return result
    bound, _ = session(invalid)
    reply = asyncio.run(bound.handle("item/tool/call", request()))
    assert reply["success"] is False and not bound.context_read


def test_closed_session_and_mid_forward_revocation_cannot_report_success():
    bound, calls = session()
    bound.close()
    with pytest.raises(Denied):
        asyncio.run(bound.handle("item/tool/call", request()))
    assert not calls
    other = None
    async def revoking(name, args):
        other.close()
        return {"would_be_success": True}
    other, _ = session(revoking)
    result = asyncio.run(other.handle("item/tool/call", request()))
    assert result["success"] is False


def test_configuration_candidate_cannot_create_real_backend_without_host_receipt(tmp_path):
    with pytest.raises(Denied, match="qualification"):
        CodexDynamicBackend(profile(), None, qualification_receipt=None,
                            binary_path=str(tmp_path / "cli"), config_home=str(tmp_path / "home"))


def receipt(candidate):
    return {"profile_hash": candidate.profile_hash, "model": candidate.model,
            "binary_version": candidate.binary_version, "source_commit": candidate.source_commit,
            "binary_sha256": "a" * 64, "full_tool_inventory_observed": True,
            "used_fallback_model_metadata": False, "observed_tools": list(candidate.expected_tools),
            "negative_probes": {name: True for name in NEGATIVE_PROBES}, "qualification_environment": "isolated_lab"}


class FakeClient:
    """SDK-shaped fake transport, not an alternate implementation of the SDK."""
    def __init__(self, profile, callback, *, native=False):
        self.profile, self.callback, self.native = profile, callback, native
        self.closed = False
        self.index = 0
        self.thread_params = None
        self.reply = None
    def start(self):
        pass
    def initialize(self):
        return {}
    def thread_start(self, params):
        self.thread_params = copy.deepcopy(params)
        return {"thread": {"id": "thread"}, "model": self.profile.model}
    def turn_start(self, thread_id, prompt, params):
        assert thread_id == "thread"
        return {"turn": {"id": "turn", "status": "inProgress"}}
    def next_notification(self):
        self.index += 1
        if self.index == 1:
            if self.native:
                return SimpleNamespace(method="item/started", payload={"threadId": "thread", "turnId": "turn",
                    "item": {"type": "commandExecution", "id": "unexpected", "command": "whoami"}})
            self.reply = self.callback("item/tool/call", request())
            return SimpleNamespace(method="item/completed", payload={"threadId": "thread", "turnId": "turn",
                "item": {"type": "dynamicToolCall", "id": "call-1", "tool": request()["tool"], "status": "completed"}})
        if self.index == 2:
            return SimpleNamespace(method="item/completed", payload={"threadId": "thread", "turnId": "turn",
                "item": {"type": "agentMessage", "id": "message", "text": '{"status":"blocked"}'}})
        return SimpleNamespace(method="turn/completed", payload={"threadId": "thread",
            "turn": {"id": "turn", "status": "completed"}})
    def close(self):
        self.closed = True


@pytest.mark.parametrize("native", [False, True])
def test_lazy_backend_actual_unified_events_with_fake_sdk_protocol(tmp_path, monkeypatch, native):
    source = os.environ.get("SECURITY_LAB_UNIFIED_SOURCE")
    if not source:
        pytest.skip("Optional pinned UnifiedAgent events source fixture not supplied")
    monkeypatch.syspath_prepend(source)
    from unified_agent import ToolCall, ToolResult, TurnCompleted
    instances = []
    def factory(candidate, *, approval_handler, **kwargs):
        value = FakeClient(candidate, approval_handler, native=native)
        instances.append(value)
        return value
    async def forward(name, args):
        return {"bound": "synthetic-protocol-fixture"}
    candidate = profile()
    backend = CodexDynamicBackend(candidate, forward, qualification_receipt=receipt(candidate),
        binary_path=str(tmp_path / "cli"), config_home=str(tmp_path / "home"), client_factory=factory, timeout_seconds=5)
    opts = SimpleNamespace(resume=None, tool_server=None, extra_env={}, model="fixture", sandbox="read_only",
                           workspace=tmp_path, instructions="Synthetic protocol test", output_schema={"type": "object"}, effort="low")
    async def collect():
        return [event async for event in backend.stream("synthetic", opts)]
    if native:
        with pytest.raises(Denied, match="native provider"):
            asyncio.run(collect())
    else:
        events = asyncio.run(collect())
        assert len([event for event in events if isinstance(event, ToolCall)]) == 1
        assert len([event for event in events if isinstance(event, ToolResult)]) == 1
        assert events[-1].structured_output == {"status": "blocked"} and isinstance(events[-1], TurnCompleted)
        assert instances[0].reply["success"] is True
        assert len(instances[0].thread_params["dynamicTools"]) == 5
    assert instances[0].closed


def test_actual_optional_cached_sdk_raw_thread_params_preserve_dynamic_tools(monkeypatch):
    source = os.environ.get("SECURITY_LAB_CODEX_SDK_SOURCE")
    if not source:
        pytest.skip("Optional reviewed Codex Python SDK source fixture not supplied")
    monkeypatch.syspath_prepend(source)
    from openai_codex.client import CodexClient
    captured = []
    def capture(self, method, params, *, response_model):
        captured.append((method, params))
        return SimpleNamespace(thread=SimpleNamespace(id="synthetic"))
    monkeypatch.setattr(CodexClient, "request", capture)
    client = CodexClient(approval_handler=lambda method, params: {})
    params = {"model": "fixture", "dynamicTools": dynamic_tool_specs(profile()), "ephemeral": True}
    client.thread_start(params)
    assert captured == [("thread/start", params)]


def test_actual_optional_cached_sdk_routes_dynamic_server_request_to_injected_handler(monkeypatch):
    source = os.environ.get("SECURITY_LAB_CODEX_SDK_SOURCE")
    if not source:
        pytest.skip("Optional reviewed Codex Python SDK source fixture not supplied")
    monkeypatch.syspath_prepend(source)
    from openai_codex.client import CodexClient
    captured = []
    response = {"contentItems": [{"type": "inputText", "text": "synthetic"}], "success": True}
    def handle(method, params):
        captured.append((method, params))
        return response
    client = CodexClient(approval_handler=handle)
    # Exercise the real SDK server-request router without starting a process.
    result = client._handle_server_request({"id": "rpc-1", "method": "item/tool/call", "params": request()})
    assert result == response and captured == [("item/tool/call", request())]


@pytest.mark.parametrize("phase", ["start", "initialize"])
def test_cancelled_blocking_sdk_start_or_request_remains_owned_until_closed(tmp_path, monkeypatch, phase):
    source = os.environ.get("SECURITY_LAB_UNIFIED_SOURCE")
    if not source:
        pytest.skip("Optional pinned UnifiedAgent events source fixture not supplied")
    monkeypatch.syspath_prepend(source)
    entered, release = threading.Event(), threading.Event()
    instances = []
    class DelayedClient(FakeClient):
        alive = False
        def start(self):
            if phase == "start":
                entered.set()
                assert release.wait(3)
            self.alive = True  # Simulated late subprocess assignment.
        def initialize(self):
            if phase == "initialize":
                entered.set()
                assert release.wait(3)
            return {}
        def close(self):
            self.closed = True
            self.alive = False
            release.set()  # Real SDK close also unblocks pending RPC readers.
    def factory(candidate, *, approval_handler, **kwargs):
        client = DelayedClient(candidate, approval_handler)
        instances.append(client)
        return client
    async def forward(name, args):
        pytest.fail("Cancelled lifecycle must not reach host tool forwarding")
    candidate = profile()
    backend = CodexDynamicBackend(candidate, forward, qualification_receipt=receipt(candidate),
        binary_path=str(tmp_path / "cli"), config_home=str(tmp_path / "home"), client_factory=factory, timeout_seconds=5)
    opts = SimpleNamespace(resume=None, tool_server=None, extra_env={}, model="fixture", sandbox="read_only",
        workspace=tmp_path, instructions="Synthetic protocol test", output_schema=None, effort=None)
    async def exercise():
        async def collect():
            return [item async for item in backend.stream("synthetic", opts)]
        pending = asyncio.create_task(collect())
        assert await asyncio.to_thread(entered.wait, 2)
        pending.cancel()
        await asyncio.sleep(0.01)
        if phase == "start":
            # Repeated cancellation must not run close before late assignment.
            assert not instances[0].closed
            pending.cancel()
            await asyncio.sleep(0)
            assert not instances[0].closed
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert instances[0].closed and not instances[0].alive
    asyncio.run(exercise())
