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
    RPAdministratorProvider,
    RPAtomicServiceProvider,
    RPNarratorProvider,
)
from app.rp.turn_engine import RPModelOutputRejected, RPParty, RPTurn, RPTurnEngine
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
                "id": "treaty",
                "kind": "event",
                "title": "Договор",
                "content": "Договор ограничивает обе стороны.",
                "keywords": ["договор"],
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
        narrator_profile_id="openrouter-narrator",
        narrator_provider="openrouter",
        narrator_base_url="https://openrouter.ai/api/v1",
        narrator_model="narrator/model",
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
        model="deepseek/deepseek-v4-flash",
        narrator_settings={
            "reasoning_effort": "xhigh",
            "temperature": 0.7,
            "top_p": 0.9,
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
    assert call["model"] == "deepseek/deepseek-v4-flash"
    assert call["payload"]["messages"] == [
        {"role": "system", "content": "WORLD EXACT"},
        {"role": "assistant", "content": "RAW EXACT"},
        {"role": "user", "content": "PLAYER EXACT"},
    ]
    assert call["payload"]["provider"] == {
        "sort": "throughput",
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
        model="narrator/model",
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
    memory = {
        "schema_version": "rp-story-memory.v1",
        "observed_through_version": 1,
        "situation": {"coverage": 1, "status": "fresh", "current_situation": None, "canon": []},
        "threads": {"coverage": 1, "status": "fresh", "active_threads": [], "resolved_threads": []},
        "characters": {"coverage": 1, "status": "fresh", "characters": []},
        "assets_and_rules": {
            "coverage": 1,
            "status": "fresh",
            "inventory_and_assets": [],
            "rules_and_abilities": [],
        },
        "chronology_and_hooks": {
            "coverage": 1,
            "status": "fresh",
            "chronology": [],
            "unresolved_hooks": [],
        },
    }
    atomic_client = RecordingClient(
        _completion('{"candidates":[]}'),
        _completion(
            '{"result":"no_candidate","kind":"event","title":null,'
            '"content":null,"keywords":null,"evidence_span_ids":null}'
        ),
        _completion(json.dumps(memory, ensure_ascii=False)),
    )
    administrator_client = RecordingClient(
        _completion(
            '{"result":"no_proposal","target_slot":null,"after":null}'
        )
    )
    atomic = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model="qwen/qwen3.5-flash-02-23",
        client=atomic_client,  # type: ignore[arg-type]
    )
    administrator = RPAdministratorProvider(
        _settings(tmp_path),
        provider="local",
        model="administrator/model",
        client=administrator_client,  # type: ignore[arg-type]
    )
    party = _party()
    turn = _turn()
    spans = _spans()

    async def exercise() -> None:
        relationships = await atomic.extract_relationships(
            party=party, turn=turn, evidence_spans=spans
        )
        lore = await atomic.extract_runtime_lore(
            party=party, turn=turn, evidence_spans=spans
        )
        story_memory = await atomic.update_story_memory(
            party=party, turns=(turn,), previous=None
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
        assert story_memory.safe_coverage == 1
        assert review.result == "no_proposal"

    asyncio.run(exercise())

    assert [call["model"] for call in atomic_client.calls] == [
        "qwen/qwen3.5-flash-02-23",
        "qwen/qwen3.5-flash-02-23",
        "qwen/qwen3.5-flash-02-23",
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
    assert all(
        call["payload"]["provider"] == {"require_parameters": True}
        for call in atomic_client.calls
    )
    expected_roots = (
        {"candidates"},
        {"result", "kind", "title", "content", "keywords", "evidence_span_ids"},
        {
            "schema_version",
            "observed_through_version",
            "situation",
            "threads",
            "characters",
            "assets_and_rules",
            "chronology_and_hooks",
        },
    )
    for call, expected_root in zip(atomic_client.calls, expected_roots, strict=True):
        response_format = call["payload"]["response_format"]
        assert response_format["type"] == "json_schema"
        assert response_format["json_schema"]["strict"] is True
        schema = response_format["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == expected_root
        assert "OUTPUT_SCHEMA=" in call["payload"]["messages"][0]["content"]
    assert "max_tokens" not in atomic_client.calls[0]["payload"]
    assert atomic_client.calls[1]["payload"]["max_tokens"] == 400
    assert "max_tokens" not in atomic_client.calls[2]["payload"]
    assert len(administrator_client.calls) == 1
    assert administrator_client.calls[0]["provider"] == "local"
    assert administrator_client.calls[0]["model"] == "administrator/model"
    assert administrator_client.calls[0]["role"] == "rp_administrator"
    assert administrator_client.calls[0]["payload"]["reasoning"] == {
        "enabled": False
    }
    assert "provider" not in administrator_client.calls[0]["payload"]
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


def test_non_reasoning_openrouter_route_requires_schema_without_reasoning(
    tmp_path: Path,
) -> None:
    client = RecordingClient(_completion('{"candidates":[]}'))
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model="qwen/qwen3-30b-a3b-instruct-2507",
        client=client,  # type: ignore[arg-type]
    )

    asyncio.run(
        provider.extract_relationships(
            party=_party(), turn=_turn(), evidence_spans=_spans()
        )
    )

    payload = client.calls[0]["payload"]
    assert payload["provider"] == {"require_parameters": True}
    assert payload["response_format"]["type"] == "json_schema"
    assert "reasoning" not in payload


@pytest.mark.parametrize(
    ("operation", "content"),
    [
        ("relationships", '{"relationships":[],"events":[],"notes":[]}'),
        ("runtime_lore", '{"cards":[]}'),
        ("story_memory", '{"party_id":"party-one","memory_snapshot":{}}'),
    ],
)
def test_live_wrong_envelopes_are_rejected_without_normalization(
    tmp_path: Path, operation: str, content: str
) -> None:
    client = RecordingClient(_completion(content))
    provider = RPAtomicServiceProvider(
        _settings(tmp_path),
        provider="openrouter",
        model="atomic/model",
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
            party=_party(), turns=(_turn(),), previous=None
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
        model="atomic/model",
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
        model="atomic/model",
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


def test_provider_diagnostics_stay_in_legacy_database(tmp_path: Path) -> None:
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
        model="narrator/model",
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
