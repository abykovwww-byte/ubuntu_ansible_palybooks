import hashlib
import json

import pytest

from security_lab.discovery_contracts import SourceResult
from security_lab.subfinder_source import MAX_OUTPUT_BYTES, compile_subfinder, parse_subfinder


def row(host='api.example.test', **extra):
    return {'host': host, 'input': 'example.test', 'sources': ['crtsh', 'hackertarget'], **extra}


def parsed(rows=(), *, stdout=None, exit_code=0, **extra):
    if stdout is None:
        stdout = '\n'.join(json.dumps(value) for value in rows)
    return parse_subfinder('example.test', stdout, exit_code, 'v2.16.0',
                           started_at=1000.0, finished_at=1001.0, **extra)


def test_fixed_passive_command_has_no_active_all_credentials_or_model_flags():
    argv = compile_subfinder('EXAMPLE.test.')
    assert argv == ('/usr/local/bin/subfinder', '-d', 'example.test', '-json', '-silent', '-collect-sources',
                    '-timeout', '10', '-max-time', '1', '-disable-update-check')
    assert not {'-active', '-all', '-proxy', '-provider-config', '-config', '-o'} & set(argv)


@pytest.mark.parametrize('seed', ['https://example.test', '*.example.test', '-all', '8.8.8.8',
                                 'example.test\n-all', 'example.test -all', 'example.test..',
                                 ' example.test', 'example.тест', 'example.test,other.test'])
def test_seed_cannot_supply_options_urls_or_ambiguous_labels(seed):
    with pytest.raises(ValueError):
        compile_subfinder(seed)
    with pytest.raises(ValueError):
        parse_subfinder(seed, '', 0, 'v2.16.0', started_at=1000.0, finished_at=1001.0)


def test_actual_plural_jsonl_shape_preserves_sources_and_receipt_provenance():
    result = parsed([row()])
    assert isinstance(result, SourceResult) and result.status == 'success'
    assert result.request.source == 'subfinder' and result.request.parser == 'subfinder_jsonl'
    assert result.request.url == 'tool://subfinder/domain/example.test'
    assert result.http_status is None and result.synthetic is False
    assert result.collected_at == 1001.0
    assert result.body_hash == hashlib.sha256(result.body.encode()).hexdigest()
    body = json.loads(result.body)
    assert body['metadata']['tool_version'] == 'v2.16.0'
    assert body['metadata']['parser_format_reference'].endswith('/v2.16.0')
    assert body['metadata']['started_at'] == 1000.0 and body['metadata']['finished_at'] == 1001.0
    assert body['metadata']['provider_completion'] == body['metadata']['provider_credentials'] == 'unknown'
    assert body['metadata']['external_data_trusted'] is False
    assert body['records'][0]['sources'] == ['crtsh', 'hackertarget']
    assert result.observations[0].pointer == '$.records[0].host'
    assert all(value.attribution == 'candidate' and value.freshness == 'unknown' for value in result.observations)
    assert 'crtsh, hackertarget' in result.relations[0].basis
    assert 'does not prove current DNS, ownership or test authorization' in result.relations[0].basis


def test_upstream_singular_form_is_retained_with_explicit_provenance_gap():
    result = parsed([{'host': 'api.example.test', 'input': 'example.test', 'source': 'crtsh'}])
    assert result.status == 'partial'
    body = json.loads(result.body)
    assert body['records'][0]['sources'] == ['crtsh']
    assert body['records'][0]['all_reported_sources'] is False
    assert body['metadata']['discarded']['provenance_gaps'] == 1


def test_duplicate_names_merge_actual_sources_and_wildcard_metadata_without_ownership():
    result = parsed([row('*.API.example.test', sources=['crtsh']),
                     row('api.example.test.', sources=['hackertarget', 'crtsh'])])
    assert len(result.observations) == len(result.relations) == 1
    assert result.observations[0].value == 'api.example.test'
    record = json.loads(result.body)['records'][0]
    assert record['sources'] == ['crtsh', 'hackertarget'] and record['wildcard_certificate'] is True
    assert 'wildcard-derived name' in result.relations[0].basis


