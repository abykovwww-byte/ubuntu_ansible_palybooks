import pytest
from pydantic import ValidationError

from security_lab.contracts import NetworkTarget, Scope, digest


def manifest():
    return {
        'schema_version': 1,
        'targets': [{'host': 'example.test', 'scheme': 'https', 'port': 443,
                     'paths': ['/'], 'excluded_paths': [], 'methods': ['GET', 'HEAD']}],
        'authorization': {'schema_version': 1, 'reference': 'lab:synthetic', 'owner': 'fixture',
                          'basis': 'offline contract tests', 'hosts': ['example.test'], 'actions': ['checks'],
                          'valid_from': 1000, 'valid_until': 2000},
        'expires_at': 2000, 'allowed_actions': ['checks'],
        'budget': {'seconds': 100, 'rps': 2, 'concurrency': 1, 'jobs': 2}}


def test_legacy_http_v1_manifest_dump_and_hash_are_byte_compatible():
    old = manifest()
    parsed = Scope.model_validate(old)
    assert parsed.network_targets == []
    assert parsed.model_dump(mode='json') == old
    assert digest(parsed.model_dump(mode='json')) == digest(old)
    explicit_empty = Scope.model_validate({**old, 'network_targets': []})
    assert explicit_empty.model_dump(mode='json') == old
    assert Scope.model_validate_json(parsed.model_dump_json()).model_dump(mode='json') == old


def test_explicit_network_permission_is_hashed_and_separate_from_http_permission():
    old = manifest()
    new = Scope.model_validate({**old, 'network_targets': [{'host': 'example.test', 'tcp_ports': [443, 22, 22]}]})
    dumped = new.model_dump(mode='json')
    assert dumped['targets'] == old['targets']
    assert dumped['network_targets'] == [{'host': 'example.test', 'tcp_ports': [22, 443]}]
    assert dumped['allowed_actions'] == ['checks']
    assert digest(dumped) != digest(old)
    assert Scope.model_validate(dumped).model_dump(mode='json') == dumped


def test_optional_network_address_grants_preserve_existing_hash_when_empty():
    old = {**manifest(), 'network_targets': [{'host': 'example.test', 'tcp_ports': [22]}]}
    assert Scope.model_validate(old).model_dump(mode='json') == old
    empty = {**manifest(), 'network_targets': [{'host': 'example.test', 'tcp_ports': [22], 'addresses': []}]}
    assert digest(Scope.model_validate(empty).model_dump(mode='json')) == digest(old)
    grant = {**manifest(), 'network_targets': [{'host': 'example.test', 'tcp_ports': [22],
                                               'addresses': ['172.18.0.3']}]}
    parsed = Scope.model_validate(grant)
    assert parsed.model_dump(mode='json') == grant
    assert digest(parsed.model_dump(mode='json')) != digest(old)


def test_network_only_scope_needs_no_fabricated_http_target():
    data = manifest()
    data.pop('targets')
    data['network_targets'] = [{'host': 'EXAMPLE.test.', 'tcp_ports': [22]}]
    parsed = Scope.model_validate(data)
    assert parsed.targets == [] and parsed.network_targets[0].host == 'example.test'


@pytest.mark.parametrize('ports', [[], [True], [0], [65536], ['443'], ['1-100'], [22.0]])
def test_network_scope_requires_explicit_strict_tcp_ports(ports):
    with pytest.raises(ValidationError):
        NetworkTarget(host='example.test', tcp_ports=ports)


@pytest.mark.parametrize('extra', [{'udp_ports': [53]}, {'protocol': 'udp'}, {'cidr': '8.8.8.0/24'},
                                 {'paths': ['/']}, {'methods': ['GET']}, {'flags': ['-sV']}])
def test_network_scope_does_not_accept_http_or_arbitrary_scan_parameters(extra):
    with pytest.raises(ValidationError):
        NetworkTarget(host='example.test', tcp_ports=[22], **extra)


def test_network_targets_must_be_authorized_and_duplicate_hosts_merged():
    for targets in ([{'host': 'other.test', 'tcp_ports': [22]}],
                    [{'host': 'example.test', 'tcp_ports': [22]}, {'host': 'example.test', 'tcp_ports': [443]}]):
        with pytest.raises(ValidationError):
            Scope.model_validate({**manifest(), 'network_targets': targets})


def test_empty_scope_is_denied_and_network_permission_cannot_enable_poc():
    for data in ({**manifest(), 'targets': []},
                 {**manifest(), 'network_targets': [{'host': 'example.test', 'tcp_ports': [22]}],
                  'allowed_actions': ['checks', 'poc']}):
        with pytest.raises(ValidationError):
            Scope.model_validate(data)
