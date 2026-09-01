"""Typed derived mechanics for the isolated RP engine."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.rp.memory import (
    RP_MEMORY_PROMPT_MAX_CHARS,
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
    """Handle only the three fixed atomic service mechanics."""

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
                ):
                    raise ValueError(
                        "runtime Lore draft cannot fit the protected Lore prompt budget"
                    )
            except ValueError as exc:
                raise RPModelOutputRejected(
                    f"runtime Lore model output rejected: {exc}"
                ) from exc
            return self.engine.persist_runtime_lore_result(job=job, card=card)
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
) -> str:
    """Keep newest runtime Lore that fits beside immutable World Lore."""
    selected: list[dict[str, object]] = []
    for card in reversed(runtime_cards):
        candidate = _lore_card_prompt_payload(card)
        text = _serialize_lore_prompt(world_cards, (candidate, *selected))
        if len(text) <= limit:
            selected.insert(0, candidate)
    return _serialize_lore_prompt(world_cards, tuple(selected))


def runtime_lore_card_fits(
    world_cards: tuple[dict[str, object], ...], card: object, limit: int
) -> bool:
    required = _serialize_lore_prompt(
        world_cards, (_lore_card_prompt_payload(card),)
    )
    return len(required) <= limit


def _lore_card_prompt_payload(card: object) -> dict[str, object]:
    def value(name: str) -> object:
        if isinstance(card, dict):
            return card[name]
        return getattr(card, name)

    return {
        "kind": str(value("kind")),
        "origin": str(value("origin")) if not isinstance(card, dict) else "runtime",
        "title": str(value("title")),
        "content": str(value("content")),
        "keywords": tuple(value("keywords")),
    }


def _serialize_lore_prompt(
    world_cards: tuple[dict[str, object], ...],
    runtime_cards: tuple[dict[str, object], ...],
) -> str:
    return "LORE\n" + json.dumps(
        {"world": world_cards, "runtime": runtime_cards},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


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
