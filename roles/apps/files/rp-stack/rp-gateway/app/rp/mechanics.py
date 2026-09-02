"""Typed derived mechanics for the isolated RP engine."""

from __future__ import annotations

import hashlib
import json
import math
import re
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rp.memory import (
    RP_MEMORY_PROMPT_MAX_CHARS,
    RPMemoryFact,
    RPStoryMemoryRecord,
    RPStoryMemorySnapshot,
    memory_prompt_text,
)
from app.rp.turn_engine import (
    RPAdministratorJob,
    RPAdministratorProposal,
    RPModelOutputRejected,
    RPParty,
    RPRuntimeLoreCard,
    RPServiceJob,
    RPTurn,
    RPTurnEngine,
)


RP_LORE_PROMPT_MAX_CHARS = 16_000


class _StrictResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RPEvidenceSpan(_StrictResult):
    id: int = Field(gt=0)
    turn_version: int = Field(gt=0)
    role: Literal["user", "assistant"]
    text: str = Field(min_length=1, max_length=4_000)


class RPRelationshipCandidate(_StrictResult):
    character_id: str = Field(min_length=1, max_length=128)
    direction: Literal["character_to_player"]
    event_id: str = Field(min_length=1, max_length=128)
    evidence_span_ids: tuple[int, ...] = Field(min_length=1, max_length=8)


class RPRelationshipResult(_StrictResult):
    candidates: tuple[RPRelationshipCandidate, ...] = Field(max_length=16)


class RPRuntimeLoreResult(_StrictResult):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "result": {"const": "draft"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                        "keywords": {"type": "array"},
                        "evidence_span_ids": {"type": "array"},
                    }
                },
                {
                    "properties": {
                        "result": {"const": "no_candidate"},
                        "title": {"type": "null"},
                        "content": {"type": "null"},
                        "keywords": {"type": "null"},
                        "evidence_span_ids": {"type": "null"},
                    }
                },
            ]
        }
    )

    result: Literal["draft", "no_candidate"]
    kind: Literal["character", "event", "location"]
    title: str | None
    content: str | None
    keywords: tuple[str, ...] | None
    evidence_span_ids: tuple[int, ...] | None

    @model_validator(mode="after")
    def fields_match_result(self) -> RPRuntimeLoreResult:
        if self.result == "no_candidate":
            if any(
                value is not None
                for value in (
                    self.title,
                    self.content,
                    self.keywords,
                    self.evidence_span_ids,
                )
            ):
                raise ValueError("no_candidate Lore result cannot contain draft fields")
            return self
        if not self.title or not self.title.strip():
            raise ValueError("Lore draft title must contain text")
        if len(self.title) > 200:
            raise ValueError("Lore draft title exceeds 200 characters")
        if not self.content or not self.content.strip():
            raise ValueError("Lore draft content must contain text")
        if len(self.content) > 4_000:
            raise ValueError("Lore draft content exceeds 4000 characters")
        if not self.keywords or any(not item.strip() for item in self.keywords):
            raise ValueError("Lore draft needs non-empty keywords")
        if len(self.keywords) > 12 or any(len(item) > 100 for item in self.keywords):
            raise ValueError("Lore draft keyword budget exceeded")
        if len(set(self.keywords)) != len(self.keywords):
            raise ValueError("Lore draft keywords must be unique")
        if not self.evidence_span_ids:
            raise ValueError("Lore draft needs evidence spans")
        return self


class RPPlayerCorrectionResult(_StrictResult):
    model_config = ConfigDict(
        json_schema_extra={
            "oneOf": [
                {
                    "properties": {
                        "result": {"const": "draft"},
                        "target_slot": {"type": "string"},
                        "action": {"enum": ["replace", "retract"]},
                    }
                },
                {
                    "properties": {
                        "result": {"const": "no_target"},
                        "target_slot": {"type": "null"},
                        "action": {"type": "null"},
                        "after": {"type": "null"},
                        "forbidden_claims": {"maxItems": 0},
                    }
                },
            ]
        }
    )

    result: Literal["draft", "no_target"]
    target_slot: str | None
    action: Literal["replace", "retract"] | None
    after: str | None
    forbidden_claims: tuple[str, ...] = Field(max_length=20)

    @model_validator(mode="after")
    def fields_match_result(self) -> RPPlayerCorrectionResult:
        if self.result == "no_target":
            if (
                self.target_slot is not None
                or self.action is not None
                or self.after is not None
                or self.forbidden_claims
            ):
                raise ValueError("no_target correction cannot contain draft fields")
            return self
        if not self.target_slot or not self.target_slot.strip():
            raise ValueError("PlayerCorrection draft needs target_slot")
        if self.action is None:
            raise ValueError("PlayerCorrection draft needs action")
        if self.action == "replace" and (not self.after or not self.after.strip()):
            raise ValueError("replacement PlayerCorrection needs after text")
        if self.action == "retract" and self.after is not None:
            raise ValueError("retraction PlayerCorrection cannot contain after text")
        if self.after is not None and len(self.after) > 600:
            raise ValueError("PlayerCorrection replacement exceeds 600 characters")
        if any(not item.strip() or len(item) > 160 for item in self.forbidden_claims):
            raise ValueError("PlayerCorrection forbidden claims are invalid")
        if len(set(self.forbidden_claims)) != len(self.forbidden_claims):
            raise ValueError("PlayerCorrection forbidden claims must be unique")
        return self


