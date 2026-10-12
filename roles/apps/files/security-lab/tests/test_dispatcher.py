import asyncio
import json
from types import SimpleNamespace

import pytest

from security_lab.contracts import Denied, digest
from security_lab.controller import Controller
from security_lab.dispatcher import Dispatcher, FixtureBackend, WorkerResult

NOW = 1800000000


def lab_scope(seconds=180):
    return {'targets': [{'host': 'fixture.example.test', 'port': 8443, 'paths': ['/public'],
                         'excluded_paths': ['/public/admin'], 'methods': ['GET', 'HEAD']}],
            'authorization': {'reference': 'lab:isolated-docker-fixture', 'owner': 'fixture',
                              'basis': 'Owned isolated lab only', 'hosts': ['fixture.example.test'],
                              'valid_from': NOW-1, 'valid_until': NOW+300},
            'expires_at': NOW+300, 'budget': {'seconds': seconds, 'rps': 2, 'concurrency': 1, 'jobs': 10}}


@pytest.fixture
def lab(tmp_path):
    c = Controller(tmp_path, 'fixture', clock=lambda: NOW)
    case = c.create_case('fixture.example.test')['id']
    manifest = c.propose(case, 'scope', lab_scope())
    c.activate_from_task(case, manifest['id'], 'scope')
    yield c, case
    c.close()


SPEC = {'tool': 'curl', 'url': 'https://fixture.example.test:8443/public/info', 'seconds': 20}


def test_dispatch_receipts_budget_and_secrets(lab):
    async def scenario():
        c, case = lab
        b, d = FixtureBackend(), None
        d = Dispatcher(c, b)
        job = await d.execute(case, SPEC)
        task = d.active[job['id']][1]
        await task
        result = c.export(case)
        assert result['state'] == 'checks_review' and result['spent'] == 20
        assert result['jobs'][0]['status'] == 'completed'
        assert b.closed
        events = [e['event'] for e in result['audit']]
        assert events.index('job_policy_bound') < events.index('job_network_acknowledged') < events.index('job_started')
        evidence = json.loads(result['evidence'][0]['body'])
        assert evidence['synthetic'] is True and evidence['access_closed'] is True
        assert 'token_hash' not in json.dumps(result)
        c.finish_job(case, job['id'], outcome='completed', result={}, synthetic=True)
        assert c.get_case(case)['spent'] == 20 and len(c.export(case)['evidence']) == 1
    asyncio.run(scenario())


def test_production_is_closed_without_consuming_budget(lab):
    c, case = lab
    with pytest.raises(Denied, match='not yet qualified'):
        asyncio.run(Dispatcher(c).execute(case, SPEC))
    assert c.get_case(case)['jobs'] == [] and c.get_case(case)['spent'] == 0


@pytest.mark.parametrize('attributes', [
    {'lab_only': True}, {'lab_only': True, 'zero_network': False},
    {'lab_only': True, 'zero_network': None}, {'lab_only': True, 'zero_network': 1},
    {'lab_only': True, 'zero_network': 'true'}, {'lab_only': False, 'zero_network': True},
    {'zero_network': True},
])
def test_lab_label_cannot_enable_unqualified_network_or_missing_marker(lab, attributes):
    c, case = lab
    class NoResolver:
        async def resolve(self, scope):
            pytest.fail('Denied backend must not trigger target DNS resolution')
    # No other backend attributes are present: refusal must precede metadata,
    # policy, installer, target DNS, job issuance and budget reservation.
    backend = SimpleNamespace(**attributes)
    with pytest.raises(Denied, match='not yet qualified'):
        asyncio.run(Dispatcher(c, backend, target_resolver=NoResolver()).execute(case, SPEC))
    assert c.get_case(case)['jobs'] == [] and c.get_case(case)['spent'] == 0


