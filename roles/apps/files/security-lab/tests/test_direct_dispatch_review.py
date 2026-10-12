"""Admission/cleanup regressions only; these do not qualify network isolation."""
import asyncio
import json

import pytest

from security_lab.contracts import Denied
from security_lab.controller import Controller
from security_lab.dispatcher import Dispatcher, FixtureBackend
from security_lab.pentest_tools import BoundPentestTools
from security_lab.target_resolution import TargetResolver

NOW = 1_800_000_000
SPEC = {"tool": "curl", "url": "https://fixture.example.test:8443/public/info", "method": "GET", "seconds": 20}


def approved_case(controller):
    case_id = controller.create_case("fixture.example.test")["id"]
    scope = {"targets": [{"host": "fixture.example.test", "port": 8443, "paths": ["/public"], "methods": ["GET"]}],
             "authorization": {"reference": "lab:isolated-docker-fixture", "owner": "fixture", "basis": "Owned offline review",
                               "hosts": ["fixture.example.test"], "valid_from": NOW - 1, "valid_until": NOW + 300},
             "expires_at": NOW + 300, "budget": {"seconds": 180, "rps": 2, "concurrency": 1, "jobs": 10}}
    manifest = controller.propose(case_id, "scope", scope)
    controller.activate_from_task(case_id, manifest["id"], "scope")
    return case_id, manifest


def test_two_cases_cannot_pass_pending_dns_admission_together(tmp_path):
    controller = Controller(tmp_path, "fixture", clock=lambda: NOW)
    first_case, _ = approved_case(controller)
    second_case, _ = approved_case(controller)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        lookups = 0
        async def answer(host, port):
            nonlocal lookups
            lookups += 1
            entered.set()
            await release.wait()
            return ["172.30.52.3"]
        resolver = TargetResolver(answer, clock=lambda: NOW, lab_private_addresses=("172.30.52.3",))
        dispatcher = Dispatcher(controller, FixtureBackend(delay=1), target_resolver=resolver)
        first = asyncio.create_task(dispatcher.execute(first_case, SPEC))
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(dispatcher.execute(second_case, SPEC))
        await asyncio.sleep(.02)
        # Both requests are pending before the first lookup is released. The
        # second must be denied by admission, before DNS, job or budget charge.
        before_release = lookups
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        try:
            assert before_release == 1
            assert isinstance(results[0], dict) and isinstance(results[1], Denied)
            assert controller.get_case(second_case)["jobs"] == []
            assert controller.get_case(second_case)["spent"] == 0
            assert len(controller.get_case(first_case)["jobs"]) == 1
            assert controller.get_case(first_case)["spent"] == 20
        finally:
            await dispatcher.shutdown()
    try:
        asyncio.run(scenario())
    finally:
        controller.close()


@pytest.mark.parametrize("cancel_request", [False, True])
def test_facade_post_guard_does_not_cancel_running_closure(tmp_path, monkeypatch, cancel_request):
    controller = Controller(tmp_path, "fixture", clock=lambda: NOW)
    case_id, manifest = approved_case(controller)
    async def scenario():
        close_entered, close_release, submitted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        class HeldClose(FixtureBackend):
            async def close(self, lease):
                close_entered.set()
                await close_release.wait()
                await super().close(lease)
        backend = HeldClose()
        dispatcher = Dispatcher(controller, backend)
        facade = BoundPentestTools(controller, dispatcher, case_id=case_id, scope_id=manifest["id"],
                                  scope_hash=manifest["hash"], generation=controller.get_case(case_id)["generation"])
        original_execute = dispatcher.execute
        issued = {}
        async def change_after_submit(*args, **kwargs):
            job = await original_execute(*args, **kwargs)
            issued.update(job_id=job["id"], task=dispatcher.active[job["id"]][1])
            await asyncio.wait_for(close_entered.wait(), timeout=1)
            controller.stop(case_id)
            submitted.set()
            return job
        monkeypatch.setattr(dispatcher, "execute", change_after_submit)
        request = asyncio.create_task(facade.execute_tool_job(SPEC))
        await asyncio.wait_for(submitted.wait(), timeout=2)
        await asyncio.sleep(.02)
        task = issued["task"]
        assert issued["job_id"] in dispatcher.closing
        assert not task.cancelling() and not task.done()
        if cancel_request:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            assert not task.cancelling() and not task.done()
        close_release.set()
        result = await asyncio.gather(request, return_exceptions=True)
        await asyncio.gather(task, return_exceptions=True)
        if not cancel_request:
            assert isinstance(result[0], Denied)
        status = controller.get_job_status(case_id, issued["job_id"])
        assert status["outcome"] == "interrupted" and status["evidence_id"]
        evidence = next(item for item in controller.export(case_id)["evidence"] if item["id"] == status["evidence_id"])
        assert json.loads(evidence["body"])["access_closed"] is True
        assert backend.closed and controller.get_case(case_id)["spent"] == 20
    try:
        asyncio.run(scenario())
    finally:
        controller.close()