class RPAdministratorResult(_StrictResult):
    result: Literal["suggest", "no_proposal"]
    target_slot: Literal["narrator_guidance"] | None
    after: str | None

    @model_validator(mode="after")
    def fields_match_result(self) -> RPAdministratorResult:
        if self.result == "no_proposal":
            if self.target_slot is not None or self.after is not None:
                raise ValueError("no_proposal cannot contain a target or replacement")
            return self
        if self.target_slot != "narrator_guidance":
            raise ValueError("Administrator can suggest only narrator_guidance")
        if not self.after or not self.after.strip():
            raise ValueError("Administrator suggestion must contain guidance")
        if len(self.after) > 3_500:
            raise ValueError("Administrator guidance exceeds 3500 characters")
        return self


class RPAtomicServiceModel(Protocol):
    """The atomic service role; it never receives Administrator jobs."""

    async def extract_relationships(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        evidence_spans: tuple[RPEvidenceSpan, ...],
    ) -> RPRelationshipResult: ...

    async def extract_runtime_lore(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        evidence_spans: tuple[RPEvidenceSpan, ...],
        existing_runtime_lore: tuple[RPRuntimeLoreCard, ...] = (),
    ) -> RPRuntimeLoreResult: ...

    async def update_story_memory(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        previous: RPStoryMemoryRecord | None,
    ) -> RPStoryMemorySnapshot: ...

    async def draft_player_lore(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        kind: Literal["character", "event", "location"],
        evidence_spans: tuple[RPEvidenceSpan, ...],
        existing_runtime_lore: tuple[RPRuntimeLoreCard, ...] = (),
    ) -> RPRuntimeLoreResult: ...

    async def draft_player_correction(
        self,
        *,
        party: RPParty,
        instruction: str,
        raw_hint: str | None,
        candidates: tuple[dict[str, Any], ...],
    ) -> RPPlayerCorrectionResult: ...


class RPAdministratorModel(Protocol):
    """The Administrator role; it never receives atomic service jobs."""

    async def review_party(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        evidence_spans: tuple[RPEvidenceSpan, ...],
        window_hash: str,
        before_text: str,
    ) -> RPAdministratorResult: ...


