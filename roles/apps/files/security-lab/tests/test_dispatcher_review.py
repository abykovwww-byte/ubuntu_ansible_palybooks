"""Independent lifecycle regressions: no external requests or real worker launch."""

import asyncio
import hashlib
import json

import pytest

from security_lab.controller import Controller
from security_lab.contracts import Denied


NOW = 1_800_000_000


@pytest.fixture
def prepared_case(tmp_path):
    controller = Controller(tmp_path / "review", "fixture-operator", clock=lambda: NOW)
    case_id = controller.create_case("fixture.example.test")["id"]
    body = dict(
        targets=[dict(host="fixture.example.test", paths=["/public"], methods=["GET", "HEAD"])],
        authorization=dict(reference="lab:isolated-docker-fixture", owner="fixture-operator", basis="Synthetic test only",
                           hosts=["fixture.example.test"], actions=["checks"], valid_from=NOW - 1, valid_until=NOW + 86400),
        expires_at=NOW + 3600, budget=dict(seconds=120, rps=1, concurrency=1, jobs=5),
    )
    row = controller.propose(case_id, "scope", body)
    controller.activate_from_task(case_id, row["id"], "scope")
    try:
        yield controller, case_id
    finally:
        controller.close()


def test_old_generation_completion_cannot_interrupt_new_generation_job(prepared_case):
    controller, case_id = prepared_case
    old, _ = controller.prepare_job(case_id, "old-worker", "target_http", 15)
    controller.stop(case_id)
    controller.resume(case_id)
    current, token = controller.prepare_job(case_id, "new-worker", "target_http", 15)
    controller.finish_job(case_id, old["id"], outcome="interrupted",
                          result={"access_closed": True, "reason": "Delayed old worker closure"}, synthetic=True)
    assert controller.get_case(case_id)["state"] == "checking"
    assert controller.validate_capability(current["id"], token, "new-worker", "target_http")["id"] == current["id"]


def test_immediate_stop_records_unstarted_worker_closure(prepared_case):
    from security_lab.dispatcher import Dispatcher, FixtureBackend

    controller, case_id = prepared_case

    async def scenario():
        dispatcher = Dispatcher(controller, FixtureBackend(delay=60))
        job = await dispatcher.execute(case_id, {"tool": "curl", "url": "https://fixture.example.test/public"})
        # Intentionally no yield between preparation response and operator stop.
        await dispatcher.stop(case_id)
        state = controller.get_job_status(case_id, job["id"])
        assert state["finished"] is not None
        assert state["evidence_id"] is not None
        evidence = next(item for item in controller.export(case_id)["evidence"] if item["id"] == state["evidence_id"])
        assert json.loads(evidence["body"])["access_closed"] is True

    asyncio.run(scenario())


def test_concurrent_stop_does_not_cancel_already_running_closure(prepared_case):
    from security_lab.dispatcher import Dispatcher, FixtureBackend

    controller, case_id = prepared_case

    async def scenario():
        worker_started, closing, permit_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BlockingClosureBackend(FixtureBackend):
            async def run(self, command):
                worker_started.set()
                await asyncio.Event().wait()

            async def close(self, lease):
                closing.set()
                await permit_close.wait()
                self.closed = True

        backend = BlockingClosureBackend()
        dispatcher = Dispatcher(controller, backend)
        job = await dispatcher.execute(case_id, {"tool": "curl", "url": "https://fixture.example.test/public"})
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        first = asyncio.create_task(dispatcher.stop(case_id))
        await asyncio.wait_for(closing.wait(), timeout=1)
        second = asyncio.create_task(dispatcher.stop(case_id))
        await asyncio.sleep(0)
        permit_close.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)
        state = controller.get_job_status(case_id, job["id"])
        assert backend.closed is True
        assert state["finished"] is not None
        assert state["evidence_id"] is not None

    asyncio.run(scenario())


def test_cancelled_stop_request_does_not_cancel_worker_cleanup(prepared_case):
    from security_lab.dispatcher import Dispatcher, FixtureBackend

    controller, case_id = prepared_case

    async def scenario():
        worker_started, closing, permit_close = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class BlockingClosureBackend(FixtureBackend):
            async def run(self, command):
                worker_started.set()
                await asyncio.Event().wait()

            async def close(self, lease):
                closing.set()
                await permit_close.wait()
                self.closed = True

        backend = BlockingClosureBackend()
        dispatcher = Dispatcher(controller, backend)
        job = await dispatcher.execute(case_id, {"tool": "curl", "url": "https://fixture.example.test/public"})
        job_task = dispatcher.active[job["id"]][1]
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        request = asyncio.create_task(dispatcher.stop(case_id))
        await asyncio.wait_for(closing.wait(), timeout=1)
        # A native MCP request can be cancelled while its stop handler waits.
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        permit_close.set()
        await asyncio.wait_for(asyncio.gather(job_task, return_exceptions=True), timeout=2)
        assert backend.closed is True
        assert controller.get_job_status(case_id, job["id"])["finished"] is not None

    asyncio.run(scenario())


