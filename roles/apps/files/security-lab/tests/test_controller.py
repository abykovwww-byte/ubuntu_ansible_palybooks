import json
from pathlib import Path
import threading

import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied, Scope, Target, hostname
from security_lab.controller import Controller
from security_lab.reports import html_report


NOW = 1_800_000_000


def scope_body(**overrides):
    value = {"schema_version": 1, "targets": [{"host": "example.com", "excluded_paths": ["/admin"]}],
             "authorization": {"reference": "lab:owned-fixture", "owner": "fixture-operator",
                               "basis": "Synthetic lab only", "hosts": ["example.com"],
                               "valid_from": NOW - 1, "valid_until": NOW + 86400},
             "expires_at": NOW + 3600}
    value.update(overrides)
    return value


@pytest.fixture
def control(tmp_path):
    c = Controller(tmp_path / "state", "fixture-operator", clock=lambda: NOW)
    yield c
    c.close()


def approve(c, case_id, body=None):
    draft = c.propose(case_id, "scope", body or scope_body())
    pending = c.challenge(case_id, draft["id"], "fixture-session")
    c.resolve_confirmation(pending["challenge"], "fixture-session", True, transport="mcp_elicitation")
    return draft, pending


def test_fixture_preserves_provenance_and_dedup(control):
    case_id = control.create_case("Example.COM.")["id"]
    control.collect_fixture(case_id)
    control.collect_fixture(case_id)
    result = control.export(case_id)
    assert len(result["surface"]) == 3
    assert len(result["evidence"]) == 6
    assert all(e["synthetic"] == 1 for e in result["evidence"])
    assert all(len(json.loads(n["evidence_ids"])) == 2 for n in result["surface"])
    assert result["state"] == "discovery_review"


@pytest.mark.parametrize("seed", ["https://example.com", "127.0.0.1", "*.example.com", "example.com/path", "evil@host.com", "x..com"])
def test_seed_is_not_an_arbitrary_url(seed):
    with pytest.raises(ValueError):
        hostname(seed)


@pytest.mark.parametrize("extra", [{"approved_by": "operator"}, {"approved": True}, {"schema_version": 2}, {"allowed_actions": ["poc"]}])
def test_model_cannot_supply_approval_or_change_contract(extra):
    with pytest.raises(ValidationError):
        Scope.model_validate(scope_body(**extra))


def test_scope_requires_matching_authorization():
    body = scope_body()
    del body["authorization"]
    with pytest.raises(ValidationError):
        Scope.model_validate(body)
    body = scope_body(targets=[{"host": "other.example.com"}])
    with pytest.raises(ValidationError, match="authorization"):
        Scope.model_validate(body)


def test_no_capability_without_real_approval(control):
    case_id = control.create_case("example.com")["id"]
    control.propose(case_id, "scope", scope_body())
    with pytest.raises(Denied, match="approved scope"):
        control.prepare_job(case_id, "worker-1", "target_http", 20)


def test_confirmation_replay_and_wrong_session_denied(control):
    case_id = control.create_case("example.com")["id"]
    draft = control.propose(case_id, "scope", scope_body())
    p = control.challenge(case_id, draft["id"], "session-1")
    with pytest.raises(Denied):
        control.resolve_confirmation(p["challenge"], "session-2", True, transport="mcp_elicitation")
    with pytest.raises(Denied):
        control.resolve_confirmation(p["challenge"], "session-1", True, transport="model_claim")
    result = control.resolve_confirmation(p["challenge"], "session-1", True, transport="mcp_elicitation")
    assert result["approved_by"] == "fixture-operator"
    with pytest.raises(Denied):
        control.resolve_confirmation(p["challenge"], "session-1", True, transport="mcp_elicitation")


def test_unconfirmed_transport_consumes_attempt_and_allows_fresh_challenge(control):
    case_id = control.create_case("example.com")["id"]
    draft = control.propose(case_id, "scope", scope_body())
    pending = control.challenge(case_id, draft["id"], "s")
    with pytest.raises(Denied):
        control.record_unconfirmed_response(pending["challenge"], "other",
            "confirmation_not_approved", "host_decline_without_user_receipt", {})
    result = control.record_unconfirmed_response(pending["challenge"], "s",
        "confirmation_not_approved", "host_decline_without_user_receipt", {"elapsed_ms": 100})
    assert result["manifest_status"] == "pending"
    with pytest.raises(Denied):
        control.resolve_confirmation(pending["challenge"], "s", True, transport="mcp_elicitation")
    with pytest.raises(Denied):
        control.prepare_job(case_id, "worker", "target_http", 20)
    fresh = control.challenge(case_id, draft["id"], "s")
    control.resolve_confirmation(fresh["challenge"], "s", True, transport="mcp_elicitation")


def test_timeout_after_stop_records_outcome_without_changing_manifest(control):
    case_id = control.create_case("example.com")["id"]
    draft = control.propose(case_id, "scope", scope_body())
    pending = control.challenge(case_id, draft["id"], "s")
    control.stop(case_id)
    control.clock = lambda: NOW + 301
    control.record_unconfirmed_response(pending["challenge"], "s",
        "confirmation_unavailable", "TimeoutError", {})
    assert control.get_case(case_id)["state"] == "paused"
    assert control.get_case(case_id)["manifests"][0]["status"] == "pending"