class RPAtomicServiceHandler:
    """Handle fixed atomic mechanics and explicit owner-started drafts."""

    def __init__(
        self,
        engine: RPTurnEngine,
        model: RPAtomicServiceModel,
        *,
        memory_anchor_turns: int = 8,
        lore_prompt_chars: int = RP_LORE_PROMPT_MAX_CHARS,
    ):
        if memory_anchor_turns <= 0:
            raise ValueError("memory_anchor_turns must be positive")
        if lore_prompt_chars <= 0:
            raise ValueError("lore_prompt_chars must be positive")
        self.engine = engine
        self.model = model
        self.memory_anchor_turns = memory_anchor_turns
        self.lore_prompt_chars = lore_prompt_chars

    async def handle(self, job: RPServiceJob) -> dict[str, object]:
        if job.claim_token is None or job.status != "running":
            raise ValueError("service handler requires a running claimed job")
        if job.result is not None:
            return job.result
        party = self.engine.get_party(
            owner_user_id=job.owner_user_id, party_id=job.party_id
        )
        turn = self.engine.source_turn_for_service_job(job)
        spans = numbered_evidence_spans((turn,))
        if job.job_type == "relationships":
            result = await self.model.extract_relationships(
                party=party, turn=turn, evidence_spans=spans
            )
            try:
                if not isinstance(result, RPRelationshipResult):
                    result = RPRelationshipResult.model_validate(result)
                accepted, rejected = _relationship_changes(
                    party=party,
                    source_version=job.source_version,
                    spans=spans,
                    result=result,
                )
            except ValueError as exc:
                raise RPModelOutputRejected(
                    f"relationship model output rejected: {exc}"
                ) from exc
            return self.engine.persist_relationship_result(
                job=job, causes=accepted, rejected=rejected
            )
        if job.job_type == "runtime_lore":
            existing_runtime_lore = self.engine.derived_context(
                owner_user_id=job.owner_user_id, party_id=job.party_id
            ).runtime_lore_cards
            result = await self.model.extract_runtime_lore(
                party=party,
                turn=turn,
                evidence_spans=spans,
                existing_runtime_lore=existing_runtime_lore,
            )
            try:
                if not isinstance(result, RPRuntimeLoreResult):
                    result = RPRuntimeLoreResult.model_validate(result)
                card = _runtime_lore_card(job=job, spans=spans, result=result)
                if card is not None and not runtime_lore_card_fits(
                    party.world_snapshot.seed_lore_cards,
                    card,
                    self.lore_prompt_chars,
                    scenario_cards=party.scenario_snapshot.local_overrides.lore_cards,
                ):
                    raise ValueError(
                        "runtime Lore draft cannot fit the protected Lore prompt budget"
                    )
            except ValueError as exc:
                raise RPModelOutputRejected(
                    f"runtime Lore model output rejected: {exc}"
                ) from exc
            return self.engine.persist_runtime_lore_result(job=job, card=card)
        if job.job_type == "player_lore":
            kind = str(job.operation.get("kind") or "")
            if kind not in {"character", "event", "location"}:
                raise RPModelOutputRejected("player Lore operation kind is invalid")
            existing_runtime_lore = self.engine.derived_context(
                owner_user_id=job.owner_user_id, party_id=job.party_id
            ).runtime_lore_cards
            result = await self.model.draft_player_lore(
                party=party,
                turn=turn,
                kind=kind,  # type: ignore[arg-type]
                evidence_spans=spans,
                existing_runtime_lore=existing_runtime_lore,
            )
            try:
                if not isinstance(result, RPRuntimeLoreResult):
                    result = RPRuntimeLoreResult.model_validate(result)
                if result.kind != kind:
                    raise ValueError("player Lore kind changed after the model call")
                card = _runtime_lore_card(job=job, spans=spans, result=result)
                if card is not None and not runtime_lore_card_fits(
                    party.world_snapshot.seed_lore_cards,
                    card,
                    self.lore_prompt_chars,
                    scenario_cards=party.scenario_snapshot.local_overrides.lore_cards,
                ):
                    raise ValueError(
                        "player Lore draft cannot fit the protected Lore prompt budget"
                    )
            except ValueError as exc:
                raise RPModelOutputRejected(
                    f"player Lore model output rejected: {exc}"
                ) from exc
            return self.engine.record_service_job_result(
                job=job,
                result={
                    "kind": "player_lore",
                    "authoring_kind": kind,
                    "result": result.result,
                    "card": card,
                },
            )
        if job.job_type == "player_correction":
            catalog = job.operation.get("catalog")
            if not isinstance(catalog, list):
                raise RPModelOutputRejected("PlayerCorrection catalog is missing")
            candidates = rank_player_correction_targets(
                tuple(item for item in catalog if isinstance(item, dict)),
                instruction=str(job.operation.get("instruction") or ""),
                raw_hint=(
                    str(job.operation["raw_hint"])
                    if job.operation.get("raw_hint") is not None
                    else None
                ),
            )
            result = await self.model.draft_player_correction(
                party=party,
                instruction=str(job.operation.get("instruction") or ""),
                raw_hint=(
                    str(job.operation["raw_hint"])
                    if job.operation.get("raw_hint") is not None
                    else None
                ),
                candidates=candidates,
            )
            try:
                if not isinstance(result, RPPlayerCorrectionResult):
                    result = RPPlayerCorrectionResult.model_validate(result)
                draft = _player_correction_draft(candidates, result)
            except ValueError as exc:
                raise RPModelOutputRejected(
                    f"PlayerCorrection model output rejected: {exc}"
                ) from exc
            return self.engine.persist_player_correction_result(job=job, draft=draft)
        if job.job_type == "story_memory":
            return await self._update_story_memory(job, party)
        raise ValueError(f"unsupported atomic service job {job.job_type!r}")

    async def _update_story_memory(
        self, job: RPServiceJob, party: RPParty
    ) -> dict[str, object]:
        update_id = f"service-job:{job.id}"
        existing = self.engine.story_memory_by_update_id(
            owner_user_id=job.owner_user_id,
            party_id=job.party_id,
            update_id=update_id,
        )
        if existing is not None:
            return self.engine.record_service_job_result(
                job=job,
                result={
                    "kind": "story_memory",
                    "result": "updated",
                    "snapshot_id": existing.id,
                },
            )
        turns = tuple(
            turn
            for turn in self.engine.list_turns(
                owner_user_id=job.owner_user_id, party_id=job.party_id
            )
            if turn.committed_version <= job.source_version
        )
        previous = self.engine.latest_story_memory(
            owner_user_id=job.owner_user_id, party_id=job.party_id
        )
        safe_coverage = previous.snapshot.safe_coverage if previous is not None else 0
        uncovered = tuple(
            turn for turn in turns if turn.committed_version > safe_coverage
        )
        if len(uncovered) < self.memory_anchor_turns:
            return self.engine.record_service_job_result(
                job=job,
                result={
                    "kind": "story_memory",
                    "result": "not_due",
                    "uncovered_units": len(uncovered),
                },
            )
        snapshot = await self.model.update_story_memory(
            party=party, turns=uncovered, previous=previous
        )
        try:
            if not isinstance(snapshot, RPStoryMemorySnapshot):
                snapshot = RPStoryMemorySnapshot.model_validate(snapshot)
            if snapshot.observed_through_version > job.source_version:
                raise ValueError("story memory cannot observe beyond its source job")
            prompt_text = memory_prompt_text(snapshot)
            if len(prompt_text) > RP_MEMORY_PROMPT_MAX_CHARS:
                raise ValueError("story memory exceeds its protected prompt budget")
        except ValueError as exc:
            raise RPModelOutputRejected(
                f"story-memory model output rejected: {exc}"
            ) from exc
        saved = self.engine.append_story_memory(
            owner_user_id=job.owner_user_id,
            party_id=job.party_id,
            expected_base_snapshot_id=previous.id if previous is not None else None,
            update_id=update_id,
            snapshot=snapshot,
        )
        return self.engine.record_service_job_result(
            job=job,
            result={
                "kind": "story_memory",
                "result": "updated",
                "snapshot_id": saved.id,
            },
        )


