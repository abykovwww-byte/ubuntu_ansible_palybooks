import asyncio
import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from security_lab.contracts import Denied
from security_lab.handoff import build_handoff
from security_lab.mcp_server import build_server
from test_controller import control, NOW, approve, scope_body


def checkpoint(evidence_id=None):
    return {'objective': 'Resolve the existing F02, without repeating discovery',
            'summary': 'TCP port was already open; SSLRequest timed out; protocol remains unknown',
            'items': [{'id': 'F02', 'subject': 'example.com:5432', 'status': 'inconclusive',
                       'conclusion': 'Open port is not proof of PostgreSQL or unauthenticated access',
                       'evidence_ids': [evidence_id] if evidence_id else [],
                       'artifact_refs': ['outputs/prior-report.json'],
                       'next_step': 'Inspect prior version-light evidence before another probe'}],
            'next_steps': ['Resolve service identity']}


def test_continuity_is_append_only_revision_guarded_and_in_native_handoff(control):
    case = control.create_case('example.com')
    control.collect_fixture(case['id'])
    approve(control, case['id'], scope_body(targets=[{'host': 'example.com'}]))
    before = control.export(case['id'])
    evidence_id = before['evidence'][0]['id']
    recorded = control.record_checkpoint(case['id'], checkpoint(evidence_id), expected_revision=before['revision'])
    assert recorded['revision'] > before['revision']
    with pytest.raises(Denied, match='revision conflict'):
        control.record_checkpoint(case['id'], checkpoint(), expected_revision=before['revision'])
    snapshot, manifest = control.checks_context(case['id'])
    packet = build_handoff(snapshot, manifest, [], now=NOW)
    saved = packet['case_continuation']['checkpoint']
    assert saved['items'][0]['id'] == 'F02' and saved['items'][0]['evidence_ids'] == [evidence_id]
    assert packet['policy']['poc_allowed'] is False
    assert control.export(case['id'])['evidence'] == before['evidence']
    assert control.get_case(case['id'])['manifests'] == before['manifests']
    assert control.get_case(case['id'])['jobs'] == []
    events, cursor = [], 0
    while True:
        page = control.get_case_history(case['id'], after_sequence=cursor, limit=1)
        events += page['events']
        if page['next_after_sequence'] is None:
            break
        cursor = page['next_after_sequence']
    assert [event['sequence'] for event in events] == [row['sequence'] for row in snapshot['audit']]
    assert events[-1]['event'] == 'case_checkpoint'


def test_checkpoint_cannot_reference_foreign_evidence_or_read_foreign_manifest(control):
    first = control.create_case('example.com')['id']
    second = control.create_case('other.example.com')['id']
    control.collect_fixture(first)
    foreign = control.export(first)['evidence'][0]['id']
    with pytest.raises(Denied, match='evidence not found'):
        control.record_checkpoint(second, checkpoint(foreign), expected_revision=control.get_case(second)['revision'])
    draft, _ = approve(control, first)
    assert control.get_manifest(first, draft['id'])['body']['authorization']['reference'] == 'lab:owned-fixture'
    with pytest.raises(Denied, match='Manifest not found'):
        control.get_manifest(second, draft['id'])


def test_checkpoint_sanitizes_text_without_corrupting_json_and_does_not_authorize(control):
    case = control.create_case('example.com')
    value = checkpoint()
    value['summary'] = 'Quote " preserved\n< Set-Cookie: fixture-secret=session\nnot a permission'
    saved = control.record_checkpoint(case['id'], value, expected_revision=case['revision'])
    assert 'fixture-secret' not in json.dumps(saved)
    assert 'Quote " preserved' in saved['continuation']['checkpoint']['summary']
    assert control.get_case(case['id'])['state'] == 'created'


def test_mcp_exposes_history_checkpoint_and_exact_manifest_without_private_db_access(tmp_path):
    async def run():
        async with create_connected_server_and_client_session(build_server(tmp_path / 'cases', 'operator')) as sdk:
            names = {tool.name for tool in (await sdk.list_tools()).tools}
            assert {'get_case_history', 'record_case_checkpoint', 'get_manifest', 'start_service_check'} <= names
            case = (await sdk.call_tool('create_case', {'seed': 'example.com'})).structuredContent
            result = await sdk.call_tool('record_case_checkpoint', {'case_id': case['id'],
                'checkpoint': checkpoint(), 'expected_revision': case['revision']})
            assert not result.isError
            history = (await sdk.call_tool('get_case_history', {'case_id': case['id'], 'limit': 1})).structuredContent
            assert history['continuation']['checkpoint']['items'][0]['id'] == 'F02'
            assert history['next_after_sequence'] is not None
            denied = await sdk.call_tool('record_case_checkpoint', {'case_id': case['id'],
                'checkpoint': checkpoint(), 'expected_revision': case['revision']})
            assert denied.isError
    asyncio.run(run())