def test_scope_changed_while_confirmation_pending(control):
    case_id = control.create_case("example.com")["id"]
    a = control.propose(case_id, "scope", scope_body())
    p = control.challenge(case_id, a["id"], "s")
    control.propose(case_id, "scope", scope_body(targets=[{"host": "example.com", "paths": ["/public"]}]))
    with pytest.raises(Denied):
        control.resolve_confirmation(p["challenge"], "s", True, transport="mcp_elicitation")


def test_stop_during_pending_confirmation(control):
    case_id = control.create_case("example.com")["id"]
    a = control.propose(case_id, "scope", scope_body())
    p = control.challenge(case_id, a["id"], "s")
    control.stop(case_id)
    with pytest.raises(Denied):
        control.resolve_confirmation(p["challenge"], "s", True, transport="mcp_elicitation")


def test_lease_expiry_stop_and_worker_binding(control):
    case_id = control.create_case("example.com")["id"]
    approve(control, case_id)
    job, token = control.prepare_job(case_id, "ns:worker-1", "target_http", 100)
    assert control.validate_capability(job["id"], token, "ns:worker-1", "target_http")["status"] == "prepared"
    for worker, profile, secret in [("ns:worker-2", "target_http", token), ("ns:worker-1", "target_network", token), ("ns:worker-1", "target_http", "fake")]:
        with pytest.raises(Denied):
            control.validate_capability(job["id"], secret, worker, profile)
    assert token not in json.dumps(control.export(case_id))
    control.clock = lambda: NOW + 31
    with pytest.raises(Denied):
        control.validate_capability(job["id"], token, "ns:worker-1", "target_http")
    control.clock = lambda: NOW
    control.stop(case_id)
    with pytest.raises(Denied):
        control.validate_capability(job["id"], token, "ns:worker-1", "target_http")
    assert control.db.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0] >= 1


def test_resume_keeps_spent_budget_and_checks_scope_expiry(control):
    case_id = control.create_case("example.com")["id"]
    approve(control, case_id)
    with control.transaction() as db:
        db.execute("UPDATE cases SET spent=7100 WHERE id=?", (case_id,))
    control.stop(case_id)
    assert control.resume(case_id)["spent"] == 7100
    with pytest.raises(Denied, match="budget"):
        control.prepare_job(case_id, "worker", "target_http", 101)
    control.stop(case_id)
    control.clock = lambda: NOW + 3601
    with pytest.raises(Denied):
        control.resume(case_id)


def test_recovery_preserves_immutable_approval_and_ownership(tmp_path):
    c = Controller(tmp_path, "fixture-operator", clock=lambda: NOW)
    case_id = c.create_case("example.com")["id"]
    draft, _ = approve(c, case_id)
    c.close()
    c = Controller(tmp_path, "fixture-operator", clock=lambda: NOW)
    assert c.get_case(case_id)["manifests"][0]["hash"] == draft["hash"]
    c.close()
    other = Controller(tmp_path, "another-operator", clock=lambda: NOW)
    with pytest.raises(Denied):
        other.get_case(case_id)
    other.close()


def test_parallel_stop_dispatch_never_revives_old_generation(control):
    case_id = control.create_case("example.com")["id"]
    approve(control, case_id)
    jobs = []
    def prepare():
        try:
            jobs.append(control.prepare_job(case_id, "worker", "target_http", 10))
        except Denied:
            pass
    threads = [threading.Thread(target=prepare), threading.Thread(target=lambda: control.stop(case_id))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for job, token in jobs:
        with pytest.raises(Denied):
            control.validate_capability(job["id"], token, "worker", "target_http")


def test_cannot_start_competing_writer(control):
    with pytest.raises(OSError):
        Controller(control.root, "fixture-operator")


@pytest.mark.parametrize("url,method", [
    ("https://example.com/admin", "GET"), ("https://example.com/admin/x", "GET"),
    ("https://example.com/%61dmin", "GET"), ("https://example.com/public/../admin", "GET"),
    ("https://example.com.evil.test/", "GET"), ("https://example.com/", "POST"),
    ("https://example.com:444/", "GET"), ("http://example.com/", "GET"),
    ("https://user@example.com/", "GET"), ("https://example.com//admin", "GET")])
def test_http_policy_denies_identity_method_path_bypasses(url, method):
    t = Target(host="example.com", paths=["/"], excluded_paths=["/admin"])
    assert not t.permits(url, method)


def test_segment_matching_and_escaped_report(control):
    t = Target(host="example.com", paths=["/public"])
    assert t.permits("https://example.com/public/x?q=1", "GET")
    assert not t.permits("https://example.com/publicity", "GET")
    case_id = control.create_case("example.com")["id"]
    control.collect_fixture(case_id)
    result = control.export(case_id)
    result["evidence"][0]["body"] = "<script>alert(1)</script>"
    html = html_report(result)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "SYNTHETIC" in html
    assert control.integration_status()["live_execution"] is False