class RPAdministratorHandler:
    """Handle only Administrator review jobs in suggest/manual mode."""

    def __init__(
        self,
        engine: RPTurnEngine,
        model: RPAdministratorModel,
        *,
        cadence_turns: int = 8,
    ):
        if cadence_turns <= 0:
            raise ValueError("cadence_turns must be positive")
        self.engine = engine
        self.model = model
        self.cadence_turns = cadence_turns

    async def handle(
        self, job: RPAdministratorJob
    ) -> dict[str, object] | RPAdministratorProposal:
        if job.claim_token is None or job.status != "running":
            raise ValueError("Administrator handler requires a running claimed job")
        if job.result is not None:
            return job.result
        all_turns = tuple(
            turn
            for turn in self.engine.list_turns(
                owner_user_id=job.owner_user_id, party_id=job.party_id
            )
            if turn.committed_version <= job.source_version
        )
        if len(all_turns) != 1 and len(all_turns) % self.cadence_turns != 0:
            return self.engine.record_administrator_no_proposal(job=job)
        turns = self.engine.source_turns_for_administrator_job(job)
        party = self.engine.get_party(
            owner_user_id=job.owner_user_id, party_id=job.party_id
        )
        guidance = self.engine.derived_context(
            owner_user_id=job.owner_user_id, party_id=job.party_id
        ).administrator_guidance
        before_text = guidance.content if guidance is not None else ""
        result = await self.model.review_party(
            party=party,
            turns=turns,
            evidence_spans=numbered_evidence_spans(turns),
            window_hash=job.window_hash,
            before_text=before_text,
        )
        try:
            if not isinstance(result, RPAdministratorResult):
                result = RPAdministratorResult.model_validate(result)
        except ValueError as exc:
            raise RPModelOutputRejected(
                f"Administrator model output rejected: {exc}"
            ) from exc
        if result.result == "no_proposal":
            return self.engine.record_administrator_no_proposal(job=job)
        assert result.after is not None
        if result.after.strip() == before_text.strip():
            return self.engine.record_administrator_no_proposal(
                job=job, reason="unchanged_guidance"
            )
        proposal = self.engine.create_administrator_proposal(
            job=job,
            after_text=result.after,
            expected_before_text=before_text,
        )
        if proposal is None:
            return {"kind": "administrator", "result": "stale_review"}
        return proposal


