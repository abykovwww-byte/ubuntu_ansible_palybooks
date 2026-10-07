import hashlib
import json

import pytest

from security_lab.contracts import Denied
from security_lab.controller import Controller
from security_lab.discovery_contracts import Observation, Relation, SourceRequest, SourceResult
from security_lab.reports import html_report


NOW = 1_800_000_000


@pytest.fixture
def control(tmp_path):
    instance = Controller(tmp_path / "store", "operator", clock=lambda: NOW)
    yield instance
    instance.close()


def result(*, body='{"names":["api.example.test"]}', status="success", attribution="candidate", freshness="historical", synthetic=True):
    success = status in {"success", "partial"}
    return SourceResult(
        request=SourceRequest(source="fixture:ct", category="subdomains", url="https://public.example.test/search?q=example.test",
                              parser="ct", subject="example.test"),
        collected_at=float(NOW), status=status, body=body,
        body_hash=hashlib.sha256(body.encode()).hexdigest(), synthetic=synthetic,
        reason="limited response" if status != "success" else "",
        observations=[Observation(kind="hostname", value="api.example.test", attribution=attribution, freshness=freshness)] if success else [],
        relations=[Relation(source_kind="hostname", source_value="example.test", relation="certificate_name",
                            target_kind="hostname", target_value="api.example.test", basis="Certificate listed this name",
                            freshness=freshness)] if success else [],
    )


def begin(control):
    case_id = control.create_case("example.test")["id"]
    return case_id, control.begin_discovery(case_id)["id"]


def scope_body():
    return {"targets": [{"host": "example.test"}], "expires_at": NOW + 3600,
            "authorization": {"reference": "fixture:authorization", "owner": "operator", "basis": "Offline fixture",
                              "hosts": ["example.test"], "valid_from": NOW - 1, "valid_until": NOW + 86400}}


def test_receipts_deduplicate_entities_but_preserve_observation_history(control):
    case_id, run_id = begin(control)
    first = control.record_source_result(case_id, run_id, result())
    second = control.record_source_result(case_id, run_id, result(attribution="shared_provider", freshness="current_observation"))
    assert first != second
    control.finish_discovery(case_id, run_id)
    case = control.export(case_id)
    assert len(case["surface"]) == 2 and len(case["links"]) == 1
    assert len(case["observations"]) == 2 and len(case["link_observations"]) == 2
    assert {o["freshness"] for o in case["observations"]} == {"historical", "current_observation"}
    assert {o["attribution"] for o in case["observations"]} == {"candidate", "shared_provider"}
    api_node = next(n for n in case["surface"] if n["value"] == "api.example.test")
    assert api_node["attribution"] == "unknown"
    assert api_node["attribution_history"] == ["candidate", "shared_provider"]
    assert api_node["ownership_proven"] is False
    assert set(json.loads(case["links"][0]["evidence_ids"])) == {first, second}
    assert all(set(json.loads(n["evidence_ids"])) == {first, second} for n in case["surface"])
    assert case["source_receipts"][0]["request_url"].endswith("q=example.test")
    assert case["state"] == "discovery_review" and case["manifests"] == []
    assert "body" not in control.get_case(case_id)["source_receipts"][0]


@pytest.mark.parametrize("status", ["empty", "error", "blocked", "rate_limited", "not_attempted"])
def test_collection_failure_is_explicit_not_an_empty_surface_claim(control, status):
    case_id, run_id = begin(control)
    control.record_source_result(case_id, run_id, result(status=status))
    case = control.export(case_id)
    assert case["surface"] == []
    assert case["coverage"][0]["status"] == status
    assert case["source_receipts"][0]["status"] == status
    assert case["evidence"][0]["status"] == status