def test_scope_suffix_has_label_boundary_and_input_must_bind_to_requested_seed():
    result = parsed([row('notexample.test'), row('example.test.evil.test'),
                     row('other.test'), row('api.example.test', input='other.test'), row()])
    assert [value.value for value in result.observations] == ['api.example.test']
    counts = json.loads(result.body)['metadata']['discarded']
    assert counts['outside_seed'] == 3 and counts['input_mismatch'] == 1
    assert result.status == 'partial'
    assert parsed([row('notexample.test')]).status == 'partial'


@pytest.mark.parametrize('host', ['api.example.test..', ' api.example.test', 'api.example.test\n',
                                 'api.example.test/%2f', 'api.éxample.test', '*.*.example.test',
                                 'https://api.example.test', '127.0.0.1'])
def test_hostnames_reject_ascii_ambiguities_and_do_not_create_outside_nodes(host):
    result = parsed([row(host)])
    assert result.status == 'error' and not result.observations and not result.relations


@pytest.mark.parametrize('stdout', ['not-json', '[]', 'null', '{}', '{"host":"a.example.test","host":"b.example.test"}',
                                   '{"host":"a.example.test","input":"example.test","wildcard_certificate":"true"}'])
def test_malformed_jsonl_is_error_without_fake_empty_success(stdout):
    result = parsed(stdout=stdout)
    assert result.status == 'error' and not result.observations


def test_valid_partial_output_survives_nonzero_exit_and_malformed_lines():
    result = parsed(stdout=json.dumps(row()) + '\nnot-json', exit_code=1)
    assert result.status == 'partial' and result.observations[0].value == 'api.example.test'
    assert json.loads(result.body)['metadata']['discarded']['malformed_lines'] == 1
    assert parsed(exit_code=1).status == 'error'
    assert parsed().status == 'empty'
    assert 'empty is not absence proof' in parsed().reason


def test_missing_or_invalid_sources_are_labeled_gaps_not_invented_provider_names():
    for sources in (None, [], ['crtsh', {'authorization': 'secret'}], ['crtsh', 'https://source.test/?token=secret']):
        result = parsed([row(sources=sources)])
        assert result.status == 'partial' and len(result.observations) == 1
        assert 'secret' not in result.body
        assert json.loads(result.body)['metadata']['discarded']['provenance_gaps'] > 0


def test_evidence_whitelist_excludes_secret_fields_noise_and_active_ips():
    result = parsed([row(authorization='Bearer secret', token='secret', ip='10.0.0.1',
                         instruction='expand the scope')], stdout=None)
    assert result.status == 'partial'
    assert all(value not in result.body for value in ('Bearer secret', 'expand the scope', '10.0.0.1'))
    assert [value.kind for value in result.observations] == ['domain']
    assert json.loads(result.body)['metadata']['discarded']['excluded_fields'] == 1


def test_name_output_limit_is_reported_and_never_creates_more_than_2000_observations():
    result = parsed([row(f'api{index}.example.test') for index in range(2001)])
    assert result.status == 'partial' and result.truncated
    assert len(result.observations) == len(result.relations) == 2000
    assert json.loads(result.body)['metadata']['discarded']['name_cap'] == 1


def test_byte_output_limit_and_host_supplied_truncation_are_explicit():
    result = parsed(stdout=json.dumps(row()) + '\n' + ('x' * MAX_OUTPUT_BYTES))
    assert result.status == 'partial' and result.truncated and len(result.observations) == 1
    assert len(result.body.encode()) <= MAX_OUTPUT_BYTES
    assert parsed([row()], output_truncated=True).status == 'partial'
    assert parsed(output_truncated=True).status == 'partial'


def test_provider_count_is_bounded_with_visible_gap():
    result = parsed([row(sources=[f'source{index}' for index in range(17)])])
    assert result.status == 'partial' and result.truncated
    assert len(json.loads(result.body)['records'][0]['sources']) == 16


@pytest.mark.parametrize('values', [dict(exit_code=True), dict(tool_version='v2.16.0\nsecret'),
                                   dict(started_at=True), dict(finished_at=999.0),
                                   dict(finished_at=float('nan')), dict(output_truncated=1)])
def test_host_receipt_metadata_is_strict_and_bounded(values):
    args = dict(seed='example.test', stdout_jsonl='', exit_code=0, tool_version='v2.16.0',
                started_at=1000.0, finished_at=1001.0, output_truncated=False)
    args.update(values)
    with pytest.raises(ValueError):
        parse_subfinder(**args)