def test_zero_network_marker_false_on_fixture_subclass_denies_before_policy(lab):
    class NetworkFixture(FixtureBackend):
        zero_network = False
        def policy(self, job, scope):
            pytest.fail('Network-capable subclass must not receive a job')
    c, case = lab
    with pytest.raises(Denied, match='not yet qualified'):
        asyncio.run(Dispatcher(c, NetworkFixture()).execute(case, SPEC))
    assert c.get_case(case)['jobs'] == [] and c.get_case(case)['spent'] == 0


def test_fixture_subclass_inherits_zero_network_and_receives_direct_typed_plan(lab):
    class RecordingFixture(FixtureBackend):
        async def run(self, command):
            assert command.argv[command.argv.index('--noproxy') + 1] == '*'
            assert '--proxy' not in command.argv and '--cacert' not in command.argv
            assert '--insecure' not in command.argv and '--location' not in command.argv
            assert command.argv[command.argv.index('--retry') + 1] == '0'
            assert command.argv[-2:] == ('--url', 'https://fixture.example.test:8443/public/info')
            assert command.execution_authorized is False
            return await super().run(command)
    async def scenario():
        c, case = lab
        backend = RecordingFixture()
        d = Dispatcher(c, backend)
        job = await d.execute(case, SPEC)
        await d.active[job['id']][1]
        exported = c.export(case)
        evidence = json.loads(exported['evidence'][0]['body'])
        assert evidence['zero_network'] is True and evidence['synthetic'] is True
        assert exported['jobs'][0]['status'] == 'completed' and backend.closed
    asyncio.run(scenario())


def test_immediate_and_repeated_stop_proves_closure(lab):
    async def scenario():
        c, case = lab
        b = FixtureBackend(delay=1)
        d = Dispatcher(c, b)
        job = await d.execute(case, SPEC)
        await asyncio.gather(d.stop(case), d.stop(case))
        result = c.export(case)
        assert result['state'] == 'paused' and b.closed
        assert result['jobs'][0]['outcome'] == 'interrupted'
        assert result['jobs'][0]['evidence_id']
        assert c.db.execute('SELECT COUNT(*) FROM outbox WHERE delivered=0').fetchone()[0] == 0
        assert c.get_case(case)['spent'] == 20
    asyncio.run(scenario())


def test_install_failure_never_starts(lab):
    class Failing(FixtureBackend):
        async def install(self, policy, lease):
            raise RuntimeError('private diagnostic not to export')
        async def run(self, command):
            pytest.fail('worker must not start')
    async def scenario():
        c, case = lab
        d = Dispatcher(c, Failing())
        job = await d.execute(case, SPEC)
        await d.active[job['id']][1]
        exported = c.export(case)
        assert exported['jobs'][0]['status'] == 'failed'
        assert exported['jobs'][0]['started'] is None
        assert 'private diagnostic' not in json.dumps(exported)
    asyncio.run(scenario())


def test_renewal_ack_and_output_redaction(lab):
    class Result(FixtureBackend):
        async def run(self, command):
            await asyncio.sleep(.025)
            return WorkerResult(0, '{"password":"secret value with spaces"}')
    async def scenario():
        c, case = lab
        backend = Result()
        d = Dispatcher(c, backend, renewal_interval=.005)
        job = await d.execute(case, SPEC)
        await d.active[job['id']][1]
        exported = c.export(case)
        assert backend.renewals >= 1
        assert exported['jobs'][0]['lease_sequence'] == exported['jobs'][0]['acknowledged_sequence']
        assert 'secret value' not in json.dumps(exported)
    asyncio.run(scenario())


