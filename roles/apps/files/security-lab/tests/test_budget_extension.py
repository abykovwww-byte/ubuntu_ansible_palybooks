"""Explicit, append-only budget renewal; no refund, automatic approval or probe."""
import asyncio
import copy
import json
import time

import pytest

from security_lab.contracts import Denied, MAX_CASE_SECONDS
from security_lab.controller import Controller
from security_lab.handoff import build_handoff
from security_lab.native_handoff import build_native_handoff, validate_native_handoff
from test_controller import control, approve, scope_body, NOW
from test_shared_service import make_app, session, data


def auth(body=None, now=NOW):
    value = copy.deepcopy((body or scope_body())['authorization'])
    value.update(reference='lab:explicit-budget-extension', basis='Operator explicitly adds 1800 seconds for one checks run',
                 actions=['checks'], valid_from=now-1, valid_until=now+3600)
    return value


def extend(c, case, **kwargs):
    return c.propose_budget_extension(case, kwargs.pop('additional_seconds', 1800),
        kwargs.pop('authorization', auth(now=int(c.clock()))),
        kwargs.pop('expires_at', int(c.clock())+3600),
        expected_revision=kwargs.pop('expected_revision', c.get_case(case)['revision']), **kwargs)


def test_exhausted_expired_case_gets_exact_new_native_reservation_without_refund(control):
    case = control.create_case('example.com')['id']
    body = scope_body(targets=[], network_targets=[{'host':'example.com','tcp_ports':[5432],
        'addresses':['93.184.216.34']}], budget={'seconds':7200,'rps':1,'concurrency':1,'jobs':200})
    old, _ = approve(control, case, body)
    for _ in range(4):
        job, _ = control.prepare_job(case, 'fixture-worker', 'target_network', 1800)
        control.finish_job(case, job['id'], outcome='failed', result={'synthetic':True,'target_requests':0}, synthetic=True)
    before = control.export(case)
    assert before['spent'] == 7200
    with pytest.raises(Denied, match='budget exhausted'):
        control.prepare_job(case, 'worker', 'target_network', 1)
    control.clock = lambda: NOW+3601
    pending = extend(control, case)
    assert pending['status'] == 'pending' and pending['manifest']['budget']['seconds'] == 9000
    preserved = copy.deepcopy(pending['manifest'])
    for key in ('authorization','expires_at'):
        preserved[key] = old['manifest'][key]
    preserved['budget']['seconds'] = 7200
    assert preserved == old['manifest']
    with pytest.raises(Denied, match='approved scope'):
        control.prepare_job(case, 'worker', 'target_network', 1800)
    after = control.export(case)
    assert after['spent'] == 7200 and after['evidence'] == before['evidence'] and after['jobs'] == before['jobs']
    assert control.get_manifest(case,old['id'])['body'] == old['manifest']
    event = json.loads(after['audit'][-1]['details'])
    assert event['previous_manifest_id'] == old['id'] and event['manifest_id'] == pending['id']
    assert event['previous_seconds'] == event['spent_or_reserved_seconds'] == 7200
    assert event['additional_seconds'] == 1800 and event['execution_activated'] is False
    control.activate_from_task(case, pending['id'], 'scope')
    snapshot, manifest = control.checks_context(case)
    typed = build_handoff(snapshot,manifest,[],now=control.clock())
    worker = 'existing-pentestgpt-compose'
    job, token = control.prepare_job(case,worker,'target_network',1800)
    run = 'chat-'+job['id']
    native = control.native_execution_record(case,job['id'],token,worker,'target_network',run,['tcp://example.com:5432'])
    packet = build_native_handoff(typed,native)
    result = validate_native_handoff(packet,run_id=run,now=control.clock())
    assert result.reserved_seconds == 1800
    assert result.case_allowance.seconds_limit == result.case_allowance.spent_or_reserved_seconds == 9000
    assert result.case_allowance.unallocated_seconds == 0
    with pytest.raises(Denied,match='budget exhausted'):
        control.prepare_job(case,'worker','target_network',1)


@pytest.mark.parametrize('seconds', [0,-1,7201,True,1.5,'1800'])
def test_extension_rejects_invalid_delta_without_any_mutation(control, seconds):
    case = control.create_case('example.com')['id']
    approve(control,case)
    before = control.export(case)
    with pytest.raises(Denied,match='additional seconds'):
        extend(control,case,additional_seconds=seconds)
    assert control.export(case) == before


@pytest.mark.parametrize('change', [
    {'hosts':['other.example.com']}, {'owner':'other'}, {'actions':['checks','poc']},
    {'valid_until':NOW-1,'valid_from':NOW-100},
])
def test_bad_new_authority_cannot_change_scope_or_expiry(control, change):
    case = control.create_case('example.com')['id']
    approve(control,case)
    before = control.export(case)
    with pytest.raises(ValueError):
        extend(control,case,authorization={**auth(),**change})
    assert control.export(case) == before


def test_stale_revision_pending_scope_and_active_job_are_not_retried(control):
    case = control.create_case('example.com')['id']
    approve(control,case)
    stale = control.get_case(case)['revision']-1
    with pytest.raises(Denied,match='revision conflict'):
        extend(control,case,expected_revision=stale)
    job, _ = control.prepare_job(case,'worker','target_http',1)
    with pytest.raises(Denied,match='idle checks case'):
        extend(control,case)
    control.finish_job(case,job['id'],outcome='failed',result={},synthetic=True)
    pending = extend(control,case)
    before = control.export(case)
    with pytest.raises(Denied,match='latest approved scope'):
        extend(control,case)
    assert control.export(case) == before
    with pytest.raises(Denied,match='increase requires'):
        control.propose(case,'scope',pending['manifest'])


