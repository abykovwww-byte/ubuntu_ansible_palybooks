from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import RP_ANONYMOUS_OWNER, create_app
from app.rp.content import SUPPORTED_WORLD_ID
from app.rp.mechanics import (
    RPPlayerCorrectionResult,
    RPRelationshipResult,
    RPRuntimeLoreResult,
)
from app.rp.memory import (
    RP_MEMORY_SCHEMA_VERSION,
    RPAssetsAndRulesMemory,
    RPCharactersMemory,
    RPChronologyAndHooksMemory,
    RPSituationMemory,
    RPStoryMemorySnapshot,
    RPThreadsMemory,
)
from app.rp.provider import RPNarratorProvider


RP_STACK_ROOT = Path(__file__).resolve().parents[2]
WORLD_PACKS_ROOT = RP_STACK_ROOT / "worldpacks"


@pytest.fixture
def integration_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'shared.db'}",
        rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
        worldpacks_path=str(WORLD_PACKS_ROOT),
        auth_enabled=False,
        rp_narrator_enabled=True,
        rp_atomic_service_enabled=False,
        rp_administrator_enabled=False,
        rp_derived_wait_seconds=2.0,
        rp_runner_poll_interval_seconds=0.001,
        openrouter_api_key="party-openrouter-key",
        service_openrouter_api_key="service-openrouter-key",
        local_llm_enabled=False,
    )


@pytest.fixture
def integration_client(
    integration_settings: Settings,
) -> Iterator[tuple[TestClient, Any]]:
    app = create_app(integration_settings)
    with TestClient(app) as client:
        yield client, app


def _model_profile_id(client: TestClient) -> str:
    response = client.get("/api/model-profiles")
    assert response.status_code == 200, response.text
    profile = next(
        item
        for item in response.json()["model_profiles"]
        if item["provider"] == "openrouter"
        and item["model"] == "openai/gpt-5.6-luna-pro"
    )
    return str(profile["id"])


def _preset_create_payload(client: TestClient, *, title: str) -> dict[str, Any]:
    worldpacks = client.get("/api/worldpacks")
    assert worldpacks.status_code == 200, worldpacks.text
    packs = worldpacks.json()["worldpacks"]
    assert [item["id"] for item in packs] == [SUPPORTED_WORLD_ID]
    preset_id = packs[0]["scenario_presets"][0]["id"]
    return {
        "title": title,
        "world_id": SUPPORTED_WORLD_ID,
        "scenario": {"source": "preset", "preset_id": preset_id},
        "model_profile_id": _model_profile_id(client),
    }


def _free_create_payload(client: TestClient, *, title: str) -> dict[str, Any]:
    response = client.get(f"/api/worldpacks/{SUPPORTED_WORLD_ID}")
    assert response.status_code == 200, response.text
    seed = dict(response.json()["worldpack"]["free_scenario_seed"])
    return {
        "title": title,
        "world_id": SUPPORTED_WORLD_ID,
        "scenario": seed,
        "model_profile_id": _model_profile_id(client),
    }


