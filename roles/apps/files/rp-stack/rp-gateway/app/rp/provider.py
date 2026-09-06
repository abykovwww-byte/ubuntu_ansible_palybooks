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
    RPPlayerCorrectionResult,
    RPRelationshipResult,
    RPRuntimeLoreResult,
)
from app.rp.memory import (
    RP_MEMORY_BATCH_TURNS,
    RP_MEMORY_CHUNK_MAX_CHARS,
    RP_MEMORY_RAW_CHUNK_MAX_CHARS,
)
from app.rp.narrator import RPNarratorPrompt
from app.rp.turn_engine import (
    RPModelOutputRejected,
    RPParty,
    RPRuntimeLoreCard,
    RPTurn,
)
from app.services.provider_catalog import (
    NARRATOR_MODEL,
    normalize_provider,
    openrouter_model_is_active,
    validate_narrator_settings,
)
from app.services.service_model_client import ServiceModelClient, service_prompt_text


_ACTIVE_PROVIDERS = frozenset({"local", "gemini", "openrouter"})
RP_ATOMIC_MODEL = "deepseek/deepseek-v4-pro"
RP_ATOMIC_OPENROUTER_PROVIDER = "baidu/fp8"
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
        if (self.provider, self.model) != ("openrouter", NARRATOR_MODEL):
            raise ValueError("Narrator requires the fixed OpenRouter model route")
        self.narrator_settings = validate_narrator_settings(
            self.provider, self.model, narrator_settings or {}
        )
        self.party_id = party_id
        self.request_id = request_id
        # The shared database stores auth and redacted call diagnostics. Party
        # state is owned separately through Settings.rp_sqlite_path.
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
        if (self.provider, self.model) != ("openrouter", RP_ATOMIC_MODEL):
            raise ValueError("atomic service requires the fixed OpenRouter model route")
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
                "персонажей, события или доказательства. Для каждого кандидата его "
                "character_id должен быть однозначно подтверждён хотя бы одним из "
                "указанных evidence_span_ids. Не приписывай безымянного участника "
                "известному персонажу только потому, что его ID допустим. Если участник "
                "не идентифицируется однозначно, не возвращай кандидата. Кандидат "
                "допустим только для персонажа, который сам присутствует в RAW и чьё "
                "собственное действие или прямое взаимодействие с игроком подтверждает "
                "событие. Упоминание отсутствующего персонажа как адресата отчёта, "
                "руководителя, владельца, темы разговора или получателя материала не "
                "является его действием и не создаёт событие. Для kept_agreement RAW "
                "должен показывать, что именно этот персонаж лично выполнил ранее данное "
                "соглашение. Возвращай пустой candidates, если отдельного значимого "
                "события отношений нет; не заполняй ответ ради активности. Обычное "
                "исполнение роли, текущей просьбы или инструкции, совместная работа и "
                "упоминание ранее пережитого риска сами по себе не являются "
                "kept_agreement, voluntary_help_given или shared_risk. shared_risk "
                "требует риска, который прямо присутствует в этом RAW и которому "
                "подвергаются обе стороны; kept_agreement требует подтверждённых в RAW "
                "ранее данного обещания и его исполнения, а не просто выполнения "
                "текущей просьбы. Выбранные evidence_span_ids должны сами содержать "
                "имя или alias персонажа либо включать предшествующий выбранный фрагмент, "
                "который прямо называет говорящего; не опирайся на невыбранный контекст. "
                "honest_warning требует новой существенной опасности, ограничения или "
                "цены решения, а не процедурного напоминания. event_id — "
                "kept_agreement нельзя возвращать в ходе, где соглашение только "
                "формулируется, подтверждается или обещается на будущее: выбранные RAW "
                "должны одновременно показывать два разных факта: обязательство уже "
                "существовало до текущего действия, и персонаж фактически исполнил его "
                "сейчас. Фразы о том, что персонаж подтверждает границу, договаривается, "
                "обещает, собирается или будет действовать определённым образом, "
                "устанавливают будущее обязательство, но не доказывают его исполнение; "
                "без отдельного факта исполнения возвращай пустой candidates. "
                "только точный строковый ключ из relationship_ontology.events, никогда "
                "не weight, delta или порядковый номер. Для кандидата выбери от одного "
                "до восьми самых сильных evidence_span_ids, не больше восьми. Корень "
                "ответа содержит только candidates; не возвращай обёртки relationships, "
                "events или notes. Верни только строгий JSON."
            ),
            body={
                "task": "extract_relationships",
                "active_character_ids": list(
                    party.scenario_snapshot.active_character_ids
                ),
                "active_character_references": _active_character_references(party),
                "extraction_constraints": {
                    "selected_evidence_must_identify_character": True,
                    "routine_role_or_current_request_is_not_an_event": True,
                    "honest_warning_requires_material_new_risk_or_limit": True,
                    "kept_agreement_requires_preexisting_obligation": True,
                    "kept_agreement_requires_current_fulfillment": True,
                    "agreement_creation_confirmation_or_future_intent_is_not_fulfillment": True,
                },
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
            max_tokens=2_048,
        )

    async def extract_runtime_lore(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        evidence_spans: tuple[RPEvidenceSpan, ...],
        existing_runtime_lore: tuple[RPRuntimeLoreCard, ...] = (),
    ) -> RPRuntimeLoreResult:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель runtime Lore. Создавай не более одной "
                "карточки и только из явно переданного RAW-доказательства. Если нового "
                "устойчивого факта нет, верни no_candidate. Не отождествляй новое или "
                "неполностью названное лицо с каноническим персонажем либо seed Lore "
                "card без однозначного RAW-доказательства; совпавшего имени недостаточно. "
                "Сохраняй обозначение лица ровно настолько точным, насколько позволяет "
                "RAW. Каждый фактический тезис title, content и keywords должен прямо "
                "подтверждаться одним из выбранных evidence_span_ids; не используй в "
                "карточке невыбранные фрагменты. Не создавай карточку, которая лишь "
                "повторяет или пересказывает existing_runtime_lore_cards; новый RAW "
                "должен добавлять отдельный устойчивый факт. Сохраняй сомнение, "
                "атрибуцию и неизвестность RAW, не превращай версию в установленный факт. "
                "Корень ответа содержит ровно result, kind, title, content, keywords "
                "и evidence_span_ids; не возвращай cards или другую обёртку. Для "
                "no_candidate поле kind обязательно, а title, content, keywords и "
                "evidence_span_ids равны null. Верни только строгий JSON."
            ),
            body={
                "task": "extract_runtime_lore",
                "world_id": party.world_snapshot.world_id,
                "active_character_ids": list(
                    party.scenario_snapshot.active_character_ids
                ),
                "active_character_references": _active_character_references(party),
                "seed_lore_cards": list(party.world_snapshot.seed_lore_cards),
                "existing_runtime_lore_cards": [
                    {
                        "kind": card.kind,
                        "title": card.title,
                        "content": card.content,
                        "keywords": list(card.keywords),
                        "source_version": card.source_version,
                    }
                    for card in existing_runtime_lore
                ],
                "draft_constraints": {
                    "every_claim_uses_selected_evidence": True,
                    "duplicate_or_recap_returns_no_candidate": True,
                    "preserve_uncertainty_and_attribution": True,
                },
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
            max_tokens=2_048,
        )

    async def draft_player_lore(
        self,
        *,
        party: RPParty,
        turn: RPTurn,
        kind: str,
        evidence_spans: tuple[RPEvidenceSpan, ...],
        existing_runtime_lore: tuple[RPRuntimeLoreCard, ...] = (),
    ) -> RPRuntimeLoreResult:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель player Lore. Вид карточки задан до "
                "вызова и не может меняться. Используй только один переданный полный "
                "committed turn и выбранные evidence_span_ids. Не дополняй RAW знаниями "
                "мира и не создавай карточку из предположения. Для no_candidate верни "
                "обязательный requested kind и null во всех draft-полях. Для draft "
                "каждый тезис должен прямо подтверждаться выбранными spans. Корень "
                "содержит ровно result, kind, title, content, keywords и "
                "evidence_span_ids. Верни только строгий JSON."
            ),
            body={
                "task": "draft_player_lore",
                "requested_kind": kind,
                "turn": _turn_payload(turn),
                "evidence_spans": _evidence_payload(evidence_spans),
                "world_lore_cards": list(party.world_snapshot.seed_lore_cards),
                "scenario_lore_cards": [
                    card.model_dump(mode="json")
                    for card in party.scenario_snapshot.local_overrides.lore_cards
                ],
                "existing_runtime_lore_cards": [
                    {
                        "kind": card.kind,
                        "title": card.title,
                        "content": card.content,
                        "keywords": list(card.keywords),
                    }
                    for card in existing_runtime_lore
                ],
            },
        )
        return await self._complete_result(
            role="rp_atomic_player_lore",
            party=party,
            turn_id=turn.id,
            messages=messages,
            result_type=RPRuntimeLoreResult,
            schema_name="rp_player_lore_result",
            max_tokens=2_048,
        )

    async def draft_player_correction(
        self,
        *,
        party: RPParty,
        instruction: str,
        raw_hint: str | None,
        candidates: tuple[dict[str, Any], ...],
    ) -> RPPlayerCorrectionResult:
        messages = _structured_messages(
            system=(
                "Ты атомарная служебная модель PlayerCorrection. Выбери только один "
                "точный target_slot из переданного ranked_candidates. Не создавай новые "
                "targets и не исправляй факты по знаниям вне payload. raw_hint может "
                "быть широким raw:<turn_id> или точным raw:<turn_id>:<claim_id>, но не "
                "является доказательством сам по себе. Если ни один candidate не "
                "соответствует просьбе игрока, верни no_target с null target_slot, "
                "action, after и пустым forbidden_claims. Для retract after равен null; "
                "для replace after содержит только исправленный текст. Корень ответа "
                "плоский: result, target_slot, action, after, forbidden_claims. Для "
                "memory и raw forbidden_claims всегда пуст; только для rule это полный "
                "новый список запрещённых формулировок. Верни только строгий JSON."
            ),
            body={
                "task": "draft_player_correction",
                "instruction": instruction,
                "raw_hint": raw_hint,
                "ranked_candidates": list(candidates),
            },
        )
        return await self._complete_result(
            role="rp_atomic_player_correction",
            party=party,
            turn_id=None,
            messages=messages,
            result_type=RPPlayerCorrectionResult,
            schema_name="rp_player_correction_result",
            max_tokens=2_048,
        )

    async def update_story_memory(
        self,
        *,
        party: RPParty,
        turns: tuple[RPTurn, ...],
    ) -> str:
        if not turns:
            raise ValueError("story-memory compression requires RAW turns")
        expected_versions = tuple(
            range(turns[0].committed_version, turns[-1].committed_version + 1)
        )
        if tuple(turn.committed_version for turn in turns) != expected_versions:
            raise ValueError("story-memory RAW turns must be contiguous")
        level = 1
        expected_count = RP_MEMORY_BATCH_TURNS
        while expected_count < len(turns):
            level += 1
            expected_count *= RP_MEMORY_BATCH_TURNS
        if expected_count != len(turns):
            raise ValueError("story-memory RAW span must contain a base-eight batch")
        max_chars = (
            RP_MEMORY_RAW_CHUNK_MAX_CHARS
            if level == 1
            else RP_MEMORY_CHUNK_MAX_CHARS
        )
        target_chars = "600–1200" if level == 1 else "2000–4000"
        source = "\n\n".join(
            (
                f"[TURN {turn.committed_version}]\n"
                f"PLAYER\n{turn.player_text}\n"
                f"NARRATOR\n{turn.narrator_text}"
            )
            for turn in turns
        )
        messages = [
            {
                "role": "system",
                "content": (
                    "Ты сжимаешь старые RAW-ходы ролевой партии в связную narrative "
                    "memory для будущего Narrator. Сохраняй причинно значимые действия, "
                    "решения, обещания, последствия, изменения отношений, появления и "
                    "утраты возможностей. Удаляй повторы, декоративные реплики и уже "
                    "исчерпанные подробности. Не придумывай факты и не исправляй RAW. "
                    "Верни только компактный связный текст из нескольких абзацев: без "
                    "JSON, таблиц, списков, заголовков, служебных полей и комментариев."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Сожми RAW-ходы {turns[0].committed_version}-"
                    f"{turns[-1].committed_version}. Ориентир — {target_chars} символов; "
                    "сохраняй больше только ради причинно значимых событий. Итог должен "
                    "быть короче источника "
                    f"и не длиннее {max_chars} символов.\n\n{source}"
                ),
            },
        ]
        payload: dict[str, Any] = {
            "messages": messages,
            "stream": False,
            "temperature": 0,
            "max_tokens": 1_024 if level == 1 else 2_048,
            "reasoning": {"enabled": False},
            "provider": {
                **_exact_openrouter_provider(self.model),
                "require_parameters": True,
            },
        }
        completion = await self.client.complete(
            role="rp_atomic_story_memory",
            provider=self.provider,  # type: ignore[arg-type]
            model=self.model,
            party_id=party.id,
            turn_id=turns[-1].id,
            request_id=None,
            prompt=service_prompt_text(payload),
            payload=payload,
        )
        narrative = _completion_content(
            completion.data, role="rp_atomic_story_memory"
        ).strip()
        if narrative.startswith(("```", "{", "[")):
            raise RPModelOutputRejected(
                "rp_atomic_story_memory returned structured data instead of prose"
            )
        return narrative

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
        if (self.provider, self.model) != ("local", "gemma-4-26b-a4b-it-rp-q4"):
            raise ValueError("Administrator requires the fixed local model route")
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
                "нужна, верни no_proposal. Поле after — только краткая инструкция "
                "Narrator о поведении в будущих ответах, исправляющая наблюдаемую "
                "повторяющуюся ошибку. after не является продолжением сцены, репликой "
                "персонажа, отчётом, пересказом RAW или новым каноном; не добавляй в "
                "него факты и события. Если устойчивой коррекции Narrator из окна не "
                "следует, верни no_proposal. Корень ответа содержит ровно result, "
                "target_slot и after; не возвращай другую обёртку. Для no_proposal поля "
                "target_slot и after равны null. Верни только строгий JSON."
            ),
            body={
                "task": "review_party",
                "guidance_contract": {
                    "future_narrator_instruction_only": True,
                    "scene_or_character_dialogue_forbidden": True,
                    "raw_recap_or_new_canon_forbidden": True,
                },
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
            max_tokens=2_048,
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
    if clean_provider == "openrouter" and not (
        openrouter_model_is_active(clean_model)
        or clean_model.casefold() == RP_ATOMIC_MODEL
    ):
        raise ValueError(f"OpenRouter model route is retired or unsafe: {clean_model}")
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
    provider_preferences: dict[str, Any] = _exact_openrouter_provider(model)
    if narrator_settings:
        provider_preferences["require_parameters"] = True
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
        "temperature": 0,
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
        payload["provider"] = {
            **_exact_openrouter_provider(model),
            "require_parameters": True,
        }
        payload["reasoning"] = {"enabled": False}
    else:
        payload["reasoning"] = {"enabled": False}
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    return payload


def _exact_openrouter_provider(model: str) -> dict[str, Any]:
    clean_model = model.casefold()
    if clean_model == NARRATOR_MODEL:
        exact_provider = "openai"
    elif clean_model == RP_ATOMIC_MODEL:
        exact_provider = RP_ATOMIC_OPENROUTER_PROVIDER
    else:
        raise ValueError(f"OpenRouter model has no accepted exact provider route: {model}")
    return {
        "order": [exact_provider],
        "only": [exact_provider],
        "allow_fallbacks": False,
    }


def _strict_result(
    data: dict[str, Any], *, result_type: type[_ResultT], role: str
) -> _ResultT:
    content = _completion_content(data, role=role)
    try:
        return result_type.model_validate_json(content, strict=True)
    except (ValidationError, ValueError, TypeError) as exc:
        raise RPModelOutputRejected(f"{role} returned invalid strict JSON: {exc}") from exc


def _completion_content(data: dict[str, Any], *, role: str) -> str:
    response_error = data.get("error")
    if isinstance(response_error, dict):
        code = str(response_error.get("code") or "unknown")
        message = str(response_error.get("message") or "provider completion failed")
        raise RuntimeError(f"{role} provider error {code}: {message}")
    choices = data.get("choices")
    if (
        not isinstance(choices, list)
        or len(choices) != 1
        or not isinstance(choices[0], dict)
    ):
        raise RPModelOutputRejected(f"{role} response must contain exactly one choice")
    choice = choices[0]
    finish_reason = str(choice.get("finish_reason") or "").strip().casefold()
    if finish_reason == "error":
        error = choice.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "unknown")
            message = str(error.get("message") or "provider completion failed")
            raise RuntimeError(f"{role} provider error {code}: {message}")
        raise RuntimeError(f"{role} provider completion failed")
    if finish_reason == "length":
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


def _active_character_references(party: RPParty) -> list[dict[str, Any]]:
    active_ids = party.scenario_snapshot.active_character_ids
    references: dict[str, dict[str, Any]] = {}
    for bundle in party.world_snapshot.seed_lore_cards:
        cards = bundle.get("cards") if isinstance(bundle, dict) else None
        if not isinstance(cards, list):
            continue
        for card in cards:
            if not isinstance(card, dict):
                continue
            key = card.get("key")
            if not isinstance(key, str) or not key.startswith("npc:"):
                continue
            character_id = key.removeprefix("npc:")
            if character_id not in active_ids:
                continue
            reference: dict[str, Any] = {"character_id": character_id}
            title = card.get("title")
            if isinstance(title, str) and title.strip():
                reference["title"] = title
            keywords = card.get("keywords")
            if isinstance(keywords, list) and all(
                isinstance(keyword, str) and keyword.strip() for keyword in keywords
            ):
                reference["aliases"] = keywords
            references[character_id] = reference
    return [
        references.get(character_id, {"character_id": character_id})
        for character_id in active_ids
    ]


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
