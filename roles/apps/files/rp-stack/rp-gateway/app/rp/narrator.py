"""History-first narrator path for the isolated RP engine."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, fields
from typing import Literal, Protocol

from app.rp.memory import (
    RP_MEMORY_PROMPT_MAX_CHARS,
    RPStoryMemoryRecord,
    memory_prompt_text,
)
from app.rp.mechanics import (
    RP_LORE_PROMPT_MAX_CHARS,
    lore_prompt_text,
    relationship_values,
)
from app.rp.turn_engine import (
    RPBackgroundJobConflict,
    RPDerivedContext,
    RPIdempotencyConflict,
    RPParty,
    RPPartyVersionConflict,
    RPTurn,
    RPTurnEngine,
)


_GATEWAY_NARRATOR_RULES = (
    "Продолжай одну ролевую сцену на языке World. "
    "Не выбирай за игрока мысли, слова, чувства или решение. "
    "Верни только художественный текст сцены без JSON и служебных пояснений."
)


class RPPromptBudgetExceeded(RuntimeError):
    """The required prompt cannot fit without dropping protected context."""

    def __init__(self, layer: str, actual_chars: int, limit_chars: int):
        super().__init__(
            f"RP prompt layer {layer!r} uses {actual_chars} chars; limit is {limit_chars}"
        )
        self.layer = layer
        self.actual_chars = actual_chars
        self.limit_chars = limit_chars


class RPNarratorUnavailable(RuntimeError):
    """The one allowed narrator call failed and the player can retry unchanged input."""

    def __init__(self, player_text: str | None):
        super().__init__("Narrator is temporarily unavailable; retry the same action")
        self.player_text = player_text
        self.retryable = True


@dataclass(frozen=True, slots=True)
class RPPromptLimits:
    raw_window_turns: int = 50
    raw_anchor_turns: int = 8
    gateway_rules_chars: int = 1_000
    world_chars: int = 40_000
    scenario_chars: int = 8_000
    player_chars: int = 4_000
    world_rules_chars: int = 8_000
    memory_chars: int = RP_MEMORY_PROMPT_MAX_CHARS
    lore_chars: int = RP_LORE_PROMPT_MAX_CHARS
    relationship_chars: int = 12_000
    administrator_chars: int = 4_000
    narrator_note_chars: int = 1_500
    opening_chars: int = 4_000
    hard_input_chars: int = 400_000

    def __post_init__(self) -> None:
        for field in fields(self):
            field_name = field.name
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ValueError(f"{field_name} must be a positive integer")


@dataclass(frozen=True, slots=True)
class RPPromptMessage:
    role: Literal["system", "user", "assistant"]
    block_id: str
    content: str

    def provider_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class RPNarratorPrompt:
    messages: tuple[RPPromptMessage, ...]
    raw_turn_versions: tuple[int, ...]
    safe_memory_coverage: int
    stable_prefix_hash: str
    input_chars: int


class RPNarrator(Protocol):
    """One provider route; retry/fallback policy does not live in the RP engine."""

    async def complete(self, prompt: RPNarratorPrompt) -> str: ...


class RPNarratorPromptBuilder:
    def __init__(self, limits: RPPromptLimits | None = None):
        self.limits = limits or RPPromptLimits()

    def build_turn(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        memory: RPStoryMemoryRecord | None,
        player_text: str,
        derived: RPDerivedContext | None = None,
    ) -> RPNarratorPrompt:
        if not isinstance(player_text, str) or not player_text.strip():
            raise ValueError("player_text must be a non-empty string")
        return self._build(
            party=party,
            turns=turns,
            memory=memory,
            derived=derived,
            current=RPPromptMessage("user", "current_player_action", player_text),
        )

    def build_opening(self, *, party: RPParty) -> RPNarratorPrompt:
        scenario = party.scenario_snapshot
        opening = f"OPENING_REQUEST\n{scenario.opening}"
        self._bounded("opening", opening, self.limits.opening_chars)
        return self._build(
            party=party,
            turns=(),
            memory=None,
            derived=None,
            current=RPPromptMessage("user", "opening_request", opening),
        )

    def _build(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        memory: RPStoryMemoryRecord | None,
        derived: RPDerivedContext | None,
        current: RPPromptMessage,
    ) -> RPNarratorPrompt:
        world = party.world_snapshot
        scenario = party.scenario_snapshot
        gateway_rules = self._bounded(
            "gateway_rules", _GATEWAY_NARRATOR_RULES, self.limits.gateway_rules_chars
        )
        world_content = self._bounded(
            "world",
            _world_content(world),
            self.limits.world_chars,
        )
        scenario_experience = self._bounded(
            "scenario",
            _scenario_experience(scenario),
            self.limits.scenario_chars,
        )
        player = self._bounded(
            "player",
            f"PLAYER_ROLE\n{scenario.player_role}",
            self.limits.player_chars,
        )
        world_rules = self._bounded(
            "world_rules",
            f"WORLD_RULES\n{world.setting_rules}",
            self.limits.world_rules_chars,
        )
        static_messages = (
            RPPromptMessage("system", "gateway_narrator_rules", gateway_rules),
            RPPromptMessage("system", "world", world_content),
            RPPromptMessage("system", "scenario_experience", scenario_experience),
            RPPromptMessage("system", "player_role", player),
            RPPromptMessage("system", "world_rules", world_rules),
        )

        safe_coverage = memory.snapshot.safe_coverage if memory is not None else 0
        raw_turns = select_raw_turns(
            turns,
            safe_coverage=safe_coverage,
            window_turns=self.limits.raw_window_turns,
            anchor_turns=self.limits.raw_anchor_turns,
        )
        raw_messages = tuple(
            message for turn in raw_turns for message in _raw_messages(turn)
        )
        volatile_messages: list[RPPromptMessage] = []
        if memory is not None:
            memory_text = self._bounded(
                "memory",
                memory_prompt_text(memory.snapshot),
                self.limits.memory_chars,
            )
            volatile_messages.append(
                RPPromptMessage("system", "story_memory", memory_text)
            )
        relationship_text = self._bounded(
            "relationships",
            _relationship_prompt_text(party, derived),
            self.limits.relationship_chars,
        )
        volatile_messages.append(
            RPPromptMessage(
                "system", "party_relationships", relationship_text
            )
        )
        runtime_lore = (
            derived.runtime_lore_cards if derived is not None else ()
        )
        if world.seed_lore_cards or runtime_lore:
            lore_text = lore_prompt_text(
                world.seed_lore_cards,
                runtime_lore,
                self.limits.lore_chars,
            )
            if len(lore_text) > self.limits.lore_chars:
                raise RPPromptBudgetExceeded(
                    "lore", len(lore_text), self.limits.lore_chars
                )
            volatile_messages.append(
                RPPromptMessage("system", "lore", lore_text)
            )
        if derived is not None and derived.administrator_guidance is not None:
            administrator_text = self._bounded(
                "administrator",
                "ACCEPTED_PARTY_ADMINISTRATOR_GUIDANCE\n"
                + derived.administrator_guidance.content,
                self.limits.administrator_chars,
            )
            volatile_messages.append(
                RPPromptMessage(
                    "system", "administrator_guidance", administrator_text
                )
            )
        narrator_note = self._bounded(
            "narrator_note",
            f"SCENARIO_NARRATOR_NOTE\n{scenario.narrator_note}",
            self.limits.narrator_note_chars,
        )
        volatile_messages.append(
            RPPromptMessage("system", "scenario_narrator_note", narrator_note)
        )

        messages = (*static_messages, *raw_messages, *volatile_messages, current)
        input_chars = sum(len(message.content) for message in messages)
        if input_chars > self.limits.hard_input_chars:
            raise RPPromptBudgetExceeded(
                "hard_input", input_chars, self.limits.hard_input_chars
            )

        stable_raw = raw_turns[: self.limits.raw_window_turns]
        stable_messages = (
            *static_messages,
            *tuple(message for turn in stable_raw for message in _raw_messages(turn)),
        )
        stable_prefix_hash = hashlib.sha256(
            json.dumps(
                [
                    {
                        "role": message.role,
                        "content": message.content,
                    }
                    for message in stable_messages
                ],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return RPNarratorPrompt(
            messages=messages,
            raw_turn_versions=tuple(turn.committed_version for turn in raw_turns),
            safe_memory_coverage=safe_coverage,
            stable_prefix_hash=stable_prefix_hash,
            input_chars=input_chars,
        )

    @staticmethod
    def _bounded(layer: str, content: str, limit: int) -> str:
        if len(content) > limit:
            raise RPPromptBudgetExceeded(layer, len(content), limit)
        return content


class RPNarratorService:
    """Call Narrator once, then atomically append its plain-text result."""

    def __init__(
        self,
        engine: RPTurnEngine,
        narrator: RPNarrator,
        prompt_builder: RPNarratorPromptBuilder | None = None,
        *,
        atomic_service_enabled: bool = True,
        derived_wait_seconds: float = 0.0,
        derived_poll_interval: float = 0.05,
    ):
        if not isinstance(atomic_service_enabled, bool):
            raise ValueError("atomic_service_enabled must be a boolean")
        if (
            not isinstance(derived_wait_seconds, (int, float))
            or isinstance(derived_wait_seconds, bool)
            or derived_wait_seconds < 0
        ):
            raise ValueError("derived_wait_seconds must be non-negative")
        if (
            not isinstance(derived_poll_interval, (int, float))
            or isinstance(derived_poll_interval, bool)
            or derived_poll_interval <= 0
        ):
            raise ValueError("derived_poll_interval must be positive")
        self.engine = engine
        self.narrator = narrator
        self.prompt_builder = prompt_builder or RPNarratorPromptBuilder()
        self.atomic_service_enabled = atomic_service_enabled
        self.derived_wait_seconds = float(derived_wait_seconds)
        self.derived_poll_interval = float(derived_poll_interval)

    async def narrate_turn(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        player_text: str,
    ) -> RPTurn:
        _validate_request_fields(request_id, idempotency_key, expected_version)
        if not isinstance(player_text, str) or not player_text.strip():
            raise ValueError("player_text must be a non-empty string")
        turns = self.engine.list_turns(owner_user_id=owner_user_id, party_id=party_id)
        replay = _turn_replay(
            turns,
            turn_kind="narrative",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            player_text=player_text,
        )
        if replay is not None:
            return replay
        claim = self.engine.claim_narration(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind="narrative",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            player_text=player_text,
        )
        if claim.turn is not None:
            return claim.turn
        if not claim.acquired:
            return await self._wait_for_narration(
                owner_user_id=owner_user_id,
                party_id=party_id,
                turn_kind="narrative",
                request_id=request_id,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
                player_text=player_text,
            )
        if claim.request.claim_token is None:
            raise RuntimeError("acquired narration claim has no token")
        try:
            await self._wait_for_previous_atomic_service_jobs(
                owner_user_id=owner_user_id,
                party_id=party_id,
                turns=turns,
            )
            party = self.engine.get_party(
                owner_user_id=owner_user_id, party_id=party_id
            )
            if party.current_version != expected_version:
                raise RPPartyVersionConflict(
                    f"party {party_id!r} is at version {party.current_version}, "
                    f"not {expected_version}"
                )
            turns = self.engine.list_turns(
                owner_user_id=owner_user_id, party_id=party_id
            )
            memory = self.engine.latest_story_memory(
                owner_user_id=owner_user_id, party_id=party_id
            )
            derived = self.engine.derived_context(
                owner_user_id=owner_user_id, party_id=party_id
            )
            prompt = self.prompt_builder.build_turn(
                party=party,
                turns=turns,
                memory=memory,
                player_text=player_text,
                derived=derived,
            )
            narrator_text = await self._complete(prompt, player_text)
            return self.engine.complete_narration(
                owner_user_id=owner_user_id,
                party_id=party_id,
                turn_kind="narrative",
                request_id=request_id,
                idempotency_key=idempotency_key,
                expected_version=expected_version,
                player_text=player_text,
                narrator_text=narrator_text,
                claim_token=claim.request.claim_token,
            )
        except BaseException as exc:
            self._fail_claim(
                request_id=claim.request.id,
                claim_token=claim.request.claim_token,
                error=exc,
            )
            raise

    async def _wait_for_previous_atomic_service_jobs(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        turns: tuple[RPTurn, ...],
    ) -> None:
        if (
            not self.atomic_service_enabled
            or self.derived_wait_seconds == 0
            or not turns
        ):
            return
        source_version = turns[-1].committed_version
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.derived_wait_seconds
        while True:
            jobs = self.engine.service_jobs_for_source_version(
                owner_user_id=owner_user_id,
                party_id=party_id,
                source_version=source_version,
            )
            if all(job.status not in {"pending", "running"} for job in jobs):
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                return
            await asyncio.sleep(min(self.derived_poll_interval, remaining))

    async def narrate_opening(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        request_id: str,
        idempotency_key: str,
    ) -> RPTurn:
        _validate_request_fields(request_id, idempotency_key, 0)
        turns = self.engine.list_turns(owner_user_id=owner_user_id, party_id=party_id)
        replay = _turn_replay(
            turns,
            turn_kind="opening_scene",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=0,
            player_text="",
        )
        if replay is not None:
            return replay
        claim = self.engine.claim_narration(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind="opening_scene",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=0,
            player_text="",
        )
        if claim.turn is not None:
            return claim.turn
        if not claim.acquired:
            return await self._wait_for_narration(
                owner_user_id=owner_user_id,
                party_id=party_id,
                turn_kind="opening_scene",
                request_id=request_id,
                idempotency_key=idempotency_key,
                expected_version=0,
                player_text="",
            )
        if claim.request.claim_token is None:
            raise RuntimeError("acquired narration claim has no token")
        try:
            party = self.engine.get_party(
                owner_user_id=owner_user_id, party_id=party_id
            )
            if party.current_version != 0:
                raise RPPartyVersionConflict(
                    f"party {party_id!r} already started at version {party.current_version}"
                )
            prompt = self.prompt_builder.build_opening(party=party)
            narrator_text = await self._complete(prompt, None)
            return self.engine.complete_narration(
                owner_user_id=owner_user_id,
                party_id=party_id,
                turn_kind="opening_scene",
                request_id=request_id,
                idempotency_key=idempotency_key,
                expected_version=0,
                player_text="",
                narrator_text=narrator_text,
                claim_token=claim.request.claim_token,
            )
        except BaseException as exc:
            self._fail_claim(
                request_id=claim.request.id,
                claim_token=claim.request.claim_token,
                error=exc,
            )
            raise

    async def _wait_for_narration(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        turn_kind: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        player_text: str,
    ) -> RPTurn:
        while True:
            request = self.engine.get_narration_request(
                owner_user_id=owner_user_id,
                party_id=party_id,
                idempotency_key=idempotency_key,
            )
            if request.status == "failed":
                raise RPNarratorUnavailable(player_text or None)
            if request.status == "succeeded":
                replay = _turn_replay(
                    self.engine.list_turns(
                        owner_user_id=owner_user_id, party_id=party_id
                    ),
                    turn_kind=turn_kind,
                    request_id=request_id,
                    idempotency_key=idempotency_key,
                    expected_version=expected_version,
                    player_text=player_text,
                )
                if replay is None:
                    raise RuntimeError("succeeded narration has no committed turn")
                return replay
            await asyncio.sleep(0.01)

    def _fail_claim(
        self, *, request_id: int, claim_token: str, error: BaseException
    ) -> None:
        try:
            self.engine.fail_narration(
                request_id=request_id,
                claim_token=claim_token,
                error=str(error) or type(error).__name__,
            )
        except RPBackgroundJobConflict:
            pass

    async def _complete(
        self, prompt: RPNarratorPrompt, player_text: str | None
    ) -> str:
        try:
            narrator_text = await self.narrator.complete(prompt)
        except Exception as exc:
            raise RPNarratorUnavailable(player_text) from exc
        if not isinstance(narrator_text, str) or not narrator_text.strip():
            raise RPNarratorUnavailable(player_text)
        return narrator_text


def select_raw_turns(
    turns: tuple[RPTurn, ...],
    *,
    safe_coverage: int,
    window_turns: int,
    anchor_turns: int,
) -> tuple[RPTurn, ...]:
    """Return W..W+A-1 complete units plus every unit not safely covered."""
    if safe_coverage < 0:
        raise ValueError("safe_coverage must be non-negative")
    if window_turns <= 0 or anchor_turns <= 0:
        raise ValueError("RAW window and anchor must be positive")
    ordered = tuple(sorted(turns, key=lambda turn: turn.committed_version))
    if not ordered:
        return ()
    desired_start = max(len(ordered) - window_turns, 0)
    anchored_start = (desired_start // anchor_turns) * anchor_turns
    first_uncovered = next(
        (
            index
            for index, turn in enumerate(ordered)
            if turn.committed_version > safe_coverage
        ),
        len(ordered),
    )
    return ordered[min(anchored_start, first_uncovered) :]


def _turn_replay(
    turns: tuple[RPTurn, ...],
    *,
    turn_kind: str,
    request_id: str,
    idempotency_key: str,
    expected_version: int,
    player_text: str,
) -> RPTurn | None:
    for turn in turns:
        if turn.idempotency_key == idempotency_key:
            if (
                turn.turn_kind != turn_kind
                or turn.request_id != request_id
                or turn.expected_version != expected_version
                or turn.player_text != player_text
            ):
                raise RPIdempotencyConflict(
                    f"idempotency key {idempotency_key!r} already owns another turn"
                )
            return turn
        if turn.request_id == request_id:
            raise RPIdempotencyConflict(
                f"request {request_id!r} is already committed with another key"
            )
    return None


def _validate_request_fields(
    request_id: str, idempotency_key: str, expected_version: int
) -> None:
    if not isinstance(request_id, str) or not request_id.strip():
        raise ValueError("request_id must be a non-empty string")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ValueError("idempotency_key must be a non-empty string")
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 0
    ):
        raise ValueError("expected_version must be a non-negative integer")


def _world_content(world: object) -> str:
    return "\n".join(
        (
            "WORLD",
            f"title={world.title}",
            f"language={world.language}",
            f"premise={world.premise}",
            "CANON",
            *world.canon,
            "CHARACTERS",
            world.characters,
        )
    )


def _scenario_experience(scenario: object) -> str:
    return "\n".join(
        (
            "SCENARIO_EXPERIENCE",
            f"style={scenario.style}",
            f"format={scenario.format}",
            f"difficulty={scenario.difficulty or 'none'}",
            f"detail_level={scenario.detail_level}",
            scenario.narrator_system,
        )
    )


def _relationship_prompt_text(
    party: RPParty, derived: RPDerivedContext | None
) -> str:
    causes = derived.relationship_causes if derived is not None else ()
    totals = relationship_values(party, causes)
    recent: list[dict[str, object]] = []
    for cause in causes:
        recent.append(
            {
                "character_id": cause.character_id,
                "direction": cause.direction,
                "axis": cause.axis,
                "event": cause.event_id,
                "delta": cause.delta,
                "source_version": cause.source_version,
            }
        )
    payload = {
        "authored_starting_relationships": (
            party.scenario_snapshot.starting_relationships
        ),
        "current": [
            {
                "character_id": character_id,
                "direction": "character_to_player",
                "axis": axis,
                "value": value,
            }
            for (character_id, axis), value in sorted(totals.items())
        ],
        "recent_causes": recent[-20:],
    }
    return "PARTY_RELATIONSHIPS\n" + json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _raw_messages(turn: RPTurn) -> tuple[RPPromptMessage, ...]:
    block_id = f"raw:{turn.committed_version}"
    if turn.turn_kind == "opening_scene":
        return (RPPromptMessage("assistant", block_id, turn.narrator_text),)
    if turn.turn_kind == "narrative":
        return (
            RPPromptMessage("user", block_id, turn.player_text),
            RPPromptMessage("assistant", block_id, turn.narrator_text),
        )
    raise ValueError(f"unknown RP turn kind {turn.turn_kind!r}")