def test_direct_scope_inflation_is_denied_and_activated_extension_survives_restart(tmp_path):
    root = tmp_path/'state'
    c = Controller(root,'fixture-operator',clock=lambda:NOW)
    try:
        case = c.create_case('example.com')['id']
        inflated = scope_body(budget={'seconds':9000})
        with pytest.raises(Denied,match='increase requires'):
            c.propose(case,'scope',inflated)
        approve(c,case)
        pending = extend(c,case)
        c.activate_from_task(case,pending['id'],'scope')
        before = c.export(case)
    finally:
        c.close()
    c = Controller(root,'fixture-operator',clock=lambda:NOW)
    try:
        assert c.export(case) == before
        renewed = c.propose(case,'scope',pending['manifest'])
        c.activate_from_task(case,renewed['id'],'scope')
        for _ in range(10):
            grant = extend(c,case,additional_seconds=7200)
            c.activate_from_task(case,grant['id'],'scope')
        assert c.get_manifest(case,grant['id'])['body']['budget']['seconds'] == 81000
        before = c.export(case)
        with pytest.raises(Denied,match='supported case limit'):
            extend(c,case,additional_seconds=MAX_CASE_SECONDS-81000+1)
        assert c.export(case) == before
    finally:
        c.close()


def test_shared_mcp_proposal_is_revision_guarded_pending_and_attributed(tmp_path):
    app = make_app(tmp_path)
    async def run():
        async with app.inner.router.lifespan_context(app.inner):
            async with session(app,'opencode') as first, session(app,'codex') as second:
                names = {t.name for t in (await first.list_tools()).tools}
                assert 'propose_budget_extension' in names
                case = data(await first.call_tool('create_case',{'seed':'example.com'}))
                cid = case['id']
                now = int(time.time())
                body = scope_body(expires_at=now+600)
                body['authorization'] = auth(now=now)
                draft = data(await first.call_tool('propose_scope',{'case_id':cid,'manifest':body,'expected_revision':case['revision']}))
                data(await first.call_tool('activate_scope_from_task',{'case_id':cid,'manifest_id':draft['id']}))
                case = data(await first.call_tool('get_case',{'case_id':cid}))
                args = {'case_id':cid,'additional_seconds':1800,'authorization':auth(now=now),
                        'expires_at':now+3600,'expected_revision':case['revision']}
                outcomes = await asyncio.gather(*(s.call_tool('propose_budget_extension',args) for s in (first,second)))
                assert sum(not r.isError for r in outcomes) == 1
                assert 'revision conflict' in next(r for r in outcomes if r.isError).content[0].text
                draft = data(next(r for r in outcomes if not r.isError))
                assert draft['status'] == 'pending' and draft['manifest']['budget']['seconds'] == 9000
                case = data(await first.call_tool('get_case',{'case_id':cid}))
                assert case['spent'] == 0 and case['jobs'] == [] and case['state'] == 'discovery_review'
                history = data(await first.call_tool('get_case_history',{'case_id':cid}))
                last = json.loads(history['events'][-1]['details'])
                assert last['client'] in {'codex','opencode'} and last['execution_activated'] is False
                data(await first.call_tool('activate_scope_from_task',{'case_id':cid,'manifest_id':draft['id']}))
                denied = await second.call_tool('activate_scope_from_task',{'case_id':cid,'manifest_id':draft['id']})
                assert denied.isError
    asyncio.run(run())


def test_time_extension_retains_job_cap_and_http_restrictions(control):
    case = control.create_case('example.com')['id']
    body = scope_body(budget={'jobs':1,'rps':1,'concurrency':1},
        targets=[{'host':'example.com','paths':['/public'],'excluded_paths':['/public/private'],
                  'methods':['GET','HEAD']}])
    body['authorization']['actions'] = ['checks','poc']
    draft, _ = approve(control,case,body)
    job, _ = control.prepare_job(case,'worker','target_http',1)
    control.finish_job(case,job['id'],outcome='failed',result={},synthetic=True)
    pending = extend(control,case,authorization=auth(body))
    assert pending['manifest']['targets'] == draft['manifest']['targets']
    assert pending['manifest']['authorization']['actions'] == ['checks']
    control.activate_from_task(case,pending['id'],'scope')
    with pytest.raises(Denied,match='budget exhausted'):
        control.prepare_job(case,'worker','target_http',1)


def test_extension_and_scope_ceiling_reject_corrupt_approved_source(control):
    case = control.create_case('example.com')['id']
    draft, _ = approve(control,case)
    with control.transaction() as db:
        corrupt = copy.deepcopy(draft['manifest'])
        corrupt['budget']['seconds'] = 10000
        db.execute('UPDATE manifests SET body=? WHERE id=?',(json.dumps(corrupt),draft['id']))
    before = control.export(case)
    with pytest.raises(Denied,match='integrity failure'):
        extend(control,case)
    with pytest.raises(Denied,match='integrity failure'):
        control.propose(case,'scope',corrupt)
    assert control.export(case) == before
