"""Passive Subfinder discovery using the existing Controller and tools runtime.

The surface receipts are candidates, not testing authorization. This module
creates no provider, credential configuration, scope approval or second store.
"""
from __future__ import annotations

import asyncio
import hashlib

from .contracts import Denied, canonical
from .discovery_contracts import SourceRequest, SourceResult
from .subfinder_source import parse_subfinder


class SubfinderDiscovery:
    def __init__(self, controller, runtime):
        self.controller, self.runtime = controller, runtime
        self.tasks: dict[str, asyncio.Task] = {}
        self._runs: dict[str, tuple[str, str]] = {}
        self._closure: dict[str, bool] = {}
        self._started: set[str] = set()

    async def start(self, case_id: str) -> dict:
        if case_id in self.tasks and not self.tasks[case_id].done():
            raise Denied("Subfinder discovery already active for this case")
        run = self.controller.begin_discovery(case_id)
        process_id = "subfinder-" + run["id"]
        self._runs[case_id] = (run["id"], process_id)
        self._closure[case_id] = False
        task = asyncio.create_task(self._collect(case_id, run["id"], process_id))
        self.tasks[case_id] = task

        def completed(value):
            if self.tasks.get(case_id) is value:
                self.tasks.pop(case_id, None)
            if not value.cancelled():
                value.exception()  # No provider/command diagnostic is logged.
        task.add_done_callback(completed)
        return {**run, "source":"subfinder", "passive":True, "scope_approval_granted":False,
                "availability":"pending_actual_cli_receipt", "synthetic":False}

    def _failure(self, seed: str, reason: str, *, closed: bool) -> SourceResult:
        body = canonical({"metadata":{"tool":"subfinder", "tool_version":"unknown",
            "execution_status":reason, "access_closed":closed, "provider_completion":"unknown",
            "external_data_trusted":False}, "records":[]})
        return SourceResult(request=SourceRequest(source="subfinder", category="subdomains",
            url="tool://subfinder/domain/"+seed, parser="subfinder_jsonl", subject=seed),
            collected_at=float(self.controller.clock()), status="error", body=body,
            body_hash=hashlib.sha256(body.encode("utf-8")).hexdigest(), reason=reason)

    async def _close(self, case_id: str, process_id: str) -> bool:
        try:
            result = await self.runtime.stop(process_id)
            self._closure[case_id] = result.get("access_closed") is True
        except Exception:
            self._closure[case_id] = False
        return self._closure[case_id]

    def _record_finish(self, case_id: str, run_id: str, result: SourceResult,
                       status: str, reason: str) -> dict:
        # Stop/replacement forbids late receipts. finish_discovery is idempotent
        # for a Controller-terminal run and preserves its current case state.
        if self.controller.run_active(case_id, run_id):
            self.controller.record_source_result(case_id, run_id, result)
        return self.controller.finish_discovery(case_id, run_id, status=status, reason=reason)

    async def _collect(self, case_id: str, run_id: str, process_id: str):
        self._started.add(process_id)
        seed = self.controller.get_case(case_id)["seed"]
        try:
            result = await self.runtime.run_subfinder(run_id=process_id, seed=seed, seconds=90)
            self._closure[case_id] = result.get("access_closed") is True
            if not self._closure[case_id]:
                await self._close(case_id, process_id)
                raise Denied("Subfinder process closure was not confirmed")
            receipts = result.get("receipts")
            if not isinstance(receipts, list) or len(receipts) != 1:
                raise Denied("Expected one actual Subfinder command receipt")
            receipt = receipts[0]
            parsed = parse_subfinder(seed, receipt["stdout"], receipt["exit_code"], result["actual_version"],
                started_at=receipt["started_at"], finished_at=receipt["finished_at"],
                output_truncated=result.get("receipts_truncated", False))
            parsed = parsed.model_copy(update={"synthetic":result.get("synthetic") is True})
            status = "completed" if parsed.status in {"success", "empty"} else "partial" if parsed.status == "partial" else "failed"
            return self._record_finish(case_id, run_id, parsed, status, parsed.reason)
        except asyncio.CancelledError:
            cleanup = asyncio.create_task(self._close(case_id, process_id))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # Cleanup remains owned and runs to completion even when the
                # outer request receives another cancellation.
                await asyncio.shield(cleanup)
            closed = self._closure.get(case_id, False)
            reason = "subfinder_interrupted" if closed else "subfinder_interrupted_closure_unconfirmed"
            return self._record_finish(case_id, run_id, self._failure(seed, reason, closed=closed), "interrupted", reason)
        except Exception:
            closed = self._closure.get(case_id, False)
            if not closed:
                closed = await self._close(case_id, process_id)
            reason = "subfinder_runtime_or_receipt_error" if closed else "subfinder_runtime_error_closure_unconfirmed"
            return self._record_finish(case_id, run_id, self._failure(seed, reason, closed=closed), "failed", reason)

    async def stop_case(self, case_id: str) -> dict:
        pair = self._runs.get(case_id)
        if pair is None:
            return {"case_id":case_id, "access_closed":True, "status":"not_started"}
        task = self.tasks.get(case_id)
        if task is not None and not task.done():
            if not task.cancelling():
                task.cancel()
            await asyncio.shield(asyncio.gather(task, return_exceptions=True))
        if pair[1] not in self._started:
            self._closure[case_id] = True
            seed = self.controller.get_case(case_id)["seed"]
            self._record_finish(case_id, pair[0], self._failure(seed, "subfinder_interrupted_before_launch", closed=True),
                                "interrupted", "subfinder_interrupted_before_launch")
        if not self._closure.get(case_id, False):
            await self._close(case_id, pair[1])
        return {"case_id":case_id, "run_id":pair[0], "access_closed":self._closure.get(case_id, False),
                "status":"interrupted"}

    async def shutdown(self) -> dict:
        results = [await self.stop_case(case_id) for case_id in tuple(self._runs)]
        return {"access_closed":all(result["access_closed"] for result in results)}
