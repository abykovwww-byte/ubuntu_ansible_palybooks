from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.core.config import Settings
from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.mechanics import RPEvidenceSpan
from app.rp.narrator import RPNarratorPrompt, RPPromptMessage
from app.rp.provider import (
    RP_ATOMIC_MODEL,
    RP_ATOMIC_OPENROUTER_PROVIDER,
    RPAdministratorProvider,
    RPAtomicServiceProvider,
    RPNarratorProvider,
)
from app.rp.turn_engine import (
    RPModelOutputRejected,
    RPParty,
    RPRuntimeLoreCard,
    RPTurn,
    RPTurnEngine,
)
from app.services.provider_catalog import NARRATOR_MODEL
from app.services.service_model_client import ServiceCompletion, ServiceModelClient


class RecordingClient:
    def __init__(self, *results: dict[str, Any] | Exception):
        self.results = list(results)
        self.calls: list[dict[str, Any]] = []

    async def complete(self, **kwargs: Any) -> ServiceCompletion:
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return ServiceCompletion(
            data=result,
            raw_response=json.dumps(result),
            status="completed",
            status_code=200,
        )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'legacy-diagnostics.db'}",
        rp_database_url=f"sqlite:///{tmp_path / 'rp-clean.db'}",
        openrouter_api_key="party-narrator-key",
        service_openrouter_api_key="stack-service-key",
    )


def _completion(content: str, *, finish_reason: str = "stop") -> dict[str, Any]:
    return {
        "model": "provider-response-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": finish_reason,
            }
        ],
    }


def _party() -> RPParty:
    world = WorldSnapshot(
        schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
        world_id="day-watch-moscow-v2",
        title="Дневной Дозор",
        language="ru",
        premise="Москва, где действует Дозор.",
        canon=("Мир держится на договоре Света и Тьмы.",),
        setting_rules="Нельзя менять RAW задним числом.",
        characters="anton: сотрудник Дозора",
        relationship_ontology={
            "axes": {
                "loyalty": {"min": -100, "max": 100, "per_turn_cap": 10}
            },
            "events": {"helped": {"axis": "loyalty", "weight": 2}},
        },
        seed_lore_cards=(
            {
                "cards": [
                    {
                        "key": "npc:anton",
                        "title": "Антон Городецкий",
                        "content": "Антон — сотрудник Дозора.",
                        "keywords": ["Антон", "Антона", "Антону"],
                    },
                    {
                        "key": "law:treaty",
                        "title": "Договор",
                        "content": "Договор ограничивает обе стороны.",
                        "keywords": ["договор"],
                    },
                ]
            },
        ),
    )
    scenario = ScenarioSnapshot(
        schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
        scenario_id="preset-one",
        title="Первая смена",
        world_id=world.world_id,
        source="preset",
        player_role="Сотрудник Дозора",
        style="Сдержанный городской фэнтези",
        format="Сцена",
        difficulty="Средняя",
        detail_level="Подробно",
        narrator_system="Веди сцену.",
        narrator_note="Сохраняй агентность игрока.",
        opening="Началась смена.",
        initial_state={
            "player": {},
            "characters": {"anton": {"trust": 0}},
            "relationships": {"anton": {"loyalty": 0}},
            "factions": {},
            "locations": {},
        },
        active_character_ids=("anton",),
        starting_relationships={"anton": {"loyalty": 0}},
    )
    return RPParty(
        id="party-one",
        owner_user_id="owner-one",
        title="Первая смена",
        narrator_profile_id="openrouter-openai-gpt-5-6-luna-pro",
        narrator_provider="openrouter",
        narrator_base_url="https://openrouter.ai/api/v1",
        narrator_model=NARRATOR_MODEL,
        narrator_settings={},
        world_snapshot=world,
        world_hash="a" * 64,
        scenario_snapshot=scenario,
        scenario_hash="b" * 64,
        current_version=1,
        created_at=1,
        updated_at=1,
    )


def _turn() -> RPTurn:
    return RPTurn(
        id=7,
        party_id="party-one",
        turn_kind="narrative",
        request_id="request-seven",
        idempotency_key="key-seven",
        expected_version=0,
        committed_version=1,
        player_text="Я помогаю Антону.",
        narrator_text="Антон принимает помощь.",
        created_at=1,
    )


