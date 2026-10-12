"""Host compiler and nft boundaries; no DNS, sockets, tools or nft are run."""
from dataclasses import FrozenInstanceError, replace
import math
import socket

import pytest

from security_lab.contracts import Denied, Scope, digest
from security_lab.direct_policy import (compile_direct_execution, nft_initial_rules,
                                       nft_lease_rules, nft_revoke_rules)
from security_lab.http_policy import HTTPJobPolicy, Lease
from security_lab.tool_catalog import CurlCheck, build_check


NOW = 1_900_000_000.0


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Direct policy compiler attempted live DNS/network")
    monkeypatch.setattr(socket, "getaddrinfo", denied)
    monkeypatch.setattr(socket, "create_connection", denied)


def bundle(addresses=None, *, method="GET", private=None, seconds=15):
    scope = Scope.model_validate({
        "targets": [{"host": "fixture.example.test", "port": 8443,
                     "paths": ["/public"], "excluded_paths": ["/public/admin"],
                     "methods": ["GET", "HEAD"]},
                    {"host": "unselected.example.test", "port": 443,
                     "paths": ["/"], "methods": ["GET"]}],
        "authorization": {"reference": "lab:isolated-docker-fixture", "owner": "test",
                          "basis": "Pure compiler tests", "hosts": ["fixture.example.test", "unselected.example.test"],
                          "valid_from": int(NOW) - 1, "valid_until": int(NOW) + 100},
        "expires_at": int(NOW) + 100,
    })
    policy = HTTPJobPolicy(
        job_id="a" * 32, generation=2, scope_hash=digest(scope.model_dump(mode="json")),
        scope=scope, deadline=NOW + seconds,
        upstreams=[{"host": "fixture.example.test", "port": 8443,
                    "addresses": addresses or ["8.8.8.8", "1.1.1.1"]},
                   {"host": "unselected.example.test", "port": 443, "addresses": ["8.8.4.4"]}],
        worker_addresses=["172.30.51.3"], lab_private_addresses=private or [],
    )
    lease = Lease(job_id=policy.job_id, generation=2, scope_hash=policy.scope_hash,
                  sequence=1, valid_until=NOW + min(20, seconds))
    plan = build_check(CurlCheck(url="https://fixture.example.test:8443/public/info", method=method,
                                seconds=seconds), scope, stage="checks", now=NOW)
    return plan, policy, lease


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_direct_compile_preserves_hostname_standard_tls_and_one_ipv4_pin(method):
    plan, policy, lease = bundle(method=method)
    result = compile_direct_execution(plan, policy, lease, now=NOW)
    assert result.argv == plan.argv[:-2] + ("--resolve", "fixture.example.test:8443:1.1.1.1", "--ipv4") + plan.argv[-2:]
    assert (result.hostname, result.destination, result.port) == ("fixture.example.test", "1.1.1.1", 8443)
    assert result.policy_hash == digest(policy.model_dump(mode="json"))
    assert "8.8.8.8" not in result.argv and "8.8.4.4" not in result.argv
    assert result.argv[result.argv.index("--noproxy") + 1] == "*"
    assert all(flag not in result.argv for flag in ("--cacert", "--insecure", "--proxy", "--location", "--connect-to"))
    with pytest.raises(FrozenInstanceError):
        result.destination = "8.8.8.8"


def test_ipv4_first_and_numeric_order_with_mixed_complete_policy_pins():
    plan, policy, lease = bundle(["2001:4860:4860::8888", "8.8.8.8", "1.1.1.1"])
    assert compile_direct_execution(plan, policy, lease, now=NOW).destination == "1.1.1.1"


def test_ipv6_pin_is_bracketed_with_explicit_family_without_dns():
    plan, policy, lease = bundle(["2001:4860:4860::8888", "2001:4860:4860::8844"])
    result = compile_direct_execution(plan, policy, lease, now=NOW)
    assert result.destination == "2001:4860:4860::8844"
    assert "fixture.example.test:8443:[2001:4860:4860::8844]" in result.argv
    assert "--ipv6" in result.argv and "--ipv4" not in result.argv


def test_exact_host_owned_private_fixture_pin_is_supported():
    plan, policy, lease = bundle(["172.30.52.3"], private=["172.30.52.3"])
    assert compile_direct_execution(plan, policy, lease, now=NOW).destination == "172.30.52.3"
    changed = policy.model_copy(update={"lab_private_addresses": []})
    with pytest.raises(Denied):
        compile_direct_execution(plan, changed, lease, now=NOW)


