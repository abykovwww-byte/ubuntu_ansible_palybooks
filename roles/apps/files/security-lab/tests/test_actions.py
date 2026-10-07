import pytest

from security_lab.contracts import Denied
from test_controller import NOW, approve, control, scope_body


def action_body(version=1):
    return {"scope_version": version, "expires_at": NOW+600,
            "actions": [{"finding_id": "F-03", "host": "example.com",
                         "description": "Synthetic lab confirmation",
                         "expected_effect": "Read the lab-only response",
                         "stop_condition": "One response, no data extraction"}]}


def test_scope_approval_does_not_approve_poc(control):
    case_id = control.create_case("example.com")["id"]
    approve(control, case_id)
    with control.transaction() as db:
        db.execute("UPDATE cases SET state='checks_review' WHERE id=?", (case_id,))
    with pytest.raises(Denied, match="authorization"):
        control.propose(case_id, "actions", action_body())


def test_second_gate_is_distinct_and_bound_to_current_scope(control):
    case_id = control.create_case("example.com")["id"]
    body = scope_body()
    body["authorization"]["actions"] = ["checks", "poc"]
    approve(control, case_id, body)
    with pytest.raises(Denied, match="checks review"):
        control.propose(case_id, "actions", action_body())
    with control.transaction() as db:
        db.execute("UPDATE cases SET state='checks_review' WHERE id=?", (case_id,))
    action = control.propose(case_id, "actions", action_body())
    assert action["status"] == "pending"
    assert control.get_case(case_id)["state"] == "checks_review"
    result = control.activate_from_task(case_id, action["id"], "actions")
    assert result["status"] == "approved"
    assert control.get_case(case_id)["state"] == "actions_approved"
    # Resume may return to checks, never automatically re-execute an approved PoC.
    control.stop(case_id)
    assert control.resume(case_id)["state"] == "scope_approved"


def test_old_action_task_cannot_survive_new_scope(control):
    case_id = control.create_case("example.com")["id"]
    body = scope_body()
    body["authorization"]["actions"] = ["checks", "poc"]
    approve(control, case_id, body)
    with control.transaction() as db:
        db.execute("UPDATE cases SET state='checks_review' WHERE id=?", (case_id,))
    action = control.propose(case_id, "actions", action_body())
    approve(control, case_id, body)
    with pytest.raises(Denied):
        control.activate_from_task(case_id, action["id"], "actions")


def test_action_task_expiration_is_checked_again_on_activation(control):
    case_id = control.create_case("example.com")["id"]
    body = scope_body()
    body["authorization"]["actions"] = ["checks", "poc"]
    approve(control, case_id, body)
    with control.transaction() as db:
        db.execute("UPDATE cases SET state='checks_review' WHERE id=?", (case_id,))
    action = control.propose(case_id, "actions", action_body())
    control.clock = lambda: NOW + 601
    with pytest.raises(Denied, match="no longer authorized"):
        control.activate_from_task(case_id, action["id"], "actions")
    stored = next(m for m in control.get_case(case_id)["manifests"] if m["id"] == action["id"])
    assert stored["status"] == "pending"