def _memory_turns() -> tuple[RPTurn, ...]:
    turn = _turn()
    return tuple(
        replace(
            turn,
            id=version,
            request_id=f"memory-request-{version}",
            idempotency_key=f"memory-key-{version}",
            expected_version=version - 1,
            committed_version=version,
            player_text=f"Действие игрока {version}." * 4,
            narrator_text=f"Последствие действия {version}." * 4,
            created_at=version,
        )
        for version in range(1, 9)
    )


def _spans() -> tuple[RPEvidenceSpan, ...]:
    return (
        RPEvidenceSpan(
            id=1,
            turn_version=1,
            role="assistant",
            text="Антон принимает помощь.",
        ),
    )


def test_narrator_sends_exact_messages_once_without_fallback_or_repair(
    tmp_path: Path,
) -> None:
    client = RecordingClient(_completion("Сцена продолжается."))
    provider = RPNarratorProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=NARRATOR_MODEL,
        narrator_settings={
            "reasoning_effort": "xhigh",
            "max_tokens": 4096,
        },
        party_id="party-one",
        request_id="request-one",
        client=client,  # type: ignore[arg-type]
    )
    prompt = RPNarratorPrompt(
        messages=(
            RPPromptMessage("system", "world", "WORLD EXACT"),
            RPPromptMessage("assistant", "raw", "RAW EXACT"),
            RPPromptMessage("user", "player", "PLAYER EXACT"),
        ),
        raw_turn_versions=(1,),
        safe_memory_coverage=0,
        stable_prefix_hash="stable",
        input_chars=30,
    )

    result = asyncio.run(provider.complete(prompt))

    assert result == "Сцена продолжается."
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["provider"] == "openrouter"
    assert call["model"] == NARRATOR_MODEL
    assert call["payload"]["messages"] == [
        {"role": "system", "content": "WORLD EXACT"},
        {"role": "assistant", "content": "RAW EXACT"},
        {"role": "user", "content": "PLAYER EXACT"},
    ]
    assert call["payload"]["provider"] == {
        "order": ["openai"],
        "only": ["openai"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
    assert call["payload"]["reasoning"] == {
        "effort": "xhigh",
        "exclude": True,
    }
    assert "fallback" not in call["payload"]
    assert "repair" not in call["payload"]


@pytest.mark.parametrize(
    "response",
    [
        {"model": "narrator/model", "choices": []},
        _completion("Оборванный ответ", finish_reason="length"),
    ],
)
def test_narrator_malformed_or_truncated_response_is_terminal_at_boundary(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    client = RecordingClient(response)
    provider = RPNarratorProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=NARRATOR_MODEL,
        client=client,  # type: ignore[arg-type]
    )
    prompt = RPNarratorPrompt(
        messages=(RPPromptMessage("user", "player", "Продолжай."),),
        raw_turn_versions=(),
        safe_memory_coverage=0,
        stable_prefix_hash="stable",
        input_chars=10,
    )

    with pytest.raises(RPModelOutputRejected):
        asyncio.run(provider.complete(prompt))

    assert len(client.calls) == 1


def test_atomic_and_administrator_use_separate_exact_routes(tmp_path: Path) -> None:
    atomic_client = RecordingClient(
        _completion('{"candidates":[]}'),
        _completion(
            '{"result":"no_candidate","kind":"event","title":null,'
            '"content":null,"keywords":null,"evidence_span_ids":null}'
        ),
        _completion("Антон принял помощь игрока, и их сотрудничество укрепилось."),
    )
    administrator_client = RecordingClient(
        _completion(
            '{"result":"no_proposal","target_slot":null,"after":null}'
        )
    )
    atomic = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=atomic_client,  # type: ignore[arg-type]
    )
    administrator = RPAdministratorProvider(
        _settings(tmp_path),
        provider="local",
        model="gemma-4-26b-a4b-it-rp-q4",
        client=administrator_client,  # type: ignore[arg-type]
    )
    party = _party()
    turn = _turn()
    memory_turns = _memory_turns()
    spans = _spans()
    existing_lore = RPRuntimeLoreCard(
        id=1,
        party_id=party.id,
        service_job_id=1,
        source_turn_id=turn.id,
        source_version=turn.committed_version,
        card_key="runtime-card-key",
        kind="event",
        origin="runtime",
        title="Сохранённое событие",
        content="Уже сохранённый устойчивый факт.",
        keywords=("событие",),
        evidence_span_ids=(1,),
        enabled=True,
        created_at=1,
        authoring_kind="event",
    )

    async def exercise() -> None:
        relationships = await atomic.extract_relationships(
            party=party, turn=turn, evidence_spans=spans
        )
        lore = await atomic.extract_runtime_lore(
            party=party,
            turn=turn,
            evidence_spans=spans,
            existing_runtime_lore=(existing_lore,),
        )
        story_memory = await atomic.update_story_memory(
            party=party, turns=memory_turns
        )
        review = await administrator.review_party(
            party=party,
            turns=(turn,),
            evidence_spans=spans,
            window_hash="window-hash",
            before_text="",
        )
        assert relationships.candidates == ()
        assert lore.result == "no_candidate"
        assert story_memory.startswith("Антон принял помощь")
        assert review.result == "no_proposal"

    asyncio.run(exercise())

    assert [call["model"] for call in atomic_client.calls] == [
        RP_ATOMIC_MODEL,
        RP_ATOMIC_MODEL,
        RP_ATOMIC_MODEL,
    ]
    assert [call["role"] for call in atomic_client.calls] == [
        "rp_atomic_relationships",
        "rp_atomic_runtime_lore",
        "rp_atomic_story_memory",
    ]
    assert all(
        call["payload"]["reasoning"] == {"enabled": False}
        for call in atomic_client.calls
    )
    assert all(call["payload"]["temperature"] == 0 for call in atomic_client.calls)
    assert all(call["provider"] == "openrouter" for call in atomic_client.calls)
    assert all(
        call["payload"]["provider"]
        == {
            "order": [RP_ATOMIC_OPENROUTER_PROVIDER],
            "only": [RP_ATOMIC_OPENROUTER_PROVIDER],
            "allow_fallbacks": False,
            "require_parameters": True,
        }
        for call in atomic_client.calls
    )
    expected_roots = (
        {"candidates"},
        {"result", "kind", "title", "content", "keywords", "evidence_span_ids"},
    )
    for call, expected_root in zip(atomic_client.calls[:2], expected_roots, strict=True):
        response_format = call["payload"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == expected_root
        assert "OUTPUT_SCHEMA=" in call["payload"]["messages"][0]["content"]
    assert atomic_client.calls[0]["payload"]["max_tokens"] == 2_048
    relationship_messages = atomic_client.calls[0]["payload"]["messages"]
    relationship_body = json.loads(relationship_messages[1]["content"])
    assert relationship_body["extraction_constraints"] == {
        "selected_evidence_must_identify_character": True,
        "routine_role_or_current_request_is_not_an_event": True,
        "honest_warning_requires_material_new_risk_or_limit": True,
        "kept_agreement_requires_preexisting_obligation": True,
        "kept_agreement_requires_current_fulfillment": True,
        "agreement_creation_confirmation_or_future_intent_is_not_fulfillment": True,
    }
    assert relationship_body["active_character_references"] == [
        {
            "character_id": "anton",
            "title": "Антон Городецкий",
            "aliases": ["Антон", "Антона", "Антону"],
        }
    ]
    lore_messages = atomic_client.calls[1]["payload"]["messages"]
    lore_body = json.loads(lore_messages[1]["content"])
    assert lore_body["active_character_references"] == relationship_body[
        "active_character_references"
    ]
    assert lore_body["existing_runtime_lore_cards"] == [
        {
            "kind": "event",
            "title": "Сохранённое событие",
            "content": "Уже сохранённый устойчивый факт.",
            "keywords": ["событие"],
            "source_version": 1,
        }
    ]
    assert lore_body["draft_constraints"] == {
        "every_claim_uses_selected_evidence": True,
        "duplicate_or_recap_returns_no_candidate": True,
        "preserve_uncertainty_and_attribution": True,
    }
    memory_payload = atomic_client.calls[2]["payload"]
    memory_messages = memory_payload["messages"]
    assert "[TURN 1]" in memory_messages[1]["content"]
    assert "[TURN 8]" in memory_messages[1]["content"]
    assert "Ориентир — 600–1200 символов" in memory_messages[1]["content"]
    assert "Верни только компактный связный текст" in memory_messages[0]["content"]
    assert "OUTPUT_SCHEMA=" not in memory_messages[0]["content"]
    assert "response_format" not in memory_payload
    assert atomic_client.calls[1]["payload"]["max_tokens"] == 2_048
    assert memory_payload["max_tokens"] == 1_024
    assert len(administrator_client.calls) == 1
    assert administrator_client.calls[0]["provider"] == "local"
    assert administrator_client.calls[0]["model"] == "gemma-4-26b-a4b-it-rp-q4"
    assert administrator_client.calls[0]["role"] == "rp_administrator"
    assert administrator_client.calls[0]["payload"]["reasoning"] == {
        "enabled": False
    }
    assert administrator_client.calls[0]["payload"]["temperature"] == 0
    assert administrator_client.calls[0]["payload"]["max_tokens"] == 2_048
    assert "provider" not in administrator_client.calls[0]["payload"]
    administrator_body = json.loads(
        administrator_client.calls[0]["payload"]["messages"][1]["content"]
    )
    assert administrator_body["guidance_contract"] == {
        "future_narrator_instruction_only": True,
        "scene_or_character_dialogue_forbidden": True,
        "raw_recap_or_new_canon_forbidden": True,
    }
    administrator_schema = administrator_client.calls[0]["payload"]["response_format"][
        "json_schema"
    ]["schema"]
    assert set(administrator_schema["properties"]) == {
        "result",
        "target_slot",
        "after",
    }
    assert "OUTPUT_SCHEMA=" in administrator_client.calls[0]["payload"]["messages"][0][
        "content"
    ]


def test_atomic_service_rejects_superseded_local_gemma(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixed OpenRouter model route"):
        RPAtomicServiceProvider(
            _settings(tmp_path),
            provider="local",
            model="gemma-4-26b-a4b-it-rp-q4",
        )


def test_narrator_rejects_atomic_service_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="fixed OpenRouter model route"):
        RPNarratorProvider(
            _settings(tmp_path),
            provider="openrouter",
            model=RP_ATOMIC_MODEL,
        )


@pytest.mark.parametrize(
    ("operation", "content"),
    [
        ("relationships", '{"relationships":[],"events":[],"notes":[]}'),
        ("runtime_lore", '{"cards":[]}'),
        ("story_memory", '{"party_id":"party-one","memory_snapshot":{}}'),
        ("story_memory", "```json\n{}\n```"),
    ],
)
def test_live_wrong_envelopes_are_rejected_without_normalization(
    tmp_path: Path, operation: str, content: str
) -> None:
    client = RecordingClient(_completion(content))
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=client,  # type: ignore[arg-type]
    )

    async def invoke() -> object:
        if operation == "relationships":
            return await provider.extract_relationships(
                party=_party(), turn=_turn(), evidence_spans=_spans()
            )
        if operation == "runtime_lore":
            return await provider.extract_runtime_lore(
                party=_party(), turn=_turn(), evidence_spans=_spans()
            )
        return await provider.update_story_memory(
            party=_party(), turns=_memory_turns()
        )

    with pytest.raises(RPModelOutputRejected):
        asyncio.run(invoke())

    assert len(client.calls) == 1


@pytest.mark.parametrize(
    ("content", "finish_reason"),
    [
        ("not-json", "stop"),
        ('{"candidates":[],"unexpected":true}', "stop"),
        ('{"candidates":[]}', "length"),
    ],
)
def test_semantic_reject_is_terminal_model_output(
    tmp_path: Path, content: str, finish_reason: str
) -> None:
    client = RecordingClient(_completion(content, finish_reason=finish_reason))
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(RPModelOutputRejected):
        asyncio.run(
            provider.extract_relationships(
                party=_party(), turn=_turn(), evidence_spans=_spans()
            )
        )

    assert len(client.calls) == 1


def test_transport_error_remains_generic_and_retryable_by_runner(
    tmp_path: Path,
) -> None:
    request = httpx.Request("POST", "https://provider.invalid/chat/completions")
    transport_error = httpx.ConnectError("connection failed", request=request)
    client = RecordingClient(transport_error)
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(httpx.ConnectError) as failed:
        asyncio.run(
            provider.extract_relationships(
                party=_party(), turn=_turn(), evidence_spans=_spans()
            )
        )

    assert failed.value is transport_error
    assert not isinstance(failed.value, RPModelOutputRejected)
    assert len(client.calls) == 1


@pytest.mark.parametrize("error_location", ["choice", "top_level"])
def test_upstream_provider_error_remains_generic_and_retryable_by_runner(
    tmp_path: Path, error_location: str
) -> None:
    provider_error = {
        "code": 504,
        "message": "Upstream idle timeout exceeded",
    }
    if error_location == "choice":
        response = _completion('{"candidates":[', finish_reason="error")
        response["choices"][0]["error"] = provider_error
    else:
        response = {"id": "provider-error", "error": provider_error}
    client = RecordingClient(response)
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=client,  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="provider error 504") as failed:
        asyncio.run(
            provider.extract_relationships(
                party=_party(), turn=_turn(), evidence_spans=_spans()
            )
        )

    assert not isinstance(failed.value, RPModelOutputRejected)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "model",
    [
        "openrouter/auto",
        "openrouter/free",
        "nvidia/nemotron-3-super:free",
        "deepseek/deepseek-v4-flash",
    ],
)
def test_clean_rp_rejects_dynamic_and_nvidia_openrouter_routes(
    tmp_path: Path, model: str
) -> None:
    with pytest.raises(ValueError, match="retired or unsafe"):
        RPNarratorProvider(
            _settings(tmp_path),
            provider="openrouter",
            model=model,
        )


