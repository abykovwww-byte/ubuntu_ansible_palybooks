"""Codex-facing MCP; approval is a server-initiated protocol exchange only."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from .contracts import Denied
from .controller import Controller
from .reports import html_report


class Confirmation(BaseModel):
    confirm: bool = Field(description="Я подтверждаю показанные цели, ограничения и основание разрешения на тестирование")


def build_server(directory: Path, principal: str) -> FastMCP:
    @asynccontextmanager
    async def lifespan(server):
        controller = Controller(directory, principal)
        try:
            yield controller
        finally:
            controller.close()

    server = FastMCP(
        "security-lab",
        instructions=("Manage one evidence-backed engagement in the current Codex chat. "
                      "Live execution is blocked during integration acceptance. Never present synthetic fixture data "
                      "as OSINT. Scope/actions are approved only by server-initiated elicitation; "
                      "there is no model-callable approve endpoint. Read integration_status before claiming readiness."),
        lifespan=lifespan,
    )
    read = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

    def controller(ctx: Context) -> Controller:
        return ctx.request_context.lifespan_context

    @server.tool(annotations=read)
    def integration_status(ctx: Context) -> dict[str, Any]:
        """Report actual integration gaps and whether live jobs are enabled."""
        return controller(ctx).integration_status()

    @server.tool(annotations=write)
    def create_case(seed: str, ctx: Context) -> dict[str, Any]:
        """Create an engagement from a domain. Does not contact or scan that domain."""
        return controller(ctx).create_case(seed)

    @server.tool(annotations=read)
    def get_case(case_id: str, ctx: Context) -> dict[str, Any]:
        """Read stage, surface, manifest versions and job status without capability secrets."""
        return controller(ctx).get_case(case_id)

    @server.tool(annotations=write)
    def collect_lab_fixture(case_id: str, ctx: Context) -> dict[str, Any]:
        """Populate SYNTHETIC offline discovery for integration tests. Makes zero network requests."""
        return controller(ctx).collect_fixture(case_id)

    @server.tool(annotations=write)
    def propose_scope(case_id: str, manifest: dict[str, Any], ctx: Context) -> dict[str, Any]:
        """Save a strict pending checks-only scope including authorization. Never approves it."""
        return controller(ctx).propose(case_id, "scope", manifest)

    async def request_confirmation(case_id: str, manifest_id: str, kind: str, ctx: Context) -> dict[str, Any]:
        c = controller(ctx)
        session = str(id(ctx.session))
        pending = c.challenge(case_id, manifest_id, session)
        if pending["kind"] != kind:
            raise Denied("Incorrect confirmation type")
        message = (
            f"Подтвердите {'scope проверок' if kind == 'scope' else 'конкретные дальнейшие действия'} "
            f"для дела {case_id}, версия {pending['version']}.\n"
            "Это решение пользователя; ответ модели не является разрешением. "
            "В прототипе даже после подтверждения live jobs остаются отключены.\n"
            + json.dumps(pending["manifest"], ensure_ascii=False, indent=2)
            + f"\nManifest hash: {pending['hash']}"
        )
        try:
            reply = await asyncio.wait_for(ctx.elicit(message=message, schema=Confirmation), timeout=300)
        except Exception as exc:
            # A missing host capability or protocol failure must never turn into consent.
            return {"status": "confirmation_unavailable", "manifest_id": manifest_id,
                    "reason": type(exc).__name__, "approved": False}
        accepted = reply.action == "accept" and reply.data is not None and reply.data.confirm is True
        return c.resolve_confirmation(pending["challenge"], session, accepted, transport="mcp_elicitation")

    @server.tool(annotations=write)
    async def request_scope_confirmation(case_id: str, manifest_id: str, ctx: Context) -> dict[str, Any]:
        """Ask the HUMAN in this chat to confirm a saved scope via MCP elicitation."""
        return await request_confirmation(case_id, manifest_id, "scope", ctx)

    @server.tool(annotations=write)
    def propose_actions(case_id: str, manifest: dict[str, Any], ctx: Context) -> dict[str, Any]:
        """Propose specific actions after checks; requires matching scope and ROE."""
        return controller(ctx).propose(case_id, "actions", manifest)

    @server.tool(annotations=write)
    async def request_action_confirmation(case_id: str, manifest_id: str, ctx: Context) -> dict[str, Any]:
        """Ask the HUMAN to authorize the specified actions via a separate elicitation."""
        return await request_confirmation(case_id, manifest_id, "actions", ctx)

    @server.tool(annotations=write)
    def stop_case(case_id: str, ctx: Context) -> dict[str, Any]:
        """Stop jobs, revoke their generation and pause this case."""
        return controller(ctx).stop(case_id)

    @server.tool(annotations=write)
    def resume_case(case_id: str, ctx: Context) -> dict[str, Any]:
        """Resume a paused case with a still-valid scope; never automatically resumes PoC."""
        return controller(ctx).resume(case_id)

    @server.tool(annotations=read)
    def get_report(case_id: str, ctx: Context) -> dict[str, Any]:
        """Return evidence-backed case data; synthetic and integration gaps remain labelled."""
        return controller(ctx).export(case_id)

    @server.resource("security-lab://case/{case_id}/report.html")
    def portable_report(case_id: str, ctx: Context) -> str:
        return html_report(controller(ctx).export(case_id))

    return server