@pytest.mark.parametrize("header", [
    "Set-Cookie: session=SECRET_HEADER_VALUE; HttpOnly",
    "Authorization: Bearer SECRET_HEADER_VALUE",
    "Set-Cookie: session=\r\n    SECRET_HEADER_VALUE; HttpOnly",
    "Authorization: Bearer\r\n\tSECRET_HEADER_VALUE",
])
def test_plain_and_folded_credential_headers_are_redacted(header):
    from security_lab.dispatcher import redact_worker_output

    output = redact_worker_output("HTTP/1.1 200 OK\r\n" + header + "\r\nX-Public: preserved\r\n\r\nbody")
    assert "SECRET_HEADER_VALUE" not in output
    assert "[REDACTED_CREDENTIAL]" in output
    assert "X-Public: preserved" in output
    assert output.endswith("body")


def test_folded_credentials_are_removed_before_evidence_hash(prepared_case):
    from security_lab.dispatcher import Dispatcher, FixtureBackend, WorkerResult, redact_worker_output

    controller, case_id = prepared_case
    output = "HTTP/1.1 200 OK\r\nSet-Cookie: session=\r\n    SECRET_HTTP_SESSION; HttpOnly\r\nAuthorization: Bearer\r\n\tSECRET_HTTP_AUTH\r\nX-Public: preserved\r\n\r\nbody"

    class HeaderBackend(FixtureBackend):
        async def run(self, command):
            return WorkerResult(0, output)

    async def scenario():
        dispatcher = Dispatcher(controller, HeaderBackend())
        job = await dispatcher.execute(case_id, {"tool": "curl", "url": "https://fixture.example.test/public"})
        await dispatcher.active[job["id"]][1]
        evidence = next(item for item in controller.export(case_id)["evidence"]
                        if item["id"] == controller.get_job_status(case_id, job["id"])["evidence_id"])
        body = json.loads(evidence["body"])
        assert "SECRET_HTTP_" not in evidence["body"]
        assert body["stdout_hash"] == hashlib.sha256(redact_worker_output(output).encode()).hexdigest()
        assert body["stdout_hash"] != hashlib.sha256(output.encode()).hexdigest()

    asyncio.run(scenario())


def test_late_closure_preserves_generation_and_marks_success_uncertain(prepared_case):
    controller, case_id = prepared_case
    job, token = controller.prepare_job(case_id, "worker", "target_http", 60)
    lease = controller.bind_job_policy(job["id"], token, "worker", "target_http", "a" * 64, {"tool": "curl"})
    controller.acknowledge_job(job["id"], token, "worker", "target_http", {**lease, "policy_hash": "a" * 64})
    controller.start_job(job["id"], token, "worker", "target_http")
    generation = controller.get_case(case_id)["generation"]
    controller.clock = lambda: NOW + 31  # Closure completed after the 30-second lease.
    controller.finish_job(case_id, job["id"], outcome="completed",
                          result={"exit_code": 0, "access_closed": True}, synthetic=True)
    state = controller.get_case(case_id)
    assert state["generation"] == generation
    assert state["spent"] == 60
    assert state["jobs"][0]["outcome"] == "interrupted"
    assert state["state"] == "checks_review"
    evidence = controller.export(case_id)["evidence"][-1]
    assert evidence["status"] == "interrupted"
    assert json.loads(evidence["body"])["access_closed"] is True


def test_late_installer_ack_cannot_revive_lease_or_start_worker(prepared_case):
    controller, case_id = prepared_case
    job, token = controller.prepare_job(case_id, "worker", "target_http", 60)
    lease = controller.bind_job_policy(job["id"], token, "worker", "target_http", "a" * 64, {"tool": "curl"})
    generation = controller.get_case(case_id)["generation"]
    controller.clock = lambda: NOW + 31
    with pytest.raises(Denied):
        controller.acknowledge_job(job["id"], token, "worker", "target_http", {**lease, "policy_hash": "a" * 64})
    with pytest.raises(Denied):
        controller.start_job(job["id"], token, "worker", "target_http")
    assert controller.get_case(case_id)["generation"] == generation
    assert controller.get_job_status(case_id, job["id"])["acknowledged_sequence"] == 0
    assert controller.get_job_status(case_id, job["id"])["started"] is None


def test_uninstalled_preparation_failure_does_not_leave_revocation_unacknowledged(prepared_case):
    from security_lab.dispatcher import Dispatcher, FixtureBackend

    controller, case_id = prepared_case

    class InvalidPolicyBackend(FixtureBackend):
        def policy(self, job, scope):
            raise ValueError("Synthetic policy creation failed before installation")

        async def install(self, policy, lease):
            pytest.fail("Installer must never be called for an invalid policy")

    async def scenario():
        dispatcher = Dispatcher(controller, InvalidPolicyBackend())
        with pytest.raises(ValueError):
            await dispatcher.execute(case_id, {"tool": "curl", "url": "https://fixture.example.test/public"})
        assert controller.get_case(case_id)["jobs"][0]["started"] is None
        await dispatcher.stop(case_id)
        assert controller.db.execute("SELECT COUNT(*) FROM outbox WHERE delivered=0").fetchone()[0] == 0

    asyncio.run(scenario())
