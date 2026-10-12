import pytest
from pydantic import ValidationError

from security_lab.contracts import Denied, Scope
from security_lab.tool_catalog import CurlCheck, NmapCheck, build_check, parse_check, preview_nmap, tool_catalog

NOW = 1_791_287_000.0


def scope():
    return Scope.model_validate({
        'targets': [{'host': 'example.test', 'scheme': 'https', 'port': 443,
                     'paths': ['/public'], 'excluded_paths': ['/public/admin'], 'methods': ['GET', 'HEAD']}],
        'authorization': {'reference': 'lab:synthetic', 'owner': 'fixture', 'basis': 'offline contract tests',
                          'hosts': ['example.test'], 'actions': ['checks'],
                          'valid_from': int(NOW) - 1, 'valid_until': int(NOW) + 100},
        'expires_at': int(NOW) + 100, 'budget': {'seconds': 100, 'rps': 2, 'concurrency': 1, 'jobs': 2}})


def network_scope():
    manifest = scope().model_dump(mode='python')
    manifest['network_targets'] = [{'host': 'example.test', 'tcp_ports': [22, 443]}]
    return Scope.model_validate(manifest)


def build(spec, **overrides):
    args = dict(stage='scope_approved', now=NOW)
    args.update(overrides)
    return build_check(spec, scope(), **args)


def test_curl_plan_has_one_exact_direct_url_and_deterministic_bounded_arguments():
    check = CurlCheck(url='https://EXAMPLE.test/public', method='GET', seconds=10)
    plan = build(check)
    assert plan.argv == (
        '/usr/bin/curl', '--disable', '--globoff', '--silent', '--show-error', '--http1.1',
        '--noproxy', '*', '--proto', '=https',
        '--max-redirs', '0', '--retry', '0', '--connect-timeout', '5', '--max-time', '10',
        '--max-filesize', '1000000', '--request', 'GET', '--include', '--url', 'https://example.test:443/public')
    assert plan.profile == 'target_http' and plan.execution_authorized is False
    assert plan.image_digest is None and plan.tool_version is None
    assert plan == build(check)
    assert '--location' not in plan.argv and '--insecure' not in plan.argv
    assert '--proxy' not in plan.argv and '--cacert' not in plan.argv
    head = build(CurlCheck(url='https://example.test/public/', method='HEAD'))
    assert '--head' in head.argv and '--request' not in head.argv


@pytest.mark.parametrize('url', [
    'https://example.test/public?x=1', 'https://example.test/public?', 'https://example.test/public#x',
    'https://name:secret@example.test/public', 'file:///etc/passwd',
    'https://example.test/public/../admin', 'https://example.test/public/%2e%2e/admin',
    'https://example.test/public/%', 'https://example.test/public;admin',
    'https://example.test//public', 'https://example.test/public\n-H Secret',
    'https://example.test/public\\admin', 'https://example.test:0/public'])
def test_curl_rejects_query_credentials_protocols_and_ambiguous_paths(url):
    with pytest.raises(ValidationError):
        CurlCheck(url=url)


@pytest.mark.parametrize('url', [
    'https://example.test/public/admin', 'https://example.test/public/admin/key',
    'https://example.test/private', 'https://example.test/publicity',
    'https://example.test.evil.test/public', 'https://api.example.test/public',
    'http://example.test/public', 'https://example.test:8443/public'])
def test_curl_cannot_widen_approved_path_host_service_or_method(url):
    with pytest.raises(Denied, match='outside'):
        build(CurlCheck(url=url))
    with pytest.raises(ValidationError):
        CurlCheck(url='https://example.test/public', method='POST')


@pytest.mark.parametrize('extra', [
    {'flags': ['--insecure']}, {'headers': {'Host': 'other.test'}}, {'image': 'attacker/image'},
    {'executable': '/bin/sh'}, {'env': {'HTTP_PROXY': 'http://other.test'}}, {'file': '/secret'},
    {'body': 'payload'}, {'shell': True}, {'seconds': '10'}, {'seconds': True}])
