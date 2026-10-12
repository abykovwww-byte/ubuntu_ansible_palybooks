import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied, digest
from security_lab.http_policy import HTTPJobPolicy
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