def test_stop_blocks_inflight_late_write_and_resume_does_not_rerun(control):
    case_id, run_id = begin(control)
    control.stop(case_id)
    assert not control.run_active(case_id, run_id)
    with pytest.raises(Denied, match="no longer active"):
        control.record_source_result(case_id, run_id, result())
    finished = control.finish_discovery(case_id, run_id, "interrupted", "MCP connection shutdown")
    assert finished["status"] == "cancelled"
    assert control.get_case(case_id)["state"] == "paused"
    case = control.resume(case_id)
    assert case["state"] == "discovery_review"
    assert case["discovery_runs"][0]["status"] == "cancelled"
    assert case["source_receipts"] == []


def test_restart_marks_interrupted_preserves_receipts_and_scope(tmp_path):
    root = tmp_path / "store"
    control = Controller(root, "operator", clock=lambda: NOW)
    case_id = control.create_case("example.test")["id"]
    scope = control.propose(case_id, "scope", scope_body())
    control.activate_from_task(case_id, scope["id"], "scope")
    run_id = control.begin_discovery(case_id)["id"]
    receipt = control.record_source_result(case_id, run_id, result())
    control.close()
    control = Controller(root, "operator", clock=lambda: NOW + 10)
    try:
        case = control.export(case_id)
        assert case["state"] == "discovery_review"
        assert case["discovery_runs"][0]["status"] == "interrupted"
        assert case["source_receipts"][0]["id"] == receipt
        assert case["manifests"][0]["hash"] == scope["hash"]
        assert case["manifests"][0]["status"] == "approved"
        assert not control.run_active(case_id, run_id)
        assert case["jobs"] == []
    finally:
        control.close()


def test_receipt_hash_tampering_rolls_back_without_evidence(control):
    case_id, run_id = begin(control)
    bad = result().model_dump()
    bad["body"] = "tampered"
    with pytest.raises(Denied, match="hash mismatch"):
        control.record_source_result(case_id, run_id, bad)
    assert control.export(case_id)["evidence"] == []


def test_scope_changes_and_activation_unavailable_during_discovery(control):
    case_id = control.create_case("example.test")["id"]
    scope = control.propose(case_id, "scope", scope_body())
    control.begin_discovery(case_id)
    with pytest.raises(Denied):
        control.propose(case_id, "scope", scope_body())
    with pytest.raises(Denied):
        control.activate_from_task(case_id, scope["id"], "scope")


def test_receipt_budget_rolls_back_101st_response(control):
    case_id, run_id = begin(control)
    for _ in range(100):
        control.record_source_result(case_id, run_id, result(status="empty"))
    with pytest.raises(Denied, match="receipt budget"):
        control.record_source_result(case_id, run_id, result())
    case = control.export(case_id)
    assert len(case["source_receipts"]) == 100 and case["surface"] == []


def test_entity_limit_rolls_back_receipt_and_all_nodes(control):
    case_id, run_id = begin(control)
    data = result().model_dump()
    data["observations"] = [Observation(kind="hostname", value=f"host{i}.example.test").model_dump() for i in range(2000)]
    # Relation endpoints count too; omitting them from observations cannot evade the limit.
    with pytest.raises(Denied, match="entity budget"):
        control.record_source_result(case_id, run_id, data)
    case = control.export(case_id)
    assert case["evidence"] == [] and case["surface"] == [] and case["links"] == []


def test_error_receipt_cannot_claim_successful_observations(control):
    case_id, run_id = begin(control)
    data = result().model_dump()
    data["status"] = "error"
    with pytest.raises(Denied, match="cannot publish observations"):
        control.record_source_result(case_id, run_id, data)
    assert control.export(case_id)["evidence"] == []


def test_report_evidence_navigation_and_untrusted_content_escaping(control):
    case_id, run_id = begin(control)
    receipt = control.record_source_result(case_id, run_id, result(body='<script>alert("x")</script>', synthetic=False))
    control.record_source_result(case_id, run_id, result(status="rate_limited"))
    control.finish_discovery(case_id, run_id, "partial", "rate limit")
    report = html_report(control.export(case_id))
    assert '<script>' not in report and '&lt;script&gt;' in report
    assert f'href="#e-{receipt}"' in report and f'id="e-{receipt}"' in report
    assert 'href="#n-' in report and 'historical' in report
    assert 'rate_limited' in report and 'shared' in report.lower()
    assert 'Live discovery и pentest не выполнялись' not in report
    assert 'default-src' in report and 'script-src' not in report