def numbered_evidence_spans(turns: tuple[RPTurn, ...]) -> tuple[RPEvidenceSpan, ...]:
    spans: list[RPEvidenceSpan] = []
    for turn in turns:
        messages = (
            (("assistant", turn.narrator_text),)
            if turn.turn_kind == "opening_scene"
            else (("user", turn.player_text), ("assistant", turn.narrator_text))
        )
        for role, text in messages:
            parts = tuple(part.strip() for part in text.splitlines() if part.strip())
            for part in parts or (text.strip(),):
                spans.append(
                    RPEvidenceSpan(
                        id=len(spans) + 1,
                        turn_version=turn.committed_version,
                        role=role,
                        text=part[:4_000],
                    )
                )
    return tuple(spans)


def _relationship_changes(
    *,
    party: RPParty,
    source_version: int,
    spans: tuple[RPEvidenceSpan, ...],
    result: RPRelationshipResult,
) -> tuple[tuple[dict[str, object], ...], tuple[dict[str, object], ...]]:
    active_characters = set(party.scenario_snapshot.active_character_ids)
    ontology = party.world_snapshot.relationship_ontology
    events = ontology.get("events")
    axes = ontology.get("axes")
    character_weights = ontology.get("character_weights", {})
    if not isinstance(events, dict) or not isinstance(axes, dict):
        raise ValueError("World relationship ontology lacks events or axes")
    span_ids = {span.id for span in spans}
    accepted: list[dict[str, object]] = []
    rejected: list[dict[str, object]] = []
    running_delta: dict[tuple[str, str], int] = {}
    seen_keys: set[str] = set()
    for index, candidate in enumerate(result.candidates):
        try:
            if candidate.character_id not in active_characters:
                raise ValueError("unknown_character_id")
            event = events.get(candidate.event_id)
            if not isinstance(event, dict):
                raise ValueError("unknown_event_id")
            if not set(candidate.evidence_span_ids).issubset(span_ids):
                raise ValueError("unknown_evidence_span")
            if len(set(candidate.evidence_span_ids)) != len(candidate.evidence_span_ids):
                raise ValueError("duplicate_evidence_span")
            axis = event.get("axis")
            weight = event.get("weight")
            if not isinstance(axis, str) or not axis.strip():
                raise ValueError("invalid_event_axis")
            if not isinstance(weight, (int, float)) or isinstance(weight, bool):
                raise ValueError("invalid_event_weight")
            axis_contract = axes.get(axis)
            if not isinstance(axis_contract, dict):
                raise ValueError("unknown_axis")
            cap = axis_contract.get("per_turn_cap")
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                raise ValueError("invalid_per_turn_cap")
            multiplier = 1.0
            character = character_weights.get(candidate.character_id)
            if isinstance(character, dict):
                multipliers = character.get("multipliers")
                if isinstance(multipliers, dict):
                    raw_multiplier = multipliers.get(candidate.event_id, 1.0)
                    if isinstance(raw_multiplier, (int, float)) and not isinstance(
                        raw_multiplier, bool
                    ):
                        multiplier = float(raw_multiplier)
            raw_delta = _round_half_away(float(weight) * multiplier)
            key = (candidate.character_id, axis)
            previous = running_delta.get(key, 0)
            bounded_turn = max(-cap, min(cap, previous + raw_delta))
            delta = bounded_turn - previous
            minimum = axis_contract.get("min", -100)
            maximum = axis_contract.get("max", 100)
            if (
                not isinstance(minimum, (int, float))
                or isinstance(minimum, bool)
                or not isinstance(maximum, (int, float))
                or isinstance(maximum, bool)
                or minimum >= maximum
            ):
                raise ValueError("invalid_axis_range")
            if delta == 0:
                raise ValueError("relationship_turn_cap_reached")
            candidate_payload = {
                "source_version": source_version,
                "character_id": candidate.character_id,
                "direction": candidate.direction,
                "event_id": candidate.event_id,
                "evidence_span_ids": list(candidate.evidence_span_ids),
            }
            candidate_key = hashlib.sha256(
                _canonical_json(candidate_payload).encode("utf-8")
            ).hexdigest()
            if candidate_key in seen_keys:
                raise ValueError("duplicate_candidate")
            seen_keys.add(candidate_key)
            running_delta[key] = previous + delta
            accepted.append(
                {
                    "candidate_index": index,
                    "candidate_key": candidate_key,
                    "character_id": candidate.character_id,
                    "event_id": candidate.event_id,
                    "axis": axis,
                    "delta": delta,
                    "seed_value": _initial_axis_value(
                        party, candidate.character_id, axis
                    ),
                    "axis_min": _round_half_away(float(minimum)),
                    "axis_max": _round_half_away(float(maximum)),
                    "evidence_span_ids": candidate.evidence_span_ids,
                }
            )
        except ValueError as exc:
            rejected.append({"index": index, "reason": str(exc)})
    return tuple(accepted), tuple(rejected)