def test_model_cannot_supply_flags_files_images_environment_or_coerced_limits(extra):
    with pytest.raises(ValidationError):
        parse_check({'tool': 'curl', 'url': 'https://example.test/public', **extra})


@pytest.mark.parametrize('stage', ['created', 'discovering', 'discovery_review', 'actions_approved', 'paused', 'completed'])
def test_checks_require_activated_stage(stage):
    with pytest.raises(Denied, match='stage'):
        build(CurlCheck(url='https://example.test/public'), stage=stage)


def test_an_explicit_additional_batch_can_start_from_checks_review():
    plan = build(CurlCheck(url='https://example.test/public'), stage='checks_review')
    assert plan.profile == 'target_http' and plan.execution_authorized is False


def test_deadline_and_duration_remain_host_constraints():
    check = CurlCheck(url='https://example.test/public')
    with pytest.raises(Denied):
        build(check, now=NOW + 100)
    with pytest.raises(Denied, match='duration'):
        build(check, now=NOW + 90)


def test_nmap_is_refused_until_canonical_network_scope_exists():
    check = NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443])
    with pytest.raises(Denied, match='canonical network scope'):
        build(check)


def test_nmap_real_port_planner_uses_explicit_network_pins_ports_and_no_dns_scripts():
    check = NmapCheck(host='example.test', addresses=['8.8.8.8', '8.8.8.8'], ports=[443, 443], seconds=20)
    plan = build_check(check, network_scope(), stage='scope_approved', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})
    assert plan.argv == (
        '/usr/bin/nmap', '--unprivileged', '-sT', '-Pn', '-n', '--disable-arp-ping',
        '--max-retries', '0', '--max-parallelism', '1', '--max-rate', '2', '--scan-delay', '500ms',
        '--host-timeout', '20s', '-p', '443', '-oX', '-', '8.8.8.8')
    assert plan.execution_authorized is False and plan.qualification == 'pending_nmap_runner_and_receiving_side_checks'
    assert '-sV' not in plan.argv and '--script' not in plan.argv
    with pytest.raises(Denied):
        preview_nmap(check, network_scope(), stage='scope_approved', now=NOW, pinned_addresses={'example.test': ['1.1.1.1']})
    with pytest.raises(Denied):
        preview_nmap(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[80]), network_scope(),
                     stage='scope_approved', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})


@pytest.mark.parametrize('addresses', [['8.8.8.0/24'], ['example.test'], ['127.0.0.1'],
                                       ['8.8.8.8', '2001:4860:4860::8888'], ['--script=default']])
def test_nmap_rejects_ranges_hostnames_special_addresses_mixed_families_or_options(addresses):
    with pytest.raises(ValidationError):
        NmapCheck(host='example.test', addresses=addresses, ports=[443])


def test_ipv6_preview_has_explicit_family_and_catalog_never_claims_qualification():
    check = NmapCheck(host='example.test', addresses=['2001:4860:4860::8888'], ports=[443])
    plan = preview_nmap(check, network_scope(), stage='scope_approved', now=NOW,
                        pinned_addresses={'example.test': ['2001:4860:4860::8888']})
    assert plan.argv[-2:] == ('-6', '2001:4860:4860::8888')
    catalog = {entry['tool']: entry for entry in tool_catalog()}
    assert {'curl', 'nmap', 'httpx', 'katana', 'nuclei', 'zap', 'ffuf', 'testssl.sh', 'gitleaks', 'sqlmap'} <= set(catalog)
    assert all(entry['production_qualified'] is False and entry['image_digest'] is None and entry['version'] is None for entry in catalog.values())
    assert catalog['nmap']['planner_implemented'] is True
    assert all(catalog[tool]['status'] == 'pending' for tool in ['httpx', 'katana', 'nuclei', 'zap', 'ffuf', 'testssl.sh'])


