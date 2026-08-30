"""Concrete one-call provider boundary for the rebuilt RP roles."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.rp.mechanics import (
    RPAdministratorResult,
    RPEvidenceSpan,
    RPRelationshipResult,
    RPRuntimeLoreResult,
)
from app.rp.memory import RPStoryMemoryRecord, RPStoryMemorySnapshot
from app.rp.narrator import RPNarratorPrompt
from app.rp.turn_engine import RPModelOutputRejected, RPParty, RPTurn
from app.services.provider_catalog import normalize_provider, validate_narrator_settings
from app.services.service_model_client import ServiceModelClient, service_prompt_text
from app.services.service_models import OPENROUTER_OPTIONAL_REASONING_MODELS


_ACTIVE_PROVIDERS = frozenset({"local", "gemini", "openrouter"})
_ResultT = TypeVar("_ResultT", bound=BaseModel)


class RPNarratorProvider:
    """Send the already assembled Narrator messages through one exact route."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str,
        model: str,
        narrator_settings: dict[str, Any] | None = None,
        party_id: str | None = None,
        request_id: str | None = None,
        client: ServiceModelClient | None = None,
    ) -> None:
        self.provider, self.model = _validated_route(provider, model)
        self.narrator_settings = validate_narrator_settings(
            self.provider, self.model, narrator_settings or {}
        )
        self.party_id = party_id
        self.request_id = request_id
        # Settings.sqlite_path remains the legacy diagnostic database. The clean
        # RP engine is owned separately through Settings.rp_sqlite_path.
        narrator_client_settings = settings
        if self.provider == "openrouter":
            narrator_client_settings = replace(
                settings,
                service_openrouter_api_key=settings.openrouter_api_key,
            )
        self.client = client or ServiceModelClient(narrator_client_settings)

    async def complete(self, prompt: RPNarratorPrompt) -> str:
        payload: dict[str, Any] = {
            "messages": [message.provider_message() for message in prompt.messages],
            "stream": False,
        }
        _apply_narrator_settings(
            payload,
            provider=self.provider,
            model=self.model,
            narrator_settings=self.narrator_settings,
        )
        completion = await self.client.complete(
            role="rp_narrator",
            provider=self.provider,  # type: ignore[arg-type]
            model=self.model,
            party_id=self.party_id,
            turn_id=None,
            request_id=self.request_id,
            prompt=service_prompt_text(payload),
            payload=payload,
        )
        return _completion_content(completion.data, role="Narrator")