@pytest.mark.parametrize("flags", [
    ("--insecure",), ("--location",), ("--retry", "1"), ("--header", "Host: outside.test"),
    ("--request", "POST"), ("--url", "https://outside.test/"),
    ("--resolve", "fixture.example.test:8443:8.8.4.4"),
    ("--cacert", "/custom.pem"), ("--proxy", "http://outside.test:8080"),
])
def test_arbitrary_flags_cannot_enter_even_a_valid_typed_plan(flags):
    plan, policy, lease = bundle()
    forged = plan.model_copy(update={"argv": plan.argv[:-2] + flags + plan.argv[-2:]})
    with pytest.raises(Denied, match="canonical"):
        compile_direct_execution(forged, policy, lease, now=NOW)


@pytest.mark.parametrize("update", [
    {"max_output_bytes": 10_000_000}, {"seconds": 20}, {"profile": "target_network"},
    {"tool": "nmap"}, {"execution_authorized": True}, {"image_digest": "untrusted"},
])
def test_noncanonical_plan_metadata_does_not_bypass_argument_equality(update):
    plan, policy, lease = bundle()
    with pytest.raises(Denied):
        compile_direct_execution(plan.model_copy(update=update), policy, lease, now=NOW)


@pytest.mark.parametrize("url", ["https://fixture.example.test:8443/public/admin/key",
                                 "https://fixture.example.test:8443/private",
                                 "http://fixture.example.test:8443/public/info",
                                 "https://outside.example.test:8443/public/info"])
def test_forged_url_cannot_widen_path_scheme_or_host(url):
    plan, policy, lease = bundle()
    forged = plan.model_copy(update={"argv": plan.argv[:-1] + (url,)})
    with pytest.raises(Denied):
        compile_direct_execution(forged, policy, lease, now=NOW)


@pytest.mark.parametrize("update", [
    {"job_id": "b" * 32}, {"generation": 3}, {"scope_hash": "b" * 64},
    {"sequence": 0}, {"sequence": True}, {"revoked": True},
    {"valid_until": NOW}, {"valid_until": NOW - 1}, {"valid_until": NOW + 30.001},
    {"valid_until": math.inf}, {"valid_until": math.nan},
])
def test_invalid_stale_or_expired_lease_denied_at_compile_and_membership(update):
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    changed = lease.model_copy(update=update)
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, changed, now=NOW)
    with pytest.raises(Denied):
        nft_lease_rules(execution, policy, changed, now=NOW)


def test_reconstructed_reservation_and_lease_remain_within_authorization():
    plan, policy, lease = bundle()
    policy = policy.model_copy(update={"deadline": NOW + 10})
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, lease.model_copy(update={"valid_until": NOW + 5}), now=NOW)
    with pytest.raises(Denied):
        compile_direct_execution(plan.model_copy(update={"seconds": 5}), policy, lease, now=NOW)


def test_setup_delay_shortens_host_generated_runtime_without_rejecting_original_plan():
    plan, policy, lease = bundle()
    original = tuple(plan.argv)
    execution = compile_direct_execution(plan, policy, lease, now=NOW + 0.25)
    assert execution.argv[execution.argv.index('--max-time') + 1] == '14.75'
    assert plan.argv == original and original[original.index('--max-time') + 1] == '15'
    # Remaining authorization can equal the job deadline; canonical checking
    # uses reservation time, current scope/lease checks still use launch time.
    scope = policy.scope.model_copy(update={'expires_at': int(NOW) + 15,
                                           'authorization': policy.scope.authorization.model_copy(
                                               update={'valid_until': int(NOW) + 15})})
    policy = policy.model_copy(update={'scope': scope, 'scope_hash': digest(scope.model_dump(mode='json'))})
    lease = lease.model_copy(update={'scope_hash': policy.scope_hash})
    assert compile_direct_execution(plan, policy, lease, now=NOW + 0.25).argv == execution.argv


def test_final_positive_milliseconds_are_bounded_and_submillisecond_time_is_refused():
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW + 14.998)
    remaining = policy.deadline - (NOW + 14.998)
    max_time = float(execution.argv[execution.argv.index('--max-time') + 1])
    assert 0 < max_time <= remaining and max_time <= 0.002
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, lease, now=NOW + 14.9995)


