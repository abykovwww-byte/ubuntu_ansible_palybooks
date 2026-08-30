from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.runner import RPRunner
from app.rp.turn_engine import (
    RPAdministratorJob,
    RPModelOutputRejected,
    RPServiceJob,
    RPTurnEngine,
)


OWNER_ID = "owner-one"
PARTY_ID = "party-one"


def _party_source() -> dict[str, WorldSnapshot | ScenarioSnapshot]:
    world = WorldSnapshot(
        schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
        world_id="day-watch-moscow-v2",
        title="Дневной Дозор",
        language="ru",
        premise="Москва после Великого договора.",
        canon=("Канон мира.",),
        setting_rules="Законы мира.",
        characters="npc-one: Базовый NPC.",
        relationship_ontology={"axes": ["trust"]},
        seed_lore_cards=({"cards": [{"id": "world-card"}]},),
    )
    scenario = ScenarioSnapshot(
        schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
        scenario_id="test-scenario",
        title="Тестовый сценарий",
        world_id=world.world_id,
        source="preset",
        player_role="Новый сотрудник.",
        style="book",
        format="plain_scene_text",
        difficulty=None,
        detail_level="default",
        narrator_system="Веди сцену.",
        narrator_note="Сохраняй агентность игрока.",
        opening="Начинается смена.",
        initial_state={
            "player": {},
            "characters": {"npc-one": {}},
            "factions": {},
            "locations": {},
            "relationships": {},
        },
        active_character_ids=("npc-one",),
        starting_relationships={},
    )
    return {"world_snapshot": world, "scenario_snapshot": scenario}


def _engine_with_queued_jobs(database: Path) -> RPTurnEngine:
    engine = RPTurnEngine(database)
    engine.create_party(
        owner_user_id=OWNER_ID,
        party_id=PARTY_ID,
        **_party_source(),
    )
    engine.commit_turn(
        owner_user_id=OWNER_ID,
        party_id=PARTY_ID,
        request_id="request-one",
        idempotency_key="key-one",
        expected_version=0,
        player_text="Я жду.",
        narrator_text="Время идёт.",
    )
    return engine


QueuedJob = RPServiceJob | RPAdministratorJob


class _TaskState:
    def __init__(self, done: bool):
        self._done = done

    def done(self) -> bool:
        return self._done


def _concurrent_claims(
    claim: Callable[[], QueuedJob | None],
) -> tuple[QueuedJob | None, ...]:
    barrier = threading.Barrier(2)

    def run(_: int) -> QueuedJob | None:
        barrier.wait()
        return claim()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return tuple(pool.map(run, range(2)))


def test_runner_is_healthy_only_while_both_role_loops_are_alive(
    tmp_path: Path,
) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    runner = RPRunner(engine, object(), object())  # type: ignore[arg-type]
    runner._service_task = _TaskState(False)  # type: ignore[assignment]
    runner._administrator_task = _TaskState(False)  # type: ignore[assignment]
    assert runner.running is True

    runner._administrator_task = _TaskState(True)  # type: ignore[assignment]
    assert runner.running is False


def test_claims_are_atomic_role_specific_and_do_not_spend_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rp-clean.db"
    engine = _engine_with_queued_jobs(database)

    first_two = (engine.claim_service_job(), engine.claim_service_job())
    assert all(job is not None for job in first_two)
    service_results = _concurrent_claims(engine.claim_service_job)
    administrator_results = _concurrent_claims(engine.claim_administrator_job)

    assert sum(job is not None for job in service_results) == 1
    assert sum(job is not None for job in administrator_results) == 1
    assert {
        job.job_type
        for job in (*first_two, *service_results)
        if isinstance(job, RPServiceJob)
    } == {"story_memory", "relationships", "runtime_lore"}
    assert all(
        job.status == "running" and job.attempts == 0
        for job in engine.list_service_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
    )
    administrator_jobs = engine.list_administrator_jobs(
        owner_user_id=OWNER_ID, party_id=PARTY_ID
    )
    assert len(administrator_jobs) == 1
    assert administrator_jobs[0].status == "running"
    assert administrator_jobs[0].attempts == 0


