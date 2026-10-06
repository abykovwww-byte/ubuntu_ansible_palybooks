"""Inspect the protocol response without losing its automatic-review metadata.

This distinguishes transport outcomes, not authenticated human UI events. The
installed-client and protected-host integration gates still apply to acceptance.
"""

from __future__ import annotations

from mcp import types
from mcp.shared.message import ServerMessageMetadata
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Confirmation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    confirm: bool = Field(
        description="Я подтверждаю показанные цели, ограничения и основание разрешения на тестирование"
    )


def classify_response(reply: types.ElicitResult) -> tuple[bool, str, dict]:
    # A host's approval reviewer is not the engagement's human decision maker.
    # Preserve only categorical diagnostics, never arbitrary response metadata.
    reviewer = (reply.meta or {}).get("approvals_reviewer")
    diagnostics = {"protocol_action": reply.action,
                   "reviewer": "absent" if reviewer is None else
                               reviewer if reviewer in ("auto_review", "user") else "other"}
    if reviewer is not None and reviewer != "user":
        return False, "automatic_response_blocked", diagnostics
    if reply.action != "accept":
        return False, "host_" + reply.action + "_without_user_receipt", diagnostics
    try:
        content = Confirmation.model_validate(reply.content)
    except ValidationError:
        return False, "invalid_confirmation_content", diagnostics
    if not content.confirm:
        return False, "confirmation_not_given", diagnostics
    return True, "accepted_protocol_response", diagnostics


async def elicit_confirmation(ctx, message: str) -> types.ElicitResult:
    # FastMCP Context.elicit validates the data but discards result._meta.
    # Use the session protocol exchange and validate the raw result ourselves.
    return await ctx.session.send_request(
        types.ServerRequest(types.ElicitRequest(params=types.ElicitRequestFormParams(
            message=message,
            requestedSchema=Confirmation.model_json_schema(),
            _meta={"codex_requires_user_input": True},
        ))),
        types.ElicitResult,
        metadata=ServerMessageMetadata(related_request_id=ctx.request_id),
    )