def test_http_paths_do_not_grant_or_restrict_an_explicit_tcp_scan():
    check = NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[22])
    plan = build_check(check, network_scope(), stage='checking', now=NOW,
                       pinned_addresses={'example.test': ['8.8.8.8']})
    assert plan.argv[plan.argv.index('-p') + 1] == '22'
    # HTTP permission on 443 does not add port 443 to a network-only port 22 grant.
    manifest = network_scope().model_dump(mode='python')
    manifest['network_targets'][0]['tcp_ports'] = [22]
    with pytest.raises(Denied, match='TCP ports'):
        build_check(check.model_copy(update={'ports': [443]}), Scope.model_validate(manifest),
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})


@pytest.mark.parametrize('pins', [None, {}, {'other.test': ['8.8.8.8']}, {'example.test': []},
                                 {'example.test': ['127.0.0.1']}, {'example.test': ['8.8.8.0/24']},
                                 {'example.test': '8.8.8.8'}, {'example.test': [True]},
                                 {'example.test': ['8.8.8.8', '169.254.0.1']}])
def test_nmap_refuses_missing_or_invalid_host_owned_pins(pins):
    with pytest.raises(Denied, match='pins'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), network_scope(),
                    stage='checking', now=NOW, pinned_addresses=pins)


@pytest.mark.parametrize('updates', [{'seconds': True}, {'seconds': 1000}, {'ports': [True]},
                                    {'ports': ['443']}, {'addresses': ['8.8.8.0/24']},
                                    {'addresses': ['--script=all']}, {'tool': 'curl'}, {'host': '--script=all'}])
def test_nmap_model_copy_cannot_bypass_canonical_validation(updates):
    check = NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]).model_copy(update=updates)
    with pytest.raises(ValidationError):
        build_check(check, network_scope(), stage='checking', now=NOW,
                    pinned_addresses={'example.test': ['8.8.8.8']})


def test_mutated_scope_nested_ports_and_authorization_are_revalidated():
    manifest = network_scope()
    manifest.network_targets[0].tcp_ports.append(True)
    with pytest.raises(ValidationError):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), manifest,
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})
    manifest = network_scope()
    manifest.authorization.hosts.clear()
    with pytest.raises(ValidationError):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), manifest,
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})


def test_curl_model_copy_cannot_add_unapproved_method_or_url():
    for updates in ({'method': 'POST'}, {'url': 'https://example.test/public?query=1'}):
        with pytest.raises(ValidationError):
            build(CurlCheck(url='https://example.test/public').model_copy(update=updates))


@pytest.mark.parametrize('stage', ['paused', 'discovering', 'actions_approved', 'completed'])
def test_nmap_admission_obeys_stage(stage):
    with pytest.raises(Denied, match='stage'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), network_scope(),
                    stage=stage, now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})


def test_nmap_admission_obeys_total_reserved_time_and_scope_window():
    manifest = network_scope().model_dump(mode='python')
    manifest['budget']['seconds'] = 10
    with pytest.raises(Denied, match='duration'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443], seconds=11),
                    Scope.model_validate(manifest), stage='checking', now=NOW,
                    pinned_addresses={'example.test': ['8.8.8.8']})
    with pytest.raises(Denied, match='duration'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), network_scope(),
                    stage='checking', now=NOW + 71, pinned_addresses={'example.test': ['8.8.8.8']})


@pytest.mark.parametrize('extra', [{'script': 'default'}, {'flags': ['-A']}, {'udp': True},
                                  {'service_detection': True}, {'ports': ['1-65535']},
                                  {'image': 'nmap:latest'}, {'output': '/tmp/result.xml'}])
def test_nmap_only_accepts_the_fixed_port_scan_grammar(extra):
    with pytest.raises(ValidationError):
        parse_check({'tool': 'nmap', 'host': 'example.test', 'addresses': ['8.8.8.8'], 'ports': [443], **extra})


