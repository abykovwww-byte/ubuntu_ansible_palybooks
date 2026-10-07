"""Real stdio/MCP qualification without any elicitation callback or target traffic."""
import asyncio
import json
import sqlite3

from lab.qualify_dispatcher_stdio import qualify


def _all_keys(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _all_keys(item)
    elif isinstance(value, list):
        for item in value:
            yield from _all_keys(item)


def test_real_stdio_checks_handoff_dispatch_and_portable_report(tmp_path):
    summary = asyncio.run(qualify(tmp_path / "qualification"))
    assert summary["completed"] and summary["synthetic"] and summary["zero_network_fixture"]
    assert summary["elicitation_callback_used"] is False and summary["live_checks"] is False
    assert len(summary["receipts"]) == 10 and all(row["passed"] for row in summary["receipts"])
    output = tmp_path / "qualification"
    context = json.loads((output / "context.json").read_text("utf-8"))
    assert context["binding"]["hash"] == summary["scope_hash"]
    assert context["execution_authorized"] is False and context["policy"]["poc_allowed"] is False
    assert not {"token", "token_hash", "capability", "body", "install_ack", "worker_identity"} & set(_all_keys(context))
    assert json.loads((output / "qualification.json").read_text("utf-8")) == summary
    report = (output / "report.html").read_text("utf-8")
    assert "SYNTHETIC" in report and "<svg " in report and summary["evidence_id"] in report
    assert "script src=" not in report

    # Independent read-back after the actual MCP subprocess closed confirms
    # durable state rather than relying only on tool-returned dictionaries.
    with sqlite3.connect(output / "case-state/control.db") as database:
        state, spent = database.execute("SELECT state,spent FROM cases WHERE id=?", (summary["case_id"],)).fetchone()
        jobs = database.execute("SELECT COUNT(*) FROM jobs WHERE case_id=?", (summary["case_id"],)).fetchone()[0]
        body, synthetic = database.execute("SELECT body,synthetic FROM evidence WHERE id=?", (summary["evidence_id"],)).fetchone()
        authorization = database.execute("SELECT approved_by FROM manifests WHERE case_id=? AND kind='scope'", (summary["case_id"],)).fetchone()[0]
    assert state == "checks_review" and spent >= 5 and jobs == 1
    assert authorization == "delegated:codex:stdio-dispatcher-fixture"
    assert synthetic == 1 and json.loads(body)["access_closed"] is True