def test_ack_and_generation_rechecked_before_start(lab):
    c, case = lab
    job, token = c.prepare_job(case, 'worker', 'target_http', 20)
    with pytest.raises(Denied, match='acknowledged'):
        c.start_job(job['id'], token, 'worker', 'target_http')
    lease = c.bind_job_policy(job['id'], token, 'worker', 'target_http', 'a'*64, {'tool': 'curl'})
    with pytest.raises(Denied, match='acknowledgement'):
        c.acknowledge_job(job['id'], token, 'worker', 'target_http', {**lease, 'policy_hash': 'b'*64})
    ack = {**lease, 'policy_hash': 'a'*64}
    c.acknowledge_job(job['id'], token, 'worker', 'target_http', ack)
    with pytest.raises(Denied):
        c.acknowledge_job(job['id'], token, 'worker', 'target_http', ack)
    c.stop(case)
    with pytest.raises(Denied):
        c.start_job(job['id'], token, 'worker', 'target_http')


def test_lease_cannot_be_revived_and_budget_persists(lab):
    c, case = lab
    job, token = c.prepare_job(case, 'worker', 'target_http', 60)
    lease = c.bind_job_policy(job['id'], token, 'worker', 'target_http', 'a'*64, {})
    c.acknowledge_job(job['id'], token, 'worker', 'target_http', {**lease, 'policy_hash': 'a'*64})
    c.start_job(job['id'], token, 'worker', 'target_http')
    c.clock = lambda: NOW+31
    with pytest.raises(Denied):
        c.renew_job(job['id'], token, 'worker', 'target_http')
    assert c.get_case(case)['spent'] == 60


def test_restart_revokes_active_and_does_not_repeat_charge(tmp_path):
    c = Controller(tmp_path, 'fixture', clock=lambda: NOW)
    case = c.create_case('fixture.example.test')['id']
    m = c.propose(case, 'scope', lab_scope())
    c.activate_from_task(case, m['id'], 'scope')
    c.prepare_job(case, 'worker', 'target_http', 20)
    c.close()
    for _ in range(2):
        c = Controller(tmp_path, 'fixture', clock=lambda: NOW)
        data = c.get_case(case)
        assert data['spent'] == 20 and data['state'] == 'paused'
        assert data['jobs'][0]['outcome'] == 'interrupted'
        c.close()


def test_approved_manifest_tamper_denies_every_capability(lab):
    c, case = lab
    job, token = c.prepare_job(case, 'worker', 'target_http', 20)
    with c.transaction() as db:
        body = lab_scope()
        body['targets'][0]['paths'] = ['/']
        db.execute("UPDATE manifests SET body=? WHERE case_id=? AND kind='scope'", (json.dumps(body), case))
    with pytest.raises(Denied, match='integrity'):
        c.validate_capability(job['id'], token, 'worker', 'target_http')


def test_multibyte_worker_preview_fits_durable_evidence(lab):
    class UnicodeOutput(FixtureBackend):
        async def run(self, command):
            return WorkerResult(0, '\U0001f50d'*20000, '\U0001f50d'*20000)
    async def scenario():
        c, case = lab
        d = Dispatcher(c, UnicodeOutput())
        job = await d.execute(case, SPEC)
        await d.active[job['id']][1]
        status = c.get_job_status(case, job['id'])
        assert status['status'] == 'completed' and status['evidence_id']
        evidence = c.export(case)['evidence'][0]
        body = json.loads(evidence['body'])
        assert body['truncated'] is True and len(evidence['body'].encode()) < 65536
    asyncio.run(scenario())


def test_v1_uncertain_jobs_charged_once_during_migration(tmp_path):
    c = Controller(tmp_path, 'fixture', clock=lambda: NOW)
    case = c.create_case('fixture.example.test')['id']
    m = c.propose(case, 'scope', lab_scope())
    c.activate_from_task(case, m['id'], 'scope')
    c.prepare_job(case, 'worker', 'target_http', 20)
    with c.transaction() as db:
        db.execute('DELETE FROM job_runtime')
        db.execute('UPDATE cases SET spent=0')
        db.execute('PRAGMA user_version=1')
    c.close()
    for _ in range(2):
        c = Controller(tmp_path, 'fixture', clock=lambda: NOW)
        assert c.get_case(case)['spent'] == 20
        assert c.db.execute('PRAGMA user_version').fetchone()[0] == 2
        c.close()