def relationship_values(
    party: RPParty, causes: tuple[object, ...]
) -> dict[tuple[str, str], int]:
    """Calculate bounded current axes from Scenario seeds plus immutable causes."""
    ontology = party.world_snapshot.relationship_ontology
    axes = ontology.get("axes")
    if not isinstance(axes, dict):
        return {}
    values: dict[tuple[str, str], int] = {}
    for character_id in party.scenario_snapshot.active_character_ids:
        for axis, contract in axes.items():
            if not isinstance(axis, str) or not isinstance(contract, dict):
                continue
            values[(character_id, axis)] = _initial_axis_value(
                party, character_id, axis
            )
    for cause in causes:
        character_id = getattr(cause, "character_id", None)
        axis = getattr(cause, "axis", None)
        delta = getattr(cause, "delta", None)
        if (
            not isinstance(character_id, str)
            or not isinstance(axis, str)
            or not isinstance(delta, int)
        ):
            continue
        key = (character_id, axis)
        values[key] = values.get(key, 0) + delta
    for key, value in tuple(values.items()):
        contract = axes.get(key[1])
        if not isinstance(contract, dict):
            continue
        minimum = contract.get("min", -100)
        maximum = contract.get("max", 100)
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)):
            values[key] = _round_half_away(
                max(float(minimum), min(float(maximum), value))
            )
    return values


def _initial_axis_value(party: RPParty, character_id: str, axis: str) -> int:
    if axis != "loyalty":
        return 0
    characters = party.scenario_snapshot.initial_state.get("characters")
    character = characters.get(character_id) if isinstance(characters, dict) else None
    trust = character.get("trust", 0) if isinstance(character, dict) else 0
    if not isinstance(trust, (int, float)) or isinstance(trust, bool):
        return 0
    mapping = party.world_snapshot.relationship_ontology.get("trust_mapping")
    if not isinstance(mapping, dict) or mapping.get("kind") != "linear":
        return _round_half_away(float(trust))
    source = mapping.get("in")
    target = mapping.get("out")
    if (
        not isinstance(source, list)
        or len(source) != 2
        or not isinstance(target, list)
        or len(target) != 2
        or any(
            not isinstance(value, (int, float)) or isinstance(value, bool)
            for value in (*source, *target)
        )
        or source[0] == source[1]
    ):
        return _round_half_away(float(trust))
    ratio = (float(trust) - float(source[0])) / (float(source[1]) - float(source[0]))
    mapped = float(target[0]) + ratio * (float(target[1]) - float(target[0]))
    return _round_half_away(mapped)


def _runtime_lore_card(
    *,
    job: RPServiceJob,
    spans: tuple[RPEvidenceSpan, ...],
    result: RPRuntimeLoreResult,
) -> dict[str, object] | None:
    if result.result == "no_candidate":
        return None
    assert result.title is not None
    assert result.content is not None
    assert result.keywords is not None
    assert result.evidence_span_ids is not None
    if not set(result.evidence_span_ids).issubset({span.id for span in spans}):
        raise ValueError("runtime Lore references an unknown evidence span")
    payload = {
        "party_id": job.party_id,
        "source_version": job.source_version,
        "kind": result.kind,
        "title": result.title.strip(),
        "content": result.content.strip(),
        "keywords": list(result.keywords),
        "evidence_span_ids": list(result.evidence_span_ids),
    }
    return {
        "card_key": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
        **payload,
    }


def lore_prompt_text(
    world_cards: tuple[dict[str, object], ...],
    runtime_cards: tuple[object, ...],
    limit: int,
    *,
    scenario_cards: tuple[object, ...] = (),
) -> str:
    """Keep newest runtime Lore beside immutable World and Scenario Lore."""
    selected: list[dict[str, object]] = []
    for card in reversed(runtime_cards):
        candidate = _lore_card_prompt_payload(card)
        text = _serialize_lore_prompt(
            world_cards, scenario_cards, (candidate, *selected)
        )
        if len(text) <= limit:
            selected.insert(0, candidate)
    return _serialize_lore_prompt(world_cards, scenario_cards, tuple(selected))