def test_multiple_pins_and_ports_are_canonical_without_implicit_port_ranges():
    check = NmapCheck(host='example.test', addresses=['8.8.8.8', '1.1.1.1', '8.8.8.8'], ports=[443, 22, 443])
    plan = build_check(check, network_scope(), stage='checks', now=NOW,
                       pinned_addresses={'example.test': ['8.8.8.8', '1.1.1.1']})
    assert plan.argv[-2:] == ('1.1.1.1', '8.8.8.8')
    assert plan.argv[plan.argv.index('-p') + 1] == '22,443'
    assert plan.argv[plan.argv.index('--max-retries') + 1] == '0'
    assert plan.argv[plan.argv.index('--max-parallelism') + 1] == '1'
    assert plan.argv[plan.argv.index('--max-rate') + 1] == '2'
    assert plan.argv[plan.argv.index('--scan-delay') + 1] == '500ms'
    assert plan.seconds == 30 and plan.max_output_bytes == 1_000_000
    assert plan.execution_authorized is False


def test_network_only_scope_compiles_nmap_and_does_not_grant_http_access():
    data = network_scope().model_dump(mode='python')
    data['targets'] = []
    manifest = Scope.model_validate(data)
    plan = build_check({'tool': 'nmap', 'host': 'example.test', 'addresses': ['8.8.8.8'], 'ports': [22]},
                       manifest, stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8']})
    assert plan.tool == 'nmap'
    with pytest.raises(Denied, match='HTTP check outside'):
        build_check(CurlCheck(url='https://example.test/'), manifest, stage='checking', now=NOW)


@pytest.mark.parametrize('address', ['10.0.0.1', '172.18.0.3', '192.168.1.88', 'fd00:1234::3'])
def test_private_scan_requires_both_explicit_approved_address_grant_and_resolver_pin(address):
    check = NmapCheck(host='example.test', addresses=[address], ports=[443])
    data = network_scope().model_dump(mode='python')
    data['network_targets'][0]['addresses'] = [address]
    approved = Scope.model_validate(data)
    plan = build_check(check, approved, stage='checking', now=NOW,
                       pinned_addresses={'example.test': [address]})
    assert plan.argv[-1] == address
    assert ('-6' in plan.argv) == (':' in address)
    with pytest.raises(Denied, match='address grants'):
        build_check(check, network_scope(), stage='checking', now=NOW,
                    pinned_addresses={'example.test': [address]})
    with pytest.raises(Denied, match='pins'):
        build_check(check, approved, stage='checking', now=NOW,
                    pinned_addresses={'example.test': ['8.8.8.8']})
    with pytest.raises(Denied, match='address grants'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), approved,
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8', address]})


@pytest.mark.parametrize('address', ['127.0.0.1', '::1', '169.254.169.254', 'fe80::1',
                                    '0.0.0.0', '::', '224.0.0.1', 'ff02::1', '240.0.0.1',
                                    '2001:db8::1', '100.64.0.1', '192.0.2.1', 'fd00::3%eth0',
                                    '::ffff:10.0.0.1'])
def test_special_addresses_cannot_be_supplied_by_spec_grant_or_resolver(address):
    with pytest.raises(ValidationError):
        NmapCheck(host='example.test', addresses=[address], ports=[443])
    data = network_scope().model_dump(mode='python')
    data['network_targets'][0]['addresses'] = [address]
    with pytest.raises(ValidationError):
        Scope.model_validate(data)
    with pytest.raises(Denied, match='pins'):
        build_check(NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443]), network_scope(),
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['8.8.8.8', address]})


def test_model_copy_private_address_still_requires_scoped_exact_grant():
    check = NmapCheck(host='example.test', addresses=['8.8.8.8'], ports=[443])
    with pytest.raises(Denied, match='address grants'):
        build_check(check.model_copy(update={'addresses': ['172.18.0.3']}), network_scope(),
                    stage='checking', now=NOW, pinned_addresses={'example.test': ['172.18.0.3']})
