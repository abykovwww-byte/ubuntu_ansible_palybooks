import json
from urllib.parse import parse_qs, urlsplit

import pytest

from security_lab.discovery_contracts import SourceRequest
from security_lab.osint_sources import dns_requests, initial_requests, ip_requests, parse_source, select_registry, source_catalog


def request(source, subject="example.test", url="https://source.invalid/"):
    return SourceRequest(source=source, parser=source, category="test", subject=subject, url=url)


def parsed(source, payload, subject="example.test"):
    return parse_source(request(source, subject), json.dumps(payload))


def test_planners_only_use_public_source_endpoints_and_separate_dns_types():
    plans = initial_requests("EXAMPLE.test.")
    assert len(plans) == 11
    assert all(urlsplit(p.url).scheme == "https" for p in plans)
    assert not any(urlsplit(p.url).hostname == "example.test" for p in plans)
    assert parse_qs(urlsplit(plans[0].url).query)["q"] == ["%.example.test"]
    dns = dns_requests("example.test")
    assert {parse_qs(urlsplit(p.url).query)["type"][0] for p in dns} == {"A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA"}
    assert all(parse_qs(urlsplit(p.url).query)["edns_client_subnet"] == ["0.0.0.0/0"] for p in dns)
    assert [p.source for p in ip_requests("8.8.8.8")] == ["ripestat", "internetdb"]
    assert [p.source for p in ip_requests("2001:4860:4860::8888")] == ["ripestat"]
    with pytest.raises(ValueError):
        ip_requests("127.0.0.1")


def test_ct_names_use_label_boundaries_strip_wildcards_and_stay_historical():
    result = parsed("crtsh", [{"name_value": "*.API.example.test\nexample.test.evil.test\nnotexample.test\nwww.example.test\nwww.example.test"}])
    assert {o.value for o in result.observations} == {"api.example.test", "www.example.test"}
    assert all(o.attribution == "candidate" and o.freshness == "historical" for o in result.observations)
    assert all(r.pointer.startswith("$[0].name_value") for r in result.relations)
    assert parsed("crtsh", [{}]).status == "partial"
    bounded = parsed("crtsh", [{"name_value": f"s{i}.example.test"} for i in range(201)])
    assert bounded.truncated and bounded.status == "partial" and len(bounded.observations) == 200


def test_wayback_filters_unrelated_credentials_and_marks_capped_history():
    result = parsed("wayback", [["timestamp", "original"], ["20200101", "https://api.example.test/old"],
                               ["20210101", "https://example.test.evil.test/x"], ["20210101", "https://user:secret@example.test/x"]])
    assert {o.value for o in result.observations} == {"api.example.test", "https://api.example.test/old"}
    assert result.status == "partial"
    assert all(o.freshness == "historical" for o in result.observations)
    assert parsed("wayback", []).status == "empty"
    assert parsed("wayback", [["badheader"]]).status == "error"
    assert parsed("wayback", [["original"], *[["https://example.test/" + str(i)] for i in range(200)]]).truncated


def test_github_metadata_matches_are_candidates_and_partial_pages_are_visible():
    result = parsed("github_repositories", {"total_count": 200, "incomplete_results": False,
                                            "items": [{"html_url": "https://github.com/org/repo"}]})
    assert result.status == "partial" and result.truncated
    assert result.observations[0].kind == "repository" and result.observations[0].attribution == "candidate"
    assert "does not prove ownership" in result.relations[0].basis
    zero = {"total_count": 0, "incomplete_results": False, "items": []}
    assert parsed("github_repositories", zero).status == "empty"
    assert parsed("github_repositories", {**zero, "incomplete_results": True}).status == "partial"
    assert parsed("github_repositories", {"message": "bad credentials"}).status == "error"


@pytest.mark.parametrize("code,status", [(0, "empty"), (3, "empty"), (2, "error"), (5, "error")])
def test_dns_failure_is_not_zero_results(code, status):
    assert parsed("dns", {"Status": code}).status == status