def _create_party(client: TestClient, payload: dict[str, Any]) -> dict[str, Any]:
    response = client.post("/api/parties", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["party"]


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return {
            *(str(key) for key in value),
            *(key for item in value.values() for key in _all_keys(item)),
        }
    if isinstance(value, list):
        return {key for item in value for key in _all_keys(item)}
    return set()


class _PlayerOperationModel:
    def __init__(self) -> None:
        self.player_lore_calls = 0
        self.player_correction_calls = 0

    async def extract_relationships(self, **_: Any) -> RPRelationshipResult:
        return RPRelationshipResult(candidates=())

    async def extract_runtime_lore(self, **_: Any) -> RPRuntimeLoreResult:
        return RPRuntimeLoreResult(
            result="no_candidate",
            kind="event",
            title=None,
            content=None,
            keywords=None,
            evidence_span_ids=None,
        )

    async def update_story_memory(
        self, *, turns: tuple[Any, ...], **_: Any
    ) -> RPStoryMemorySnapshot:
        coverage = int(turns[-1].committed_version)
        return RPStoryMemorySnapshot(
            schema_version=RP_MEMORY_SCHEMA_VERSION,
            observed_through_version=coverage,
            situation=RPSituationMemory(coverage=coverage, status="fresh"),
            threads=RPThreadsMemory(coverage=coverage, status="fresh"),
            characters=RPCharactersMemory(coverage=coverage, status="fresh"),
            assets_and_rules=RPAssetsAndRulesMemory(
                coverage=coverage, status="fresh"
            ),
            chronology_and_hooks=RPChronologyAndHooksMemory(
                coverage=coverage, status="fresh"
            ),
        )

    async def draft_player_lore(
        self, *, kind: str, **_: Any
    ) -> RPRuntimeLoreResult:
        self.player_lore_calls += 1
        return RPRuntimeLoreResult(
            result="draft",
            kind=kind,
            title="Новая договорённость",
            content="Свидетель подтвердил новую договорённость.",
            keywords=("договорённость", "свидетель"),
            evidence_span_ids=(1,),
        )

    async def draft_player_correction(
        self, *, candidates: tuple[dict[str, Any], ...], **_: Any
    ) -> RPPlayerCorrectionResult:
        self.player_correction_calls += 1
        target = next(item for item in candidates if item["target_kind"] == "raw")
        return RPPlayerCorrectionResult(
            result="draft",
            target_slot=target["target_slot"],
            action="replace",
            after="Свидетель не кивнул, а покачал головой.",
            forbidden_claims=(),
        )


def test_world_detail_seed_creates_free_party_without_filesystem_helper(
    integration_client: tuple[TestClient, Any],
) -> None:
    client, _app = integration_client

    detail = client.get(f"/api/worldpacks/{SUPPORTED_WORLD_ID}")
    assert detail.status_code == 200, detail.text
    seed = detail.json()["worldpack"]["free_scenario_seed"]
    assert seed["source"] == "free"
    assert {
        "source",
        "scenario_id",
        "title",
        "player_role",
        "style",
        "format",
        "difficulty",
        "detail_level",
        "narrator_system",
        "narrator_note",
        "opening",
        "initial_state",
        "active_character_ids",
        "local_overrides",
    }.issubset(seed)

    party = _create_party(
        client,
        _free_create_payload(client, title="Free party from public API seed"),
    )
    assert party["world_id"] == SUPPORTED_WORLD_ID
    assert party["scenario_source"] == "free"


def test_rebuilt_create_list_get_are_clean_and_owner_scoped(
    integration_client: tuple[TestClient, Any],
) -> None:
    client, app = integration_client
    preset_party = _create_party(
        client, _preset_create_payload(client, title="Preset party")
    )
    free_party = _create_party(
        client, _free_create_payload(client, title="Free party")
    )

    assert preset_party["scenario_source"] == "preset"
    assert free_party["scenario_source"] == "free"
    assert preset_party["world_id"] == free_party["world_id"] == SUPPORTED_WORLD_ID

    listed = client.get("/api/parties")
    assert listed.status_code == 200, listed.text
    assert {item["id"] for item in listed.json()["parties"]} == {
        preset_party["id"],
        free_party["id"],
    }
    fetched = client.get(f"/api/parties/{preset_party['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["party"] == preset_party

    seed = app.state.rp_engine.get_party(
        owner_user_id=RP_ANONYMOUS_OWNER,
        party_id=preset_party["id"],
    )
    app.state.rp_engine.create_party(
        owner_user_id="different-owner",
        party_id="party-other-owner",
        title="Other owner",
        world_snapshot=seed.world_snapshot,
        scenario_snapshot=seed.scenario_snapshot,
        narrator_profile_id=seed.narrator_profile_id,
        narrator_provider=seed.narrator_provider,
        narrator_model=seed.narrator_model,
        narrator_settings=seed.narrator_settings,
    )

    owner_list = client.get("/api/parties").json()["parties"]
    assert {item["id"] for item in owner_list} == {
        preset_party["id"],
        free_party["id"],
    }
    assert client.get("/api/parties/party-other-owner").status_code == 404


def test_rebuilt_turn_http_contract_is_idempotent_retryable_and_fail_open(
    integration_client: tuple[TestClient, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = integration_client
    provider_calls: list[Any] = []
    provider_state = {"fail_next": False}

    async def provider_complete(
        _provider: RPNarratorProvider, prompt: Any
    ) -> str:
        provider_calls.append(prompt)
        if provider_state["fail_next"]:
            provider_state["fail_next"] = False
            raise RuntimeError("provider transport failed")
        return f"Ответ нарратора {len(provider_calls)}."

    monkeypatch.setattr(RPNarratorProvider, "complete", provider_complete)
    party = _create_party(
        client, _preset_create_payload(client, title="HTTP contract party")
    )
    party_id = party["id"]

    unknown_start = client.post(
        f"/api/parties/{party_id}/start",
        json={"expected_version": 0},
    )
    assert unknown_start.status_code == 422, unknown_start.text

    start_headers = {"X-Request-ID": "request-start"}
    start_payload = {"idempotency_key": "start-key"}
    started = client.post(
        f"/api/parties/{party_id}/start",
        json=start_payload,
        headers=start_headers,
    )
    assert started.status_code == 200, started.text
    assert set(started.json()) == {
        "party_id",
        "started",
        "already_started",
        "state_version",
        "message",
        "turn",
    }
    assert started.json()["state_version"] == 1
    assert "raw" not in _all_keys(started.json())

    duplicate_start = client.post(
        f"/api/parties/{party_id}/start",
        json=start_payload,
        headers=start_headers,
    )
    assert duplicate_start.status_code == 200, duplicate_start.text
    assert duplicate_start.json()["turn"] == started.json()["turn"]
    assert duplicate_start.json()["started"] is False
    assert len(provider_calls) == 1

    missing_message_key = client.post(
        f"/api/parties/{party_id}/messages",
        json={"content": "Без стабильного ключа.", "expected_version": 1},
    )
    assert missing_message_key.status_code == 422, missing_message_key.text
    assert len(provider_calls) == 1

    message_payload = {
        "content": "Я продолжаю сцену.",
        "idempotency_key": "message-key",
        "expected_version": 1,
    }
    before = time.perf_counter()
    message = client.post(
        f"/api/parties/{party_id}/messages",
        json=message_payload,
        headers={"X-Request-ID": "request-message"},
    )
    elapsed = time.perf_counter() - before
    assert message.status_code == 200, message.text
    assert elapsed < 1.0
    assert set(message.json()) == {
        "party_id",
        "state_version",
        "message",
        "turn",
    }
    assert message.json()["state_version"] == 2
    assert "raw" not in _all_keys(message.json())

    duplicate_message = client.post(
        f"/api/parties/{party_id}/messages",
        json=message_payload,
        headers={"X-Request-ID": "request-message"},
    )
    assert duplicate_message.status_code == 200, duplicate_message.text
    assert duplicate_message.json()["turn"] == message.json()["turn"]
    assert len(provider_calls) == 2
    history = client.get(f"/api/parties/{party_id}/history").json()["turns"]
    assert len(history) == 2

    stale = client.post(
        f"/api/parties/{party_id}/messages",
        json={
            "content": "Устаревшее действие.",
            "idempotency_key": "stale-key",
            "expected_version": 1,
        },
        headers={"X-Request-ID": "request-stale"},
    )
    assert stale.status_code == 409, stale.text
    assert len(provider_calls) == 2

    failed_player_text = "Текст должен остаться доступен для retry."
    failed_payload = {
        "content": failed_player_text,
        "idempotency_key": "failed-key",
        "expected_version": 2,
    }
    provider_state["fail_next"] = True
    failed = client.post(
        f"/api/parties/{party_id}/messages",
        json=failed_payload,
        headers={"X-Request-ID": "request-failed"},
    )
    assert failed.status_code == 502, failed.text
    assert failed.json()["detail"]["retryable"] is True
    assert failed.json()["detail"]["player_text"] == failed_player_text
    assert len(client.get(f"/api/parties/{party_id}/history").json()["turns"]) == 2

    request_status = client.get(
        f"/api/parties/{party_id}/requests/request-failed"
    )
    assert request_status.status_code == 200, request_status.text
    assert request_status.json()["status"] == "failed"
    assert request_status.json()["request"]["player_text"] == failed_player_text
    assert "claim_token" not in _all_keys(request_status.json())

    retry = client.post(
        f"/api/parties/{party_id}/messages",
        json=failed_payload,
        headers={"X-Request-ID": "request-failed"},
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["state_version"] == 3
    assert retry.json()["turn"]["player_text"] == failed_player_text
    assert len(provider_calls) == 4
    assert len(client.get(f"/api/parties/{party_id}/history").json()["turns"]) == 3

    jobs_response = client.get(f"/api/parties/{party_id}/service-jobs")
    assert jobs_response.status_code == 200, jobs_response.text
    jobs = jobs_response.json()["jobs"]
    assert jobs
    assert all(job["status"] == "pending" for job in jobs)
    assert all(job["attempts"] == 0 for job in jobs)
    assert "claim_token" not in _all_keys(jobs_response.json())

    supervisor = client.get(f"/api/parties/{party_id}/supervisor")
    assert supervisor.status_code == 200, supervisor.text
    assert supervisor.json()["roles"]["atomic_service"]["enabled"] is False
    assert supervisor.json()["roles"]["atomic_service"]["kill_switch"] is True
    assert supervisor.json()["roles"]["atomic_service"]["status"] == "pending"
    assert {"claim_token", "raw", "raw_response"}.isdisjoint(
        _all_keys(supervisor.json())
    )

    legacy_mutation = client.post(
        f"/api/parties/{party_id}/checks",
        json={"skill": 1, "difficulty": 10},
    )
    assert legacy_mutation.status_code == 404, legacy_mutation.text


def test_clean_player_lore_and_correction_are_confirmed_owner_operations(
    integration_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        integration_settings,
        rp_atomic_service_enabled=True,
        local_llm_enabled=True,
    )
    app = create_app(settings)
    model = _PlayerOperationModel()
    assert app.state.rp_runner is not None
    app.state.rp_runner.service_handler.model = model
    narrator_prompts: list[Any] = []

    async def narrator_complete(
        _provider: RPNarratorProvider, prompt: Any
    ) -> str:
        narrator_prompts.append(prompt)
        return "Свидетель кивает и подтверждает договорённость."

    monkeypatch.setattr(RPNarratorProvider, "complete", narrator_complete)
    with TestClient(app) as client:
        payload = _free_create_payload(client, title="Player operations")
        payload["scenario"]["local_overrides"] = {
            "lore_cards": [
                {
                    "key": "scenario:sealed-room",
                    "title": "Опечатанная комната",
                    "keywords": ["комната"],
                    "content": "Комната опечатана до полуночи.",
                    "always_on": False,
                    "enabled": True,
                }
            ]
        }
        party = _create_party(client, payload)
        party_id = party["id"]
        opening = client.post(
            f"/api/parties/{party_id}/start",
            json={"idempotency_key": "player-ops-opening"},
        )
        assert opening.status_code == 200, opening.text
        turn_id = opening.json()["turn"]["id"]

        before = client.get(f"/api/parties/{party_id}/lore-cards").json()["cards"]
        assert {card["origin"] for card in before} == {"world", "scenario"}
        lore_request = {
            "source_turn_ids": [turn_id],
            "kind": "event",
            "expected_version": 1,
            "idempotency_key": "lore-draft-one",
        }
        draft = client.post(
            f"/api/parties/{party_id}/lore-cards/draft", json=lore_request
        )
        repeated_draft = client.post(
            f"/api/parties/{party_id}/lore-cards/draft", json=lore_request
        )
        assert draft.status_code == repeated_draft.status_code == 200
        assert draft.json() == repeated_draft.json()
        assert draft.json()["result"] == "draft"
        assert model.player_lore_calls == 1

        draft_body = draft.json()
        confirm_body = {
            "kind": draft_body["kind"],
            "title": draft_body["title"],
            "content": draft_body["content"] + " Игрок проверил формулировку.",
            "keywords": draft_body["keywords"],
            "always_on": False,
            "enabled": True,
            "source_turn_ids": draft_body["source_turn_ids"],
            "draft_job_id": draft_body["job_id"],
            "expected_version": 1,
            "idempotency_key": "lore-confirm-one",
        }
        confirmed = client.post(
            f"/api/parties/{party_id}/lore-cards", json=confirm_body
        )
        repeated_confirm = client.post(
            f"/api/parties/{party_id}/lore-cards", json=confirm_body
        )
        assert confirmed.status_code == repeated_confirm.status_code == 200
        assert confirmed.json() == repeated_confirm.json()
        assert confirmed.json()["card"]["authoring_kind"] == "event"
        assert confirmed.json()["card"]["content"].endswith(
            "Игрок проверил формулировку."
        )
        conflicting_confirm = client.post(
            f"/api/parties/{party_id}/lore-cards",
            json={**confirm_body, "title": "Другой текст с тем же ключом"},
        )
        assert conflicting_confirm.status_code == 409
        cards = client.get(f"/api/parties/{party_id}/lore-cards").json()["cards"]
        assert {card["origin"] for card in cards} == {
            "world",
            "scenario",
            "runtime",
        }

        correction_request = {
            "instruction": "Исправь реакцию свидетеля: он покачал головой.",
            "raw_hint": f"raw:{turn_id}",
            "expected_version": 1,
            "idempotency_key": "correction-draft-one",
        }
        correction = client.post(
            f"/api/parties/{party_id}/player-corrections/draft",
            json=correction_request,
        )
        repeated_correction = client.post(
            f"/api/parties/{party_id}/player-corrections/draft",
            json=correction_request,
        )
        assert correction.status_code == repeated_correction.status_code == 200
        assert correction.json() == repeated_correction.json()
        assert model.player_correction_calls == 1
        proposal = correction.json()["proposal"]
        assert proposal["status"] == "pending"
        assert app.state.rp_engine.get_party(
            owner_user_id=RP_ANONYMOUS_OWNER, party_id=party_id
        ).current_version == 1
        assert app.state.rp_engine.derived_context(
            owner_user_id=RP_ANONYMOUS_OWNER, party_id=party_id
        ).player_correction_overlay is None

        decision_body = {
            "decision": "accept",
            "expected_version": 1,
            "idempotency_key": "correction-accept-one",
        }
        accepted = client.post(
            f"/api/parties/{party_id}/player-corrections/{proposal['id']}/decision",
            json=decision_body,
        )
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["overlay"]["applies_to_version"] == 2
        assert app.state.rp_engine.get_party(
            owner_user_id=RP_ANONYMOUS_OWNER, party_id=party_id
        ).current_version == 1

        next_turn = client.post(
            f"/api/parties/{party_id}/messages",
            json={
                "content": "Я продолжаю разговор.",
                "idempotency_key": "player-ops-turn-two",
                "expected_version": 1,
            },
        )
        assert next_turn.status_code == 200, next_turn.text
        overlay_messages = [
            item
            for item in narrator_prompts[-1].messages
            if item.block_id == "player_correction_overlay"
        ]
        assert len(overlay_messages) == 1
        assert "покачал головой" in overlay_messages[0].content
        lore_message = next(
            item for item in narrator_prompts[-1].messages if item.block_id == "lore"
        )
        assert '"scenario"' in lore_message.content
        assert '"origin":"scenario"' in lore_message.content
        assert '"origin":"runtime"' in lore_message.content
        assert app.state.rp_engine.derived_context(
            owner_user_id=RP_ANONYMOUS_OWNER, party_id=party_id
        ).player_correction_overlay is None

        repeated_decision = client.post(
            f"/api/parties/{party_id}/player-corrections/{proposal['id']}/decision",
            json=decision_body,
        )
        assert repeated_decision.status_code == 200
        conflicting_decision = client.post(
            f"/api/parties/{party_id}/player-corrections/{proposal['id']}/decision",
            json={**decision_body, "expected_version": 2},
        )
        assert conflicting_decision.status_code == 409

        rejected_draft = client.post(
            f"/api/parties/{party_id}/player-corrections/draft",
            json={
                "instruction": "Исправь повторную реакцию свидетеля.",
                "expected_version": 2,
                "idempotency_key": "correction-draft-reject",
            },
        )
        assert rejected_draft.status_code == 200, rejected_draft.text
        rejected_id = rejected_draft.json()["proposal"]["id"]
        rejected = client.post(
            f"/api/parties/{party_id}/player-corrections/{rejected_id}/decision",
            json={
                "decision": "reject",
                "expected_version": 2,
                "idempotency_key": "correction-reject-two",
            },
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["overlay"] is None
        turn_three = client.post(
            f"/api/parties/{party_id}/messages",
            json={
                "content": "Я задаю следующий вопрос.",
                "idempotency_key": "player-ops-turn-three",
                "expected_version": 2,
            },
        )
        assert turn_three.status_code == 200, turn_three.text
        assert not any(
            item.block_id == "player_correction_overlay"
            for item in narrator_prompts[-1].messages
        )

        stale_draft = client.post(
            f"/api/parties/{party_id}/player-corrections/draft",
            json={
                "instruction": "Исправь последнюю реакцию свидетеля.",
                "expected_version": 3,
                "idempotency_key": "correction-draft-stale",
            },
        )
        assert stale_draft.status_code == 200, stale_draft.text
        stale_id = stale_draft.json()["proposal"]["id"]
        turn_four = client.post(
            f"/api/parties/{party_id}/messages",
            json={
                "content": "Я меняю сцену до решения по черновику.",
                "idempotency_key": "player-ops-turn-four",
                "expected_version": 3,
            },
        )
        assert turn_four.status_code == 200, turn_four.text
        stale = client.post(
            f"/api/parties/{party_id}/player-corrections/{stale_id}/decision",
            json={
                "decision": "accept",
                "expected_version": 3,
                "idempotency_key": "correction-stale-three",
            },
        )
        assert stale.status_code == 409, stale.text
        proposals = client.get(
            f"/api/parties/{party_id}/player-corrections"
        ).json()["proposals"]
        assert next(item for item in proposals if item["id"] == stale_id)["status"] == "stale"

        current = app.state.rp_engine.get_party(
            owner_user_id=RP_ANONYMOUS_OWNER, party_id=party_id
        )
        other_owner = app.state.rp_engine.create_party(
            owner_user_id="other-owner",
            party_id="party-other-player-ops",
            title="Other owner operations",
            world_snapshot=current.world_snapshot,
            scenario_snapshot=current.scenario_snapshot,
        )
        assert other_owner.owner_user_id == "other-owner"
        assert (
            client.get(
                "/api/parties/party-other-player-ops/player-corrections"
            ).status_code
            == 404
        )