def runtime_lore_card_fits(
    world_cards: tuple[dict[str, object], ...],
    card: object,
    limit: int,
    *,
    scenario_cards: tuple[object, ...] = (),
) -> bool:
    required = _serialize_lore_prompt(
        world_cards, scenario_cards, (_lore_card_prompt_payload(card),)
    )
    return len(required) <= limit


def _lore_card_prompt_payload(card: object) -> dict[str, object]:
    def value(name: str) -> object:
        if isinstance(card, dict):
            return card[name]
        return getattr(card, name)

    return {
        **(
            {"kind": str(value("kind"))}
            if (not isinstance(card, dict) or "kind" in card)
            else {}
        ),
        "origin": (
            str(value("origin"))
            if not isinstance(card, dict) or "origin" in card
            else "runtime"
        ),
        "title": str(value("title")),
        "content": str(value("content")),
        "keywords": tuple(value("keywords")),
    }


def _serialize_lore_prompt(
    world_cards: tuple[dict[str, object], ...],
    scenario_cards: tuple[object, ...],
    runtime_cards: tuple[dict[str, object], ...],
) -> str:
    scenario_payload = tuple(
        {
            **_lore_card_prompt_payload(
                card.model_dump(mode="json") if isinstance(card, BaseModel) else card
            ),
            "origin": "scenario",
        }
        for card in scenario_cards
    )
    return "LORE\n" + json.dumps(
        {
            "world": world_cards,
            "scenario": scenario_payload,
            "runtime": runtime_cards,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def player_correction_catalog(
    *,
    party: RPParty,
    turns: tuple[RPTurn, ...],
    memory: RPStoryMemoryRecord | None,
) -> tuple[dict[str, Any], ...]:
    """Build the exact immutable target catalog; the model only ranks a subset."""
    catalog: list[dict[str, Any]] = []
    if memory is not None:
        for section_key, section in memory.snapshot.sections().items():
            for field_name in type(section).model_fields:
                value = getattr(section, field_name)
                facts = (
                    (value,)
                    if isinstance(value, RPMemoryFact)
                    else tuple(
                        item
                        for item in value
                        if isinstance(item, RPMemoryFact)
                    )
                    if isinstance(value, tuple)
                    else ()
                )
                for fact in facts:
                    if fact.status != "active":
                        continue
                    catalog.append(
                        {
                            "target_slot": f"memory:{field_name}:{fact.fact_id}",
                            "target_kind": "memory",
                            "section": section_key,
                            "field": field_name,
                            "before": fact.text,
                            "source_versions": list(fact.source_turn_versions),
                        }
                    )
    for start, end, text in _bounded_claims(party.world_snapshot.setting_rules):
        claim_id = hashlib.sha256(
            _canonical_json(
                {"rule": "world", "start": start, "end": end, "text": text}
            ).encode("utf-8")
        ).hexdigest()
        catalog.append(
            {
                "target_slot": f"rule:{claim_id}",
                "target_kind": "rule",
                "before": text,
            }
        )
    for turn in turns:
        source_hash = hashlib.sha256(turn.narrator_text.encode("utf-8")).hexdigest()
        for start, end, text in _bounded_claims(turn.narrator_text):
            claim_id = hashlib.sha256(
                _canonical_json([turn.id, start, end, source_hash]).encode("utf-8")
            ).hexdigest()[:20]
            catalog.append(
                {
                    "target_slot": f"raw:{turn.id}:{claim_id}",
                    "target_kind": "raw",
                    "turn_id": turn.id,
                    "turn_version": turn.committed_version,
                    "start": start,
                    "end": end,
                    "before": text,
                }
            )
    return tuple(catalog)


def player_correction_catalog_hash(catalog: tuple[dict[str, Any], ...]) -> str:
    return hashlib.sha256(_canonical_json(list(catalog)).encode("utf-8")).hexdigest()


def rank_player_correction_targets(
    catalog: tuple[dict[str, Any], ...],
    *,
    instruction: str,
    raw_hint: str | None,
    limit: int = 8,
    max_chars: int = 4_000,
) -> tuple[dict[str, Any], ...]:
    """Bound provider input without treating lexical overlap as semantic truth."""
    if not instruction.strip():
        raise ValueError("PlayerCorrection instruction must contain text")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("PlayerCorrection candidate limit must be positive")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("PlayerCorrection payload limit must be positive")
    eligible = catalog
    if raw_hint:
        if raw_hint.count(":") == 2:
            exact = tuple(
                item
                for item in catalog
                if str(item.get("target_slot") or "") == raw_hint
            )
            return exact if len(_canonical_json(list(exact))) <= max_chars else ()
        eligible = tuple(
            item
            for item in catalog
            if str(item.get("target_slot") or "").startswith(raw_hint + ":")
        )
    terms = set(
        re.findall(r"[\w-]{3,}", instruction.casefold(), flags=re.UNICODE)
    )

    def overlap(candidate: dict[str, Any]) -> int:
        candidate_terms = set(
            re.findall(
                r"[\w-]{3,}",
                str(candidate.get("before") or "").casefold(),
                flags=re.UNICODE,
            )
        )
        return len(terms & candidate_terms)

    def recency(candidate: dict[str, Any]) -> int:
        if candidate.get("target_kind") == "raw":
            return int(candidate.get("turn_id") or 0)
        if candidate.get("target_kind") == "memory":
            versions = candidate.get("source_versions")
            if isinstance(versions, list):
                return max((int(item) for item in versions), default=0)
        return 0

    ranked = sorted(
        eligible,
        key=lambda candidate: (
            -overlap(candidate),
            -recency(candidate),
            str(candidate.get("target_slot") or ""),
        ),
    )
    reserved: list[dict[str, Any]] = []
    for target_kind in ("memory", "rule"):
        candidate = next(
            (
                item
                for item in ranked
                if item.get("target_kind") == target_kind and overlap(item) > 0
            ),
            None,
        )
        if candidate is not None:
            reserved.append(candidate)
    selected: list[dict[str, Any]] = []
    raw_per_turn: dict[int, int] = {}
    for candidate in (*reserved, *ranked):
        if len(selected) >= limit:
            break
        slot = str(candidate.get("target_slot") or "")
        if any(str(item.get("target_slot") or "") == slot for item in selected):
            continue
        turn_id = int(candidate.get("turn_id") or 0)
        if candidate.get("target_kind") == "raw" and raw_per_turn.get(turn_id, 0) >= 4:
            continue
        trial = (*selected, candidate)
        if len(_canonical_json(list(trial))) <= max_chars:
            selected.append(candidate)
            if candidate.get("target_kind") == "raw":
                raw_per_turn[turn_id] = raw_per_turn.get(turn_id, 0) + 1
    return tuple(selected)


def _player_correction_draft(
    candidates: tuple[dict[str, Any], ...],
    result: RPPlayerCorrectionResult,
) -> dict[str, Any] | None:
    if result.result == "no_target":
        return None
    target = next(
        (
            candidate
            for candidate in candidates
            if candidate.get("target_slot") == result.target_slot
        ),
        None,
    )
    if target is None:
        raise ValueError("PlayerCorrection selected a target outside ranked payload")
    assert result.action is not None
    if target.get("target_kind") != "rule" and result.forbidden_claims:
        raise ValueError("forbidden_claims are allowed only for World rule targets")
    return {
        "target_slot": str(target["target_slot"]),
        "target_kind": str(target["target_kind"]),
        "action": result.action,
        "before": str(target["before"]),
        "after": result.after.strip() if result.after is not None else None,
        "forbidden_claims": list(result.forbidden_claims),
    }


def _bounded_claims(text: str, limit: int = 600) -> tuple[tuple[int, int, str], ...]:
    claims: list[tuple[int, int, str]] = []
    start = 0
    while start < len(text):
        while start < len(text) and text[start].isspace():
            start += 1
        if start >= len(text):
            break
        hard_end = min(start + limit, len(text))
        end = hard_end
        if hard_end < len(text):
            boundaries = [
                text.rfind(marker, start, hard_end)
                for marker in ("\n", ". ", "! ", "? ", " ")
            ]
            boundary = max(boundaries)
            if boundary > start:
                end = boundary + (1 if text[boundary] in ".!?" else 0)
        claim = text[start:end].strip()
        if claim:
            actual_start = text.find(claim, start, end)
            claims.append((actual_start, actual_start + len(claim), claim))
        start = max(end, start + 1)
    return tuple(claims)


def _round_half_away(value: float) -> int:
    return int(math.copysign(math.floor(abs(value) + 0.5), value))


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