def test_restart_recovery_is_free_and_only_actual_failures_spend_attempts(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rp-clean.db"
    engine = _engine_with_queued_jobs(database)
    service = engine.claim_service_job()
    administrator = engine.claim_administrator_job()
    assert service is not None
    assert administrator is not None

    restarted = RPTurnEngine(database)
    recovered = restarted.recover_interrupted_work()

    assert recovered["service_jobs"] == 1
    assert recovered["administrator_jobs"] == 1
    service = restarted.claim_service_job()
    administrator = restarted.claim_administrator_job()
    assert service is not None and service.attempts == 0
    assert administrator is not None and administrator.attempts == 0
    assert service.claim_token is not None
    assert administrator.claim_token is not None

    failed_service = restarted.fail_service_job(
        job_id=service.id,
        claim_token=service.claim_token,
        error="atomic service provider failed",
    )
    failed_administrator = restarted.fail_administrator_job(
        job_id=administrator.id,
        claim_token=administrator.claim_token,
        error="Administrator provider failed",
    )

    assert (failed_service.status, failed_service.attempts) == ("pending", 1)
    assert (failed_administrator.status, failed_administrator.attempts) == (
        "pending",
        1,
    )
    persisted = RPTurnEngine(database)
    assert next(
        job
        for job in persisted.list_service_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
        if job.id == failed_service.id
    ).attempts == 1
    assert next(
        job
        for job in persisted.list_administrator_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
        if job.id == failed_administrator.id
    ).attempts == 1


class _BlockingServiceHandler:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled_and_finished = False
        self.job_id: int | None = None

    async def handle(self, job: RPServiceJob) -> object:
        self.job_id = job.id
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            self.cancelled_and_finished = True
            raise


class _BlockingAdministratorHandler:
    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.cancelled_and_finished = False
        self.job_id: int | None = None

    async def handle(self, job: RPAdministratorJob) -> object:
        self.job_id = job.id
        self.entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(0)
            self.cancelled_and_finished = True
            raise


class _CompletingServiceHandler:
    def __init__(self) -> None:
        self.handled = asyncio.Event()

    async def handle(self, job: RPServiceJob) -> object:
        self.handled.set()
        return {}


class _CompletingAdministratorHandler:
    def __init__(self) -> None:
        self.handled = asyncio.Event()

    async def handle(self, job: RPAdministratorJob) -> object:
        self.handled.set()
        return {}


def test_disabled_role_gates_leave_jobs_pending_without_attempts(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")
    service_claims = 0
    administrator_claims = 0
    claim_service_job = engine.claim_service_job
    claim_administrator_job = engine.claim_administrator_job

    def counted_service_claim() -> RPServiceJob | None:
        nonlocal service_claims
        service_claims += 1
        return claim_service_job()

    def counted_administrator_claim() -> RPAdministratorJob | None:
        nonlocal administrator_claims
        administrator_claims += 1
        return claim_administrator_job()

    engine.claim_service_job = counted_service_claim  # type: ignore[method-assign]
    engine.claim_administrator_job = (  # type: ignore[method-assign]
        counted_administrator_claim
    )

    async def exercise() -> None:
        runner = RPRunner(
            engine,
            _CompletingServiceHandler(),
            _CompletingAdministratorHandler(),
            service_enabled=False,
            administrator_enabled=False,
            poll_interval=0.001,
        )
        await runner.start()
        await asyncio.sleep(0.01)
        await runner.stop()

    asyncio.run(exercise())

    assert service_claims == administrator_claims == 0
    assert all(
        (job.status, job.attempts, job.claim_token) == ("pending", 0, None)
        for job in engine.list_service_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
    )
    administrator = engine.list_administrator_jobs(
        owner_user_id=OWNER_ID, party_id=PARTY_ID
    )[0]
    assert (administrator.status, administrator.attempts, administrator.claim_token) == (
        "pending",
        0,
        None,
    )


class _RejectingServiceHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, job: RPServiceJob) -> object:
        self.calls += 1
        raise RPModelOutputRejected("invalid service model result")


class _RejectingAdministratorHandler:
    def __init__(self) -> None:
        self.calls = 0

    async def handle(self, job: RPAdministratorJob) -> object:
        self.calls += 1
        raise RPModelOutputRejected("invalid Administrator model result")


def test_rejected_model_output_is_terminal_without_unchanged_auto_retry(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")
    for _ in range(2):
        skipped = engine.claim_service_job()
        assert skipped is not None
        assert skipped.claim_token is not None
        engine.complete_service_job(
            job_id=skipped.id,
            claim_token=skipped.claim_token,
            result={"kind": skipped.job_type, "result": "not_exercised"},
        )

    async def exercise() -> tuple[int, int]:
        service_handler = _RejectingServiceHandler()
        administrator_handler = _RejectingAdministratorHandler()
        runner = RPRunner(
            engine,
            service_handler,
            administrator_handler,
            poll_interval=0.001,
        )
        await runner.start()
        for _ in range(100):
            service = engine.list_service_jobs(
                owner_user_id=OWNER_ID, party_id=PARTY_ID
            )[-1]
            administrator = engine.list_administrator_jobs(
                owner_user_id=OWNER_ID, party_id=PARTY_ID
            )[0]
            if service.status == administrator.status == "failed":
                break
            await asyncio.sleep(0.001)
        await runner.stop()
        return service_handler.calls, administrator_handler.calls

    service_calls, administrator_calls = asyncio.run(exercise())

    service = engine.list_service_jobs(
        owner_user_id=OWNER_ID, party_id=PARTY_ID
    )[-1]
    administrator = engine.list_administrator_jobs(
        owner_user_id=OWNER_ID, party_id=PARTY_ID
    )[0]
    assert (service.status, service.attempts, service_calls) == ("failed", 1, 1)
    assert (
        administrator.status,
        administrator.attempts,
        administrator_calls,
    ) == ("failed", 1, 1)


def test_runner_retries_transient_claim_errors_without_stopping_workers(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")
    claim_service_job = engine.claim_service_job
    claim_administrator_job = engine.claim_administrator_job
    service_claims = 0
    administrator_claims = 0

    def transient_service_claim() -> RPServiceJob | None:
        nonlocal service_claims
        service_claims += 1
        if service_claims == 1:
            raise RuntimeError("temporary service claim failure")
        return claim_service_job()

    def transient_administrator_claim() -> RPAdministratorJob | None:
        nonlocal administrator_claims
        administrator_claims += 1
        if administrator_claims == 1:
            raise RuntimeError("temporary Administrator claim failure")
        return claim_administrator_job()

    engine.claim_service_job = transient_service_claim  # type: ignore[method-assign]
    engine.claim_administrator_job = (  # type: ignore[method-assign]
        transient_administrator_claim
    )

    async def exercise() -> None:
        service_handler = _CompletingServiceHandler()
        administrator_handler = _CompletingAdministratorHandler()
        runner = RPRunner(
            engine,
            service_handler,
            administrator_handler,
            poll_interval=0.001,
        )
        await runner.start()
        await asyncio.wait_for(
            asyncio.gather(
                service_handler.handled.wait(),
                administrator_handler.handled.wait(),
            ),
            timeout=1,
        )
        assert runner.running is True
        await runner.stop()

    asyncio.run(exercise())

    assert service_claims >= 2
    assert administrator_claims >= 2


def test_retryable_handler_failure_waits_one_poll_before_reclaim(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")
    for _ in range(2):
        skipped = engine.claim_service_job()
        assert skipped is not None
        assert skipped.claim_token is not None
        engine.complete_service_job(
            job_id=skipped.id,
            claim_token=skipped.claim_token,
            result={"kind": skipped.job_type, "result": "not_exercised"},
        )

    async def exercise() -> tuple[float, float]:
        handled = asyncio.Event()
        call_times: list[float] = []

        class FailOnce:
            async def handle(self, job: RPServiceJob) -> object:
                call_times.append(asyncio.get_running_loop().time())
                if len(call_times) == 1:
                    raise RuntimeError("retryable provider failure")
                handled.set()
                return {}

        poll_interval = 0.03
        runner = RPRunner(
            engine,
            FailOnce(),
            _CompletingAdministratorHandler(),
            administrator_enabled=False,
            poll_interval=poll_interval,
        )
        await runner.start()
        await asyncio.wait_for(handled.wait(), timeout=1)
        await runner.stop()
        assert len(call_times) == 2
        return call_times[0], call_times[1]

    first_call, second_call = asyncio.run(exercise())

    assert second_call - first_call >= 0.02
    retried = engine.list_service_jobs(
        owner_user_id=OWNER_ID, party_id=PARTY_ID
    )[-1]
    assert (retried.status, retried.attempts) == ("succeeded", 1)


def test_finished_tasks_are_not_running_but_require_stop_cleanup(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")

    async def exercise() -> None:
        runner = RPRunner(
            engine,
            _CompletingServiceHandler(),
            _CompletingAdministratorHandler(),
        )
        runner._service_task = asyncio.create_task(asyncio.sleep(0))
        runner._administrator_task = asyncio.create_task(asyncio.sleep(0))
        await asyncio.gather(runner._service_task, runner._administrator_task)

        assert runner.running is False
        with pytest.raises(RuntimeError, match="already running"):
            await runner.start()

        await runner.stop()
        assert await runner.start() == {
            "narration_requests": 0,
            "service_jobs": 0,
            "administrator_jobs": 0,
        }
        await runner.stop()

    asyncio.run(exercise())


def test_start_waits_for_stop_to_finish_task_cleanup(tmp_path: Path) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")

    async def exercise() -> None:
        runner = RPRunner(
            engine,
            _CompletingServiceHandler(),
            _CompletingAdministratorHandler(),
        )
        cleanup_started = asyncio.Event()
        allow_cleanup = asyncio.Event()

        async def task_with_slow_cleanup() -> None:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await allow_cleanup.wait()
                raise

        runner._service_task = asyncio.create_task(task_with_slow_cleanup())
        runner._administrator_task = asyncio.create_task(task_with_slow_cleanup())
        await asyncio.sleep(0)
        stop_task = asyncio.create_task(runner.stop())
        await asyncio.wait_for(cleanup_started.wait(), timeout=1)

        with pytest.raises(RuntimeError, match="already running"):
            await runner.start()

        allow_cleanup.set()
        await asyncio.wait_for(stop_task, timeout=1)
        assert runner.running is False

    asyncio.run(exercise())


def test_runner_stop_cancels_awaits_and_requeues_claimed_work(
    tmp_path: Path,
) -> None:
    engine = _engine_with_queued_jobs(tmp_path / "rp-clean.db")

    async def exercise() -> tuple[int, int, bool, bool]:
        service_handler = _BlockingServiceHandler()
        administrator_handler = _BlockingAdministratorHandler()
        runner = RPRunner(
            engine,
            service_handler,
            administrator_handler,
            poll_interval=0.001,
        )
        assert await runner.start() == {
            "narration_requests": 0,
            "service_jobs": 0,
            "administrator_jobs": 0,
        }
        await asyncio.wait_for(
            asyncio.gather(
                service_handler.entered.wait(),
                administrator_handler.entered.wait(),
            ),
            timeout=1,
        )
        assert service_handler.job_id is not None
        assert administrator_handler.job_id is not None

        await runner.stop()

        assert runner.running is False
        return (
            service_handler.job_id,
            administrator_handler.job_id,
            service_handler.cancelled_and_finished,
            administrator_handler.cancelled_and_finished,
        )

    service_id, administrator_id, service_awaited, administrator_awaited = (
        asyncio.run(exercise())
    )

    assert service_awaited is True
    assert administrator_awaited is True
    service = next(
        job
        for job in engine.list_service_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
        if job.id == service_id
    )
    administrator = next(
        job
        for job in engine.list_administrator_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )
        if job.id == administrator_id
    )
    assert (service.status, service.attempts, service.claim_token) == (
        "pending",
        0,
        None,
    )
    assert (
        administrator.status,
        administrator.attempts,
        administrator.claim_token,
    ) == ("pending", 0, None)
