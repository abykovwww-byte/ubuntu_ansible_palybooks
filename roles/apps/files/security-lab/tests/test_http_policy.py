import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied, digest
from security_lab.http_policy import HTTPJobPolicy, Lease, LeaseClock, request_permitted
from security_lab.nft_policy import render_job_firewall, revoke_job_firewall
from test_controller import NOW, scope_body


def policy_body():
    scope = scope_body(targets=[{"host": "example.com", "paths": ["/public"],
                                "excluded_paths": ["/public/admin"], "methods": ["GET"]}])
    from security_lab.contracts import Scope
    scope = Scope.model_validate(scope).model_dump(mode="json")
    return dict(job_id="a" * 32, generation=2, scope_hash=digest(scope), scope=scope,
                upstreams=[{"host": "example.com", "port": 443, "addresses": ["93.184.216.34"]}],
                worker_addresses=["172.30.51.3", "fd00:51::3"], deadline=float(NOW + 60))


@pytest.fixture
def policy():
    return HTTPJobPolicy.model_validate(policy_body())


def request(policy, **overrides):
    args = dict(scheme="https", host="example.com", port=443, method="GET", raw_path=b"/public/info?q=1",
                host_headers=["example.com"], connect=("example.com", 443), sni="example.com")
    args.update(overrides)
    request_permitted(policy, **args)


def test_allowed_and_h2_service(policy):
    request(policy)
    request(policy, host_headers=[], http_authority="example.com:443")


@pytest.mark.parametrize("overrides", [
    {"raw_path": b"/public/admin"}, {"raw_path": b"/publicity"}, {"raw_path": b"/public/%2e%2e/admin"},
    {"raw_path": b"/public/%252fadmin"}, {"raw_path": b"/public/../admin"}, {"raw_path": b"/public//admin"},
    {"raw_path": b"/public;anything"}, {"raw_path": b"/public\x00"}, {"raw_path": b"/public\\admin"},
    {"method": "POST"}, {"host_headers": ["evil.com"]}, {"host_headers": ["example.com", "example.com"]},
    {"host_headers": []}, {"http_authority": "evil.com"}, {"sni": "evil.com"}, {"sni": None},
    {"connect": ("evil.com", 443)}, {"port": 8443}, {"host": "example.com.evil.com"}, {"scheme": "http"},
    {"raw_path": b"https://example.com/public"},
])
def test_bypass_denied(policy, overrides):
    with pytest.raises(Denied):
        request(policy, **overrides)


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "::1", "fe80::1", "0.0.0.0", "10.0.0.4"])
def test_nonpublic_upstream_never_inherited(address):
    body = policy_body()
    body["upstreams"][0]["addresses"] = [address]
    with pytest.raises(ValidationError):
        HTTPJobPolicy.model_validate(body)


def test_exact_private_fixture_exception_and_dns_pin(policy):
    body = policy_body()
    body["upstreams"][0]["addresses"] = ["172.30.52.3"]
    body["lab_private_addresses"] = ["172.30.52.3"]
    lab = HTTPJobPolicy.model_validate(body)
    assert lab.pin("example.com", 443, ["172.30.52.3"]) == "172.30.52.3"
    for answers in (["172.30.52.4"], ["172.30.52.3", "127.0.0.1"], []):
        with pytest.raises(Denied):
            lab.pin("example.com", 443, answers)


def lease(policy, **extra):
    value = dict(job_id=policy.job_id, generation=policy.generation, scope_hash=policy.scope_hash,
                 sequence=1, valid_until=float(NOW+30))
    value.update(extra)
    return Lease(**value)


def test_monotonic_expiry_and_replay(policy):
    wall, mono = [float(NOW)], [100.0]
    clock = LeaseClock(policy, clock=lambda: wall[0], monotonic=lambda: mono[0])
    clock.update(lease(policy))
    clock.check()
    with pytest.raises(Denied):
        clock.update(lease(policy))
    wall[0] -= 100
    mono[0] += 31
    with pytest.raises(Denied):
        clock.check()


def test_revocation_is_final(policy):
    clock = LeaseClock(policy, clock=lambda: NOW, monotonic=lambda: 100)
    clock.update(lease(policy, revoked=True))
    with pytest.raises(Denied):
        clock.check()
    with pytest.raises(Denied):
        clock.update(lease(policy, sequence=2))


def test_firewall_expiry_covers_existing_packets_and_ipv6(policy):
    rules = render_job_firewall(policy, lease(policy), NOW)
    assert "table inet sl_job" in rules and "timeout 30000ms" in rules
    assert "fd00:51::3" in rules
    assert "ip6 daddr @worker6 tcp sport 8080 accept" in rules
    assert "ct state established accept" not in rules.replace("@upstream4 ct state established accept", "").replace("@upstream6 ct state established accept", "")
    assert "chain forward" in rules and "policy drop" in rules
    revoked = render_job_firewall(policy, lease(policy, revoked=True), NOW)
    assert "elements =" not in revoked
    assert "flush ruleset" not in rules + revoke_job_firewall()