def test_dns_links_outside_seed_as_providers_not_owned_assets():
    plan = dns_requests("example.test")[3]
    result = parse_source(plan, json.dumps({"Status": 0, "Answer": [
        {"type": 15, "data": "10 mail.provider.test."}, {"type": 1, "data": "8.8.8.8"},
        {"type": 16, "data": '"verification=opaque"'}]}))
    assert result.status == "success"
    assert next(o for o in result.observations if o.value == "mail.provider.test").attribution == "shared_provider"
    assert any(r.relation == "resolves_to" and r.target_value == "8.8.8.8" for r in result.relations)
    assert any(o.kind == "dns_record" and "opaque" in o.value for o in result.observations)
    invalid = parsed("dns", {"Status": 0, "TC": True, "Answer": [{"type": 28, "data": "8.8.8.8"}]})
    assert invalid.status == "partial" and invalid.truncated
    assert not any(o.kind == "ip" for o in invalid.observations)


def test_iana_bootstrap_longest_suffix_https_only_and_domain_boundary():
    body = json.dumps({"services": [
        [["test"], ["http://insecure.test/", "https://registry.test/rdap/"]],
        [["example.test"], ["https://more-specific.test/"]],
        [["ample.test"], ["https://wrong.test/"]],
        [["example.test"], ["https://user:secret@bad.test/", "https://127.0.0.1/", "https://localhost/"]],
    ]})
    plan = select_registry("api.example.test", body)
    assert plan.url == "https://more-specific.test/domain/api.example.test"
    assert plan.source == "rdap_domain"
    assert select_registry("other.invalid", body) is None
    assert parse_source(request("rdap_bootstrap"), body).status == "success"
    with pytest.raises(ValueError):
        select_registry("example.test", "{}")


def test_rdap_keeps_metadata_and_delegation_without_contact_or_owner_claim():
    result = parsed("rdap_domain", {"objectClassName": "domain", "ldhName": "EXAMPLE.test",
        "status": ["active"], "events": [{"eventAction": "registration", "eventDate": "2020-01-01T00:00:00Z"}],
        "nameservers": [{"ldhName": "ns.provider.test"}],
        "entities": [{"vcardArray": ["vcard", [["fn", {}, "text", "Private Person"]]]}]})
    assert result.status == "success"
    assert not any("Private Person" in o.value for o in result.observations)
    assert next(o for o in result.observations if o.value == "ns.provider.test").attribution == "shared_provider"
    assert parsed("rdap_domain", {"objectClassName": "domain", "ldhName": "different.test"}).status == "error"


def test_routing_is_provider_relationship_and_never_prefix_expansion():
    result = parsed("ripestat", {"status": "ok", "data": {"prefix": "8.8.8.0/24", "asns": [15169]}}, "8.8.8.8")
    assert result.status == "success"
    assert {o.kind for o in result.observations} == {"network", "asn"}
    assert all(o.attribution == "shared_provider" for o in result.observations)
    assert parsed("ripestat", {"status": "ok", "data": {"prefix": "1.1.1.0/24", "asns": []}}, "8.8.8.8").status == "error"


def test_internetdb_is_historical_unverified_and_requires_same_ip():
    result = parsed("internetdb", {"ip": "8.8.8.8", "ports": [443], "hostnames": ["cohost.other.test"],
                                  "vulns": ["CVE-2020-0001"], "cpes": [], "tags": []}, "8.8.8.8")
    assert result.status == "success"
    assert all(o.freshness == "historical" and o.attribution == "candidate" for o in result.observations)
    assert any(o.kind == "vulnerability_claim" for o in result.observations)
    assert all("unverified" in r.basis for r in result.relations)
    assert parsed("internetdb", {"ip": "1.1.1.1"}, "8.8.8.8").status == "error"
    assert parsed("internetdb", {"detail": "No information available"}, "8.8.8.8").status == "error"  # transport handles HTTP404


def test_malformed_sources_and_optional_tools_have_honest_coverage():
    for source in ("crtsh", "wayback", "dns", "github_repositories", "rdap_domain", "ripestat", "internetdb"):
        assert parse_source(request(source), "<html>upstream error</html>").status == "error"
    assert parse_source(request("unknown"), "{}").status == "error"
    catalog = source_catalog()
    optional = {row["source"]: row for row in catalog if not row["implemented"]}
    assert set(optional) == {"dnsdumpster", "github_code", "searxng"}
    subfinder = next(row for row in catalog if row['source'] == 'subfinder')
    assert subfinder['implemented_method'] == 'host_configured_compose_cli'
    assert subfinder['status'] == 'not_attempted'
    assert all(row["status"] == "not_attempted" for row in optional.values())
    assert all(row["subscription_required"] is False for row in catalog)
