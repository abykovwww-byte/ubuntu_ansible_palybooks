"""Lifecycle owner for the two persisted RP background roles."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from app.rp.turn_engine import (
    RPAdministratorJob,
    RPBackgroundJobConflict,
    RPModelOutputRejected,
    RPServiceJob,
    RPTurnEngine,
)


logger = logging.getLogger(__name__)


class RPServiceJobHandler(Protocol):
    async def handle(self, job: RPServiceJob) -> dict[str, Any] | object: ...


class RPAdministratorJobHandler(Protocol):
    async def handle(self, job: RPAdministratorJob) -> dict[str, Any] | object: ...


class RPRunner:
    """Recover, start, cancel, and await both role-specific worker loops."""

    def __init__(
        self,
        engine: RPTurnEngine,
        service_handler: RPServiceJobHandler,
        administrator_handler: RPAdministratorJobHandler,
        *,
        poll_interval: float = 0.05,
    ):
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self.engine = engine
        self.service_handler = service_handler
        self.administrator_handler = administrator_handler
        self.poll_interval = poll_interval
        self._service_task: asyncio.Task[None] | None = None
        self._administrator_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return any(
            task is not None and not task.done()
            for task in (self._service_task, self._administrator_task)
        )

    async def start(self) -> dict[str, int]:
        if self._service_task is not None or self._administrator_task is not None:
            raise RuntimeError("RP runner is already running")
        recovered = self.engine.recover_interrupted_work()
        self._service_task = asyncio.create_task(
            self._run_service_jobs(), name="rp-atomic-service-runner"
        )
        self._administrator_task = asyncio.create_task(
            self._run_administrator_jobs(), name="rp-administrator-runner"
        )
        return recovered

    async def stop(self) -> None:
        tasks = tuple(
            task
            for task in (self._service_task, self._administrator_task)
            if task is not None
        )
        for task in tasks:
            task.cancel()
        try:
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            self._service_task = None
            self._administrator_task = None

    async def _run_service_jobs(self) -> None:
        while True:
            try:
                job = self.engine.claim_service_job()
            except Exception:
                logger.exception("Failed to claim RP atomic service job")
                await asyncio.sleep(self.poll_interval)
                continue
            if job is None:
                await asyncio.sleep(self.poll_interval)
                continue
            assert job.claim_token is not None
            try:
                result = await self.service_handler.handle(job)
                self.engine.complete_service_job(
                    job_id=job.id,
                    claim_token=job.claim_token,
                    result=result if isinstance(result, dict) else None,
                )
            except asyncio.CancelledError:
                self._release_service(job)
                raise
            except RPBackgroundJobConflict:
                continue
            except RPModelOutputRejected as exc:
                try:
                    self.engine.fail_service_job(
                        job_id=job.id,
                        claim_token=job.claim_token,
                        error=str(exc),
                        retryable=False,
                    )
                except RPBackgroundJobConflict:
                    pass
            except Exception as exc:
                try:
                    self.engine.fail_service_job(
                        job_id=job.id,
                        claim_token=job.claim_token,
                        error=str(exc) or type(exc).__name__,
                    )
                except RPBackgroundJobConflict:
                    pass

    async def _run_administrator_jobs(self) -> None:
        while True:
            try:
                job = self.engine.claim_administrator_job()
            except Exception:
                logger.exception("Failed to claim RP Administrator job")
                await asyncio.sleep(self.poll_interval)
                continue
            if job is None:
                await asyncio.sleep(self.poll_interval)
                continue
            assert job.claim_token is not None
            try:
                result = await self.administrator_handler.handle(job)
                self.engine.complete_administrator_job(
                    job_id=job.id,
                    claim_token=job.claim_token,
                    result=result if isinstance(result, dict) else None,
                )
            except asyncio.CancelledError:
                self._release_administrator(job)
                raise
            except RPBackgroundJobConflict:
                continue
            except RPModelOutputRejected as exc:
                try:
                    self.engine.fail_administrator_job(
                        job_id=job.id,
                        claim_token=job.claim_token,
                        error=str(exc),
                        retryable=False,
                    )
                except RPBackgroundJobConflict:
                    pass
            except Exception as exc:
                try:
                    self.engine.fail_administrator_job(
                        job_id=job.id,
                        claim_token=job.claim_token,
                        error=str(exc) or type(exc).__name__,
                    )
                except RPBackgroundJobConflict:
                    pass

    def _release_service(self, job: RPServiceJob) -> None:
        assert job.claim_token is not None
        try:
            self.engine.release_service_job(
                job_id=job.id, claim_token=job.claim_token
            )
        except RPBackgroundJobConflict:
            pass

    def _release_administrator(self, job: RPAdministratorJob) -> None:
        assert job.claim_token is not None
        try:
            self.engine.release_administrator_job(
                job_id=job.id, claim_token=job.claim_token
            )
        except RPBackgroundJobConflict:
            pass
