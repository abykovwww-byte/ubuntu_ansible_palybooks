"""Developer-only real MCP stdio qualification, with zero-network fixture data.

This harness is not an operator approval CLI and accepts no domain, arbitrary
tool, target URL, image, capability, or command argument. Normal operation is
entirely through Codex chat; this script tests that protocol without UI prompts.
No curl process, Linux firewall, target traffic or model API is exercised here.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
SPECIFICATION = {"tool": "curl", "url": "https://fixture.example.test:8443/public/info", "method": "GET", "seconds": 5}


def _write(path: Path, value: object) -> None:
    body = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
        output.write(body)


async def qualify(output_dir: Path) -> dict:
    output_dir = Path(output_dir).absolute()
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    state = output_dir / "case-state"
    state.mkdir(mode=0o700)
    receipts = []

    def passed(name: str) -> None:
        receipts.append({"test": name, "passed": True})

    async def call(session: ClientSession, name: str, arguments: dict) -> dict:
        result = await session.call_tool(name, arguments)
        if result.isError or not isinstance(result.structuredContent, dict):
            raise AssertionError("MCP qualification failed at " + name)
        return result.structuredContent

    parameters = StdioServerParameters(command=sys.executable,
        args=[str(ROOT / "server.py"), "--data-dir", str(state), "--principal", "stdio-dispatcher-fixture"], cwd=str(ROOT))
    # Intentionally no elicitation_callback: the user's chat task is authority.
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name: tool for tool in (await session.list_tools()).tools}
            required = {"collect_osint_fixture", "activate_scope_from_task", "get_pentest_context", "execute_tool_job",
                        "execute_fixture_check", "get_job_status", "get_source_evidence"}
            assert required <= set(tools)
            assert not {"execute_shell", "approve_scope", "request_scope_confirmation", "request_action_confirmation"} & set(tools)
            for name in ("execute_tool_job", "execute_fixture_check"):
                assert set(tools[name].inputSchema["properties"]) == {"case_id", "specification"}
            assert set(tools["activate_scope_from_task"].inputSchema["properties"]) == {"case_id", "manifest_id"}
            assert not {"prepare_job", "bind_job_policy", "acknowledge_job", "start_job", "renew_job", "finish_job"} & set(tools)
            passed("model_tools_exclude_launcher_and_capability_fields")

            created = await call(session, "create_case", {"seed": "fixture.example.test"})
            case_id = created["id"]
            collected = await call(session, "collect_osint_fixture", {"case_id": case_id})
            assert collected["state"] == "discovery_review" and collected["jobs"] == []
            assert collected["source_receipts"] and all(receipt["synthetic"] for receipt in collected["source_receipts"])
            passed("offline_osint_parsers_preserve_synthetic_receipts")

            now = int(time.time())
            manifest = {
                "targets": [{"host": "fixture.example.test", "scheme": "https", "port": 8443,
                             "paths": ["/public"], "excluded_paths": ["/public/admin"], "methods": ["GET"]}],
                "authorization": {"reference": "lab:isolated-docker-fixture", "owner": "stdio-dispatcher-fixture",
                                  "basis": "Owned synthetic zero-network fixture only",
                                  "hosts": ["fixture.example.test"], "valid_from": now - 1, "valid_until": now + 600},
                "expires_at": now + 600, "budget": {"seconds": 60, "rps": 2, "concurrency": 1, "jobs": 3},
            }
            proposed = await call(session, "propose_scope", {"case_id": case_id, "manifest": manifest})
            activated = await call(session, "activate_scope_from_task", {"case_id": case_id, "manifest_id": proposed["id"]})
            assert activated["status"] == "approved" and activated["authorization_source"] == "codex_operator_delegation"
            passed("explicit_task_activation_without_ui_callback")

            context = await call(session, "get_pentest_context", {"case_id": case_id})
            assert context["binding"]["hash"] == proposed["hash"]
            assert context["stage"] == "checks" and context["execution_authorized"] is False
            assert context["policy"]["scope_expansion_allowed"] is False and context["policy"]["poc_allowed"] is False
            assert context["effective_scope"]["targets"] == manifest["targets"]
            assert all(node["testing_scope_authority"] is False for node in context["untrusted_osint_data"]["surface_nodes"])
            assert all(receipt["synthetic"] for receipt in context["untrusted_osint_data"]["source_receipts"])
            passed("checks_context_binds_exact_scope_without_execution_grant")

            production = await session.call_tool("execute_tool_job", {"case_id": case_id, "specification": SPECIFICATION})
            assert production.isError
            assert (await call(session, "get_case", {"case_id": case_id}))["jobs"] == []
            passed("production_refused_before_issuing_job")

            forged = await session.call_tool("execute_fixture_check", {"case_id": case_id, "specification": {
                **SPECIFICATION, "image_digest": "model-selected-image", "flags": ["--insecure"],
                "capability": "forged", "install_ack": {"sequence": 1}}})
            assert forged.isError
            assert (await call(session, "get_case", {"case_id": case_id}))["jobs"] == []
            passed("model_launcher_image_flags_capability_and_ack_injection_refused")

            submitted = await call(session, "execute_fixture_check", {"case_id": case_id, "specification": SPECIFICATION})
            job_id = submitted["id"]
            for _ in range(100):
                job = await call(session, "get_job_status", {"case_id": case_id, "job_id": job_id})
                if job["evidence_id"] is not None:
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("Synthetic dispatcher evidence did not finish")
            assert job["outcome"] == "completed"
            evidence = await call(session, "get_source_evidence", {"case_id": case_id, "receipt_id": job["evidence_id"], "offset": 0})
            body = json.loads(evidence["body"])
            assert evidence["synthetic"] == 1 and body["synthetic"] is True
            assert body["lab_only"] is True and body["access_closed"] is True
            assert body["scope_hash"] == proposed["hash"]
            assert body["tool"] == "curl" and body["tool_version"] == "synthetic:no-binary"
            assert body["image_digest"] == "synthetic:no-image"
            assert "no network" in body["stdout"]
            passed("synthetic_dispatcher_job_evidence_and_access_closure")

            final_case = await call(session, "get_case", {"case_id": case_id})
            assert final_case["state"] == "checks_review" and len(final_case["jobs"]) == 1
            assert final_case["spent"] >= SPECIFICATION["seconds"]
            assert final_case["jobs"][0]["evidence_id"] == evidence["id"]
            passed("durable_checks_review_and_budget_accounting")
            exported = await session.read_resource(f"security-lab://case/{case_id}/report.html")
            report = exported.contents[0].text
            assert "SYNTHETIC" in report and "<svg " in report
            assert evidence["id"] in report
            passed("portable_surface_and_worker_evidence_report")
            status = await call(session, "integration_status", {})
            assert status["live_execution"] is False
            passed("production_readiness_remains_closed")

    if os.name == "posix":
        for file in state.iterdir():
            if file.is_file():
                file.chmod(0o600)
    summary = {"completed": True, "synthetic": True, "zero_network_fixture": True,
               "real_stdio_sdk": True, "live_checks": False, "elicitation_callback_used": False,
               "case_id": case_id, "job_id": job_id, "evidence_id": evidence["id"],
               "context_hash": context["context_hash"], "scope_hash": proposed["hash"], "receipts": receipts}
    _write(output_dir / "context.json", context)
    _write(output_dir / "qualification.json", summary)
    _write(output_dir / "report.html", report)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    arguments = parser.parse_args()
    summary = asyncio.run(qualify(arguments.output_dir))
    print(f'Stdio dispatcher fixture: {len(summary["receipts"])} checks passed; synthetic, zero network, live checks disabled')


if __name__ == "__main__":
    main()