class RPAtomicServiceProvider:
    """One structured provider route for the three atomic service operations."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str,
        model: str,
        client: ServiceModelClient | None = None,
    ) -> None:
        self.provider, self.model = _validated_route(provider, model)
        self.client = client or ServiceModelClient(settings)

    async def extract_relationships(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        evidence_spans: tuple[RPEvidenceSpan, ...],
    ) -> RPRelationshipResult:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель отношений. Используй только переданные "
                "события онтологии и пронумерованные фрагменты RAW. Не выдумывай "
                "персонажей, события или доказательства. Корень ответа содержит только "
                "candidates; не возвращай обёртки relationships, events или notes. "
                "Верни только строгий JSON."
            ),
            body={
                "task": "extract_relationships",
                "active_character_ids": list(
                    party.scenario_snapshot.active_character_ids
                ),
                "relationship_ontology": party.world_snapshot.relationship_ontology,
                "turn": _turn_payload(turn),
                "evidence_spans": _evidence_payload(evidence_spans),
            },
        )
        return await self._complete_result(
            role="rp_atomic_relationships",
            party=party,
            turn_id=turn.id,
            messages=messages,
            result_type=RPRelationshipResult,
            schema_name="rp_relationship_result",
        )

    async def extract_runtime_lore(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        evidence_spans: tuple[RPEvidenceSpan, ...],
    ) -> RPRuntimeLoreResult:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель runtime Lore. Создавай не более одной "
                "карточки и только из явно переданного RAW-доказательства. Если нового "
                "устойчивого факта нет, верни no_candidate. Корень ответа содержит ровно "
                "result, kind, title, content, keywords и evidence_span_ids; не возвращай "
                "cards или другую обёртку. Для no_candidate поле kind обязательно, а "
                "title, content, keywords и evidence_span_ids равны null. Верни только "
                "строгий JSON."
            ),
            body={
                "task": "extract_runtime_lore",
                "world_id": party.world_snapshot.world_id,
                "active_character_ids": list(
                    party.scenario_snapshot.active_character_ids
                ),
                "seed_lore_cards": list(party.world_snapshot.seed_lore_cards),
                "turn": _turn_payload(turn),
                "evidence_spans": _evidence_payload(evidence_spans),
            },
        )
        return await self._complete_result(
            role="rp_atomic_runtime_lore",
            party=party,
            turn_id=turn.id,
            messages=messages,
            result_type=RPRuntimeLoreResult,
            schema_name="rp_runtime_lore_result",
            max_tokens=400,
        )

    async def update_story_memory(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        previous: RPStoryMemoryRecord | None,
    ) -> RPStoryMemorySnapshot:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель памяти. Обнови пять секций памяти только "
                "по committed RAW. Не меняй RAW и не утверждай safe coverage за пределами "
                "переданных ходов. Верни сам полный snapshot без обёртки party_id или "
                "memory_snapshot. Корень содержит schema_version, observed_through_version, "
                "situation, threads, characters, assets_and_rules и chronology_and_hooks. "
                "schema_version равен rp-story-memory.v1; observed_through_version равен "
                "наибольшему переданному committed_version. coverage каждой секции — это "
                "номер committed-версии Party, а не процент; после проверки всех ходов он "
                "равен observed_through_version. Каждый элемент массива фактов внутри "
                "секций — JSON-объект по OUTPUT_SCHEMA; не добавляй в массивы фактов "
                "голые строки. "
                "Для успешного полного обновления status каждой секции равен fresh. "
                "Каждый факт содержит ровно fact_id, text, status, authority и "
                "source_turn_versions: status факта — active, superseded или retracted; "
                "authority — player, narrator или inference; source_turn_versions — "
                "положительные уникальные версии по возрастанию, не выше coverage. "
                "fact_id уникален во всём snapshot и соответствует OUTPUT_SCHEMA. "
                "Верни строгий JSON."
            ),
            body={
                "task": "update_story_memory",
                "party_id": party.id,
                "previous": (
                    previous.snapshot.model_dump(mode="json")
                    if previous is not None
                    else None
                ),
                "turns": [_turn_payload(turn) for turn in turns],
            },
        )
        return await self._complete_result(
            role="rp_atomic_story_memory",
            party=party,
            turn_id=turns[-1].id if turns else None,
            messages=messages,
            result_type=RPStoryMemorySnapshot,
            schema_name="rp_story_memory_snapshot",
        )

    async def _complete_result(
        self,
        *,
        role: str,
        party: RPParty,
        turn_id: int | None,
        messages: list[dict[str, str]],
        result_type: type[_ResultT],
        schema_name: str,
        max_tokens: int | None = None,
    ) -> _ResultT:
        payload = _structured_payload(
            messages=messages,
            result_type=result_type,
            schema_name=schema_name,
            provider=self.provider,
            model=self.model,
            max_tokens=max_tokens,
        )
        completion = await self.client.complete(
            role=role,
            provider=self.provider,  # type: ignore[arg-type]
            model=self.model,
            party_id=party.id,
            turn_id=turn_id,
            prompt=service_prompt_text(payload),
            payload=payload,
        )
        return _strict_result(completion.data, result_type=result_type, role=role)


class RPAdministratorProvider:
    """A route dedicated to Administrator suggest-mode reviews."""

    def __init__(
        self,
        settings: Settings,
        *,
        provider: str,
        model: str,
        client: ServiceModelClient | None = None,
    ) -> None:
        self.provider, self.model = _validated_route(provider, model)
        self.client = client or ServiceModelClient(settings)

    async def review_party(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
        evidence_spans: tuple[RPEvidenceSpan, ...],
        window_hash: str,
        before_text: str,
    ) -> RPAdministratorResult:
        messages = _structured_messages(
            system=(
                "Ты Администратор RP-партии в режиме suggest. Ты не меняешь World, RAW "
                "или партию напрямую. Разрешена только versioned-рекомендация для "
                "narrator_guidance, строго обоснованная переданным окном. Если правка не "
                "нужна, верни no_proposal. Корень ответа содержит ровно result, "
                "target_slot и after; не возвращай другую обёртку. Для no_proposal поля "
                "target_slot и after равны null. Верни только строгий JSON."
            ),
            body={
                "task": "review_party",
                "world_id": party.world_snapshot.world_id,
                "scenario_id": party.scenario_snapshot.scenario_id,
                "window_hash": window_hash,
                "current_narrator_guidance": before_text,
                "turns": [_turn_payload(turn) for turn in turns],
                "evidence_spans": _evidence_payload(evidence_spans),
            },
        )
        payload = _structured_payload(
            messages=messages,
            result_type=RPAdministratorResult,
            schema_name="rp_administrator_result",
            provider=self.provider,
            model=self.model,
        )
        completion = await self.client.complete(
            role="rp_administrator",
            provider=self.provider,  # type: ignore[arg-type]
            model=self.model,
            party_id=party.id,
            turn_id=turns[-1].id if turns else None,
            prompt=service_prompt_text(payload),
            payload=payload,
        )
        return _strict_result(
            completion.data,
            result_type=RPAdministratorResult,
            role="Administrator",
        )


def _validated_route(provider: str, model: str) -> tuple[str, str]:
    clean_provider = normalize_provider(provider)
    if clean_provider not in _ACTIVE_PROVIDERS:
        raise ValueError(f"provider is retired or unsupported: {provider}")
    clean_model = model.strip()
    if not clean_model:
        raise ValueError("model must contain text")
    return clean_provider, clean_model


def _apply_narrator_settings(
    payload: dict[str, Any],
    *,
    provider: str,
    model: str,
    narrator_settings: dict[str, Any],
) -> None:
    if narrator_settings.get("temperature") is not None:
        payload["temperature"] = float(narrator_settings["temperature"])
    if narrator_settings.get("top_p") is not None:
        payload["top_p"] = float(narrator_settings["top_p"])
    if narrator_settings.get("max_tokens") is not None:
        payload["max_tokens"] = int(narrator_settings["max_tokens"])
    effort = narrator_settings.get("reasoning_effort")
    if effort == "none":
        payload["reasoning"] = {"enabled": False}
    elif effort:
        payload["reasoning"] = {"effort": effort, "exclude": True}
    if provider != "openrouter":
        return
    provider_preferences: dict[str, Any] = {}
    if model.casefold() == "deepseek/deepseek-v4-flash":
        provider_preferences["sort"] = "throughput"
    if narrator_settings:
        provider_preferences["require_parameters"] = True
    if provider_preferences:
        payload["provider"] = provider_preferences


def _structured_messages(
    *, system: str, body: dict[str, Any]
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _canonical_json(body)},
    ]


def _structured_payload(
    *,
    messages: list[dict[str, str]],
    result_type: type[BaseModel],
    schema_name: str,
    provider: str,
    model: str,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    schema = result_type.model_json_schema(mode="validation")
    prompt_messages = [dict(message) for message in messages]
    prompt_messages[0]["content"] = (
        f"{prompt_messages[0]['content']} "
        "Верни ровно один JSON-объект, который проходит OUTPUT_SCHEMA. "
        "Не повторяй и не оборачивай входные данные. "
        f"OUTPUT_SCHEMA={_canonical_json(schema)}"
    )
    payload: dict[str, Any] = {
        "messages": prompt_messages,
        "stream": False,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": schema,
            },
        },
    }
    if provider == "openrouter":
        payload["provider"] = {"require_parameters": True}
        if model in OPENROUTER_OPTIONAL_REASONING_MODELS:
            payload["reasoning"] = {"enabled": False}
    else:
        payload["reasoning"] = {"enabled": False}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _strict_result(
    data: dict[str, Any], *, result_type: type[_ResultT], role: str
) -> _ResultT:
    content = _completion_content(data, role=role)
    try:
        return result_type.model_validate_json(content, strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        raise RPModelOutputRejected(f"{role} returned invalid strict JSON: {exc}") from exc


def _completion_content(data: dict[str, Any], *, role: str) -> str:
    choices = data.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise RPModelOutputRejected(f"{role} response must contain exactly one choice")
    choice = choices[0]
    if str(choice.get("finish_reason") or "").strip().casefold() == "length":
        raise RPModelOutputRejected(f"{role} response was truncated")
    message = choice.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("content"), str):
        raise RPModelOutputRejected(f"{role} response content is missing")
    content = str(message["content"])
    if not content.strip():
        raise RPModelOutputRejected(f"{role} response content is empty")
    return content


def _turn_payload(turn: RPTurn) -> dict[str, Any]:
    return {
        "id": turn.id,
        "turn_kind": turn.turn_kind,
        "committed_version": turn.committed_version,
        "player_text": turn.player_text,
        "narrator_text": turn.narrator_text,
    }


def _evidence_payload(
    evidence_spans: tuple[RPEvidenceSpan, ...],
) -> list[dict[str, Any]]:
    return [span.model_dump(mode="json") for span in evidence_spans]


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