def test_provider_diagnostics_stay_in_shared_database(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    RPTurnEngine(settings.rp_sqlite_path)
    captured_authorization: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_authorization.append(request.headers["Authorization"])
        return httpx.Response(
            200,
            request=request,
            json=_completion("Точный ответ."),
        )

    narrator_client = ServiceModelClient(
        replace(
            settings,
            service_openrouter_api_key=settings.openrouter_api_key,
        ),
        transport=httpx.MockTransport(handler),
    )
    provider = RPNarratorProvider(
        settings,
        provider="openrouter",
        model=NARRATOR_MODEL,
        party_id="party-one",
        client=narrator_client,
    )
    prompt = RPNarratorPrompt(
        messages=(RPPromptMessage("user", "player", "Продолжай."),),
        raw_turn_versions=(),
        safe_memory_coverage=0,
        stable_prefix_hash="stable",
        input_chars=10,
    )

    assert asyncio.run(provider.complete(prompt)) == "Точный ответ."
    assert captured_authorization == ["Bearer party-narrator-key"]
    RPTurnEngine(settings.rp_sqlite_path)

    with sqlite3.connect(settings.rp_sqlite_path) as connection:
        clean_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    with sqlite3.connect(settings.sqlite_path) as connection:
        trace = connection.execute(
            "SELECT role, provider, model, status FROM service_call_log"
        ).fetchone()

    assert "service_call_log" not in clean_tables
    assert trace == (
        "rp_narrator",
        "openrouter",
        "provider-response-model",
        "completed",
    )


def test_player_operations_use_openrouter_structured_route_with_discriminated_schema(
    tmp_path: Path,
) -> None:
    client = RecordingClient(
        _completion(
            '{"result":"draft","kind":"event","title":"Событие",'
            '"content":"Свидетель подтвердил факт.","keywords":["свидетель"],'
            '"evidence_span_ids":[1]}'
        ),
        _completion(
            '{"result":"no_target","target_slot":null,"action":null,'
            '"after":null,"forbidden_claims":[]}'
        ),
    )
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model=RP_ATOMIC_MODEL,
        client=client,  # type: ignore[arg-type]
    )

    async def exercise() -> None:
        lore = await provider.draft_player_lore(
            party=_party(),
            turn=_turn(),
            kind="event",
            evidence_spans=_spans(),
        )
        correction = await provider.draft_player_correction(
            party=_party(),
            instruction="Исправь реакцию свидетеля.",
            raw_hint="raw:1",
            candidates=(
                {
                    "target_slot": "raw:1:" + "a" * 20,
                    "target_kind": "raw",
                    "before": "Свидетель кивает.",
                },
            ),
        )
        assert lore.result == "draft"
        assert correction.result == "no_target"

    asyncio.run(exercise())
    assert [call["role"] for call in client.calls] == [
        "rp_atomic_player_lore",
        "rp_atomic_player_correction",
    ]
    for call in client.calls:
        payload = call["payload"]
        assert payload["reasoning"] == {"enabled": False}
        schema = payload["response_format"]["json_schema"]["schema"]
        assert len(schema["oneOf"]) == 2