def test_long_jobs_still_require_live_lease_no_more_than_thirty_seconds():
    plan, policy, lease = bundle(seconds=40)
    lease = lease.model_copy(update={'valid_until': NOW + 30})
    assert compile_direct_execution(plan, policy, lease, now=NOW).argv
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, lease.model_copy(update={'valid_until': NOW + 30.001}), now=NOW)


def test_future_reservation_cannot_start_early_even_with_a_live_lease():
    plan, policy, lease = bundle()
    policy = policy.model_copy(update={'deadline': NOW + 15.25})
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, lease, now=NOW)


@pytest.mark.parametrize("now", [NOW - 2, NOW + 20, NOW + 40, NOW + 100, math.nan, math.inf, True])
def test_host_time_must_be_finite_and_inside_all_windows(now):
    plan, policy, lease = bundle()
    with pytest.raises(Denied):
        compile_direct_execution(plan, policy, lease, now=now)


def test_ipv4_initial_rules_are_empty_drop_default_and_established_requires_timed_membership():
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    initial = nft_initial_rules(execution)
    assert initial.count("policy drop;") == 3
    assert "flags timeout" in initial and "elements" not in initial
    assert "ct state established ip saddr . tcp sport @allowed4 accept" in initial
    assert "ct state { new, established } ip daddr . tcp dport @allowed4 accept" in initial
    assert "ct state established accept" not in initial
    for forbidden in ("flush ruleset", 'iifname "lo"', 'oifname "lo"', "udp", "dport 53", "icmpv6", "8.8.4.4", "1.1.1.1"):
        assert forbidden not in initial


def test_ipv6_ndp_is_the_only_control_plane_exception():
    plan, policy, lease = bundle(["2001:4860:4860::8888"])
    initial = nft_initial_rules(compile_direct_execution(plan, policy, lease, now=NOW))
    assert initial.count("icmpv6 type { nd-neighbor-solicit, nd-neighbor-advert }") == 2
    assert initial.count("ip6 hoplimit 255") == 2
    for forbidden in ("router-solicit", "router-advert", "echo-request", "icmpv6 accept"):
        assert forbidden not in initial


def test_replace_membership_allows_only_selected_endpoint_and_replay_never_refreshes_absolute_expiry():
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    assert nft_lease_rules(execution, policy, lease, now=NOW) == (
        "flush set inet security_lab_direct allowed4\n"
        "flush set inet security_lab_direct allowed6\n"
        "add element inet security_lab_direct allowed4 { 1.1.1.1 . 8443 timeout 15000ms }\n")
    assert "timeout 9999ms" in nft_lease_rules(execution, policy, lease, now=NOW + 5.0001)
    assert "timeout 1000ms" in nft_lease_rules(execution, policy, lease, now=NOW + 14)
    with pytest.raises(Denied):
        nft_lease_rules(execution, policy, lease, now=NOW + 20)
    assert nft_revoke_rules() == "flush set inet security_lab_direct allowed4\nflush set inet security_lab_direct allowed6\n"


def test_ipv6_membership_has_only_exact_ipv6_destination_and_port():
    plan, policy, lease = bundle(["2001:4860:4860::8888"])
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    rules = nft_lease_rules(execution, policy, lease, now=NOW)
    assert "add element inet security_lab_direct allowed6 { 2001:4860:4860::8888 . 8443 timeout 15000ms }" in rules
    assert "add element inet security_lab_direct allowed4" not in rules


@pytest.mark.parametrize("changes", [{"destination": "8.8.8.8"}, {"destination": "8.8.4.4"},
                                     {"hostname": "unselected.example.test"}, {"port": 443},
                                     {"policy_hash": "b" * 64}])
def test_membership_rejects_different_endpoint_or_policy_binding(changes):
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    with pytest.raises(Denied):
        nft_lease_rules(replace(execution, **changes), policy, lease, now=NOW)


@pytest.mark.parametrize("changes", [{"destination": "1.1.1.1; flush ruleset"}, {"destination": "127.0.0.1"},
                                     {"destination": "169.254.169.254"}, {"destination": "fe80::1"},
                                     {"port": True}, {"hostname": "fixture.example.test;"}])
def test_renderers_reject_unsafe_endpoint_or_nft_text_injection(changes):
    plan, policy, lease = bundle()
    execution = compile_direct_execution(plan, policy, lease, now=NOW)
    with pytest.raises(Denied):
        nft_initial_rules(replace(execution, **changes))