def test_interrupted_finish_and_repeated_shutdown_do_not_revive_case(control):
    case_id, run_id = begin(control)
    receipt = control.record_source_result(case_id, run_id, result())
    first = control.finish_discovery(case_id, run_id, "interrupted", "MCP lifespan cancelled")
    assert first["status"] == "interrupted"
    assert control.get_case(case_id)["state"] == "discovery_review"
    control.stop(case_id)
    assert control.finish_discovery(case_id, run_id, "completed") == first
    case = control.get_case(case_id)
    assert case["state"] == "paused"
    assert case["source_receipts"][0]["id"] == receipt


def test_terminal_finish_never_overwrites_a_new_discovery_run(control):
    case_id, old_run = begin(control)
    control.finish_discovery(case_id, old_run)
    new_run = control.begin_discovery(case_id)["id"]
    old = control.finish_discovery(case_id, old_run, "interrupted")
    assert old["status"] == "completed"
    assert control.run_active(case_id, new_run)
    other_case = control.create_case("other.example.test")["id"]
    with pytest.raises(Denied, match="not found"):
        control.finish_discovery(other_case, old_run)
    with pytest.raises(Denied, match="not found"):
        control.finish_discovery(case_id, "missing")


def test_report_exposes_unimplemented_sources_and_does_not_count_not_attempted_as_responses(control):
    case_id, run_id = begin(control)
    # Non-synthetic means this receipt describes an absent attempt, not real fetched data.
    control.record_source_result(case_id, run_id, result(status="not_attempted", body="", synthetic=False))
    report = html_report(control.export(case_id))
    assert 'HTTP-ответов внешних источников: 0' in report
    assert 'Записей с пробелами сбора: 1' in report
    assert 'из них not_attempted: 1' in report
    for source in ["dnsdumpster", "subfinder", "github_code", "searxng", "seed_http"]:
        assert source in report
    assert "not_implemented" in report


def test_svg_map_is_bounded_escaped_and_links_back_to_full_evidence(control):
    import xml.etree.ElementTree as ET

    case_id, run_id = begin(control)
    malicious = '<img src=x onerror="alert(1)">'
    data = result().model_dump()
    data["observations"] += [Observation(kind="domain", value=f"node{i}.example.test").model_dump() for i in range(35)]
    data["observations"].append(Observation(kind="repository", value=malicious).model_dump())
    data["relations"].append(Relation(source_kind="hostname", source_value="example.test", relation=malicious,
                                     target_kind="repository", target_value=malicious, basis="Synthetic escaping test").model_dump())
    receipt = control.record_source_result(case_id, run_id, data)
    report = html_report(control.export(case_id))
    svg_text = report[report.index('<svg '):report.index('</svg>') + len('</svg>')]
    svg = ET.fromstring(svg_text)
    ns = {"s": "http://www.w3.org/2000/svg"}
    rectangles = svg.findall('.//s:rect', ns)
    assert 1 <= len(rectangles) <= 24
    assert len(rectangles) < len(control.get_case(case_id)["surface"])
    assert all('class' in rectangle.attrib and rectangle.attrib['class'] == 'map-node' for rectangle in rectangles)
    node_destinations = [a.attrib['href'] for a in svg.findall('.//s:a', ns) if a.attrib['href'].startswith('#n-')]
    assert node_destinations and all(f'id="{destination[1:]}"' in report for destination in node_destinations)
    assert any(a.attrib['href'] == f'#e-{receipt}' for a in svg.findall('.//s:a', ns))
    assert not svg.findall('.//s:img', ns) and '<img src=x' not in svg_text
    assert '&lt;img' in svg_text and 'alert(1)' in svg_text
    assert 'node34.example.test' in report  # Omitted overview nodes remain in full table.
    assert 'не расширяет scope' in report and 'Обзор:' in report
    assert '<script' not in report and '<image' not in svg_text
