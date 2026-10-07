import asyncio

import pytest

from security_lab.contracts import Denied
from security_lab.controller import Controller
from security_lab.dispatcher import Dispatcher, FixtureBackend
from security_lab.target_resolution import TargetResolver
from test_dispatcher import lab_scope, SPEC, NOW


@pytest.fixture
def case(tmp_path):
    controller = Controller(tmp_path, "fixture", clock=lambda: NOW)
    case_id = controller.create_case("fixture.example.test")["id"]
    manifest = controller.propose(case_id, "scope", lab_scope())
    controller.activate_from_task(case_id, manifest["id"], "scope")
    yield controller, case_id
    controller.close()


def resolver(callback):
    return TargetResolver(callback, clock=lambda: NOW, lab_private_addresses=("172.30.52.3", "172.30.52.4"))


def test_changed_dns_before_install_never_opens_worker(case):
    async def run():
        controller, case_id = case
        calls = 0
        async def answer(host, port):
            nonlocal calls
            calls += 1
            return ["172.30.52.3"] if calls == 1 else ["172.30.52.4"]
        backend = FixtureBackend()
        dispatcher = Dispatcher(controller, backend, target_resolver=resolver(answer))
        submitted = await dispatcher.execute(case_id, SPEC)
        await dispatcher.active[submitted["id"]][1]
        assert backend.current_policy is None and backend.closed
        assert controller.get_job_status(case_id, submitted["id"])["outcome"] == "failed"
    asyncio.run(run())


def test_dns_change_during_renewal_stops_without_new_lease(case):
    async def run():
        controller, case_id = case
        calls = 0
        async def answer(host, port):
            nonlocal calls
            calls += 1
            return ["172.30.52.3"] if calls <= 2 else ["172.30.52.4"]
        backend = FixtureBackend(delay=0.05)
        dispatcher = Dispatcher(controller, backend, renewal_interval=0.001, target_resolver=resolver(answer))
        submitted = await dispatcher.execute(case_id, SPEC)
        await dispatcher.active[submitted["id"]][1]
        assert backend.current_policy is not None and backend.closed and backend.renewals == 0
        assert controller.get_job_status(case_id, submitted["id"])["outcome"] == "failed"
    asyncio.run(run())


def test_scope_change_during_lookup_refused_before_charge(case):
    async def run():
        controller, case_id = case
        async def answer(host, port):
            manifest = controller.propose(case_id, "scope", lab_scope())
            controller.activate_from_task(case_id, manifest["id"], "scope")
            return ["172.30.52.3"]
        dispatcher = Dispatcher(controller, FixtureBackend(), target_resolver=resolver(answer))
        with pytest.raises(Denied, match="stale"):
            await dispatcher.execute(case_id, SPEC)
        assert controller.get_case(case_id)["jobs"] == [] and controller.get_case(case_id)["spent"] == 0
    asyncio.run(run())


def test_mixed_private_dns_denied_before_job(case):
    async def run():
        controller, case_id = case
        async def answer(host, port):
            return ["172.30.52.3", "127.0.0.1"]
        dispatcher = Dispatcher(controller, FixtureBackend(), target_resolver=resolver(answer))
        with pytest.raises(Denied):
            await dispatcher.execute(case_id, SPEC)
        assert controller.get_case(case_id)["jobs"] == []
    asyncio.run(run())
