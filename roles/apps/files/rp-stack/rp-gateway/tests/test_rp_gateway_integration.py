from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import RP_ANONYMOUS_OWNER, create_app
from app.rp.content import SUPPORTED_WORLD_ID
from app.rp.provider import RPNarratorProvider


RP_STACK_ROOT = Path(__file__).resolve().parents[2]
WORLD_PACKS_ROOT = RP_STACK_ROOT / "worldpacks"
STATE_SCHEMA = RP_STACK_ROOT / "state" / "schema.json"


@pytest.fixture
def integration_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'legacy.db'}",
        rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
        world_state_path=str(tmp_path / "legacy-state.json"),
        party_state_root=str(tmp_path / "legacy-parties"),
        state_schema_path=str(STATE_SCHEMA),
        worldpacks_path=str(WORLD_PACKS_ROOT),
        auth_enabled=False,
        rp_rebuild_enabled=True,
        rp_narrator_enabled=True,
        rp_atomic_service_enabled=False,
        rp_administrator_enabled=False,
        rp_derived_wait_seconds=2.0,
        rp_runner_poll_interval_seconds=0.001,
        openrouter_api_key="party-openrouter-key",
        service_openrouter_api_key="service-openrouter-key",
        openrouter_models=("openrouter/auto",),
        openrouter_model_catalog_live=False,
        gemini_model_catalog_live=False,
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
        and item["model"] == "openrouter/auto"
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


def test_hidden_model_profile_cannot_be_selected_by_direct_clean_party_post(
    integration_client: tuple[TestClient, Any],
) -> None:
    client, app = integration_client
    hidden_profile_id = "hidden-openrouter-batch"
    app.state.party_store.upsert_model_profile(
        {
            "id": hidden_profile_id,
            "title": "Hidden batch profile",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "example/hidden-roleplay-model:batch",
            "params": {
                "context_tokens": 131_072,
                "rp_specialized": True,
                "source": "test",
            },
            "api_key_source": "server_env_or_managed_key",
        }
    )
    listed_ids = {
        item["id"]
        for item in client.get("/api/model-profiles").json()["model_profiles"]
    }
    assert hidden_profile_id not in listed_ids

    payload = _preset_create_payload(client, title="Hidden profile must fail")
    payload["model_profile_id"] = hidden_profile_id
    response = client.post("/api/parties", json=payload)

    assert response.status_code == 400, response.text
    assert "profile" in response.json()["detail"].lower()


def test_custom_base_byok_uses_immutable_exact_endpoint_and_secret_or_fails_closed(
    integration_client: tuple[TestClient, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, app = integration_client
    custom_profile_id = "custom-base-roleplay"
    custom_base_url = "https://rp-provider.example.test/v1"
    app.state.party_store.upsert_model_profile(
        {
            "id": custom_profile_id,
            "title": "Custom-base RP model",
            "provider": "openrouter",
            "base_url": custom_base_url,
            "model": "example/custom-roleplay-model",
            "params": {
                "context_tokens": 131_072,
                "rp_specialized": True,
                "source": "test",
            },
            "api_key_source": "managed_key",
        }
    )
    payload = _preset_create_payload(client, title="Custom-base BYOK party")
    payload["model_profile_id"] = custom_profile_id
    party = _create_party(client, payload)
    party_id = party["id"]

    stored_party = app.state.rp_engine.get_party(
        owner_user_id=RP_ANONYMOUS_OWNER,
        party_id=party_id,
    )
    assert stored_party.narrator_base_url == custom_base_url

    provider_routes: list[tuple[str, str]] = []

    async def complete(
        provider: RPNarratorProvider, _prompt: Any
    ) -> str:
        provider_routes.append(
            (
                provider.client.settings.openrouter_api_base,
                provider.client.settings.service_openrouter_api_key,
            )
        )
        return "Нарратор отвечает через точный пользовательский маршрут."

    monkeypatch.setattr(RPNarratorProvider, "complete", complete)
    no_exact_secret = client.post(
        f"/api/parties/{party_id}/start",
        json={"idempotency_key": "custom-base-no-exact-secret"},
    )
    assert no_exact_secret.status_code == 400, no_exact_secret.text
    assert provider_routes == []

    wrong_key = client.post(
        f"/api/parties/{party_id}/byok",
        json={
            "label": "Wrong endpoint key",
            "api_key": "wrong-endpoint-secret",
            "provider": "openrouter",
            "base_url": "https://other-provider.example.test/v1",
            "is_default": True,
        },
    )
    assert wrong_key.status_code == 400, wrong_key.text
    assert "binding" in wrong_key.json()["detail"].lower()

    exact_key = client.post(
        f"/api/parties/{party_id}/byok",
        json={
            "label": "Exact endpoint key",
            "api_key": "exact-custom-base-secret",
            "provider": "openrouter",
            "base_url": custom_base_url,
            "is_default": True,
        },
    )
    assert exact_key.status_code == 200, exact_key.text
    started = client.post(
        f"/api/parties/{party_id}/start",
        json={"idempotency_key": "custom-base-exact-secret"},
    )

    assert started.status_code == 200, started.text
    assert provider_routes == [(custom_base_url, "exact-custom-base-secret")]
    assert (
        app.state.rp_engine.get_party(
            owner_user_id=RP_ANONYMOUS_OWNER,
            party_id=party_id,
        ).narrator_base_url
        == custom_base_url
    )


def test_rebuilt_create_list_get_are_clean_and_owner_scoped(
    integration_client: tuple[TestClient, Any],
    integration_settings: Settings,
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

    with sqlite3.connect(integration_settings.rp_sqlite_path) as connection:
        stored = connection.execute(
            "SELECT id, owner_user_id FROM rp_parties ORDER BY id"
        ).fetchall()
    with sqlite3.connect(integration_settings.sqlite_path) as connection:
        legacy_party_count = connection.execute(
            "SELECT count(*) FROM parties"
        ).fetchone()[0]

    assert {row[0] for row in stored} == {preset_party["id"], free_party["id"]}
    assert {row[1] for row in stored} == {RP_ANONYMOUS_OWNER}
    assert legacy_party_count == 0
    legacy_party_root = Path(integration_settings.party_state_root)
    assert not legacy_party_root.exists() or list(legacy_party_root.iterdir()) == []

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
    integration_settings: Settings,
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

    previous_handler = app.state.rp_runner.service_handler
    selected_service_model = client.patch(
        "/api/admin/global-settings/service-model",
        json={"choice_id": "or-qwen-3.7-flash"},
    )
    assert selected_service_model.status_code == 200, selected_service_model.text
    assert selected_service_model.json()["selected"]["model"] == "qwen/qwen3.7-flash"
    assert app.state.rp_runner.service_handler is not previous_handler
    assert (
        app.state.rp_runner.service_handler.model.model == "qwen/qwen3.7-flash"
    )

    unknown_start = client.post(
        f"/api/parties/{party_id}/start",
        json={"expected_version": 0},
    )
    assert unknown_start.status_code == 422, unknown_start.text

    with sqlite3.connect(integration_settings.sqlite_path) as connection:
        connection.execute(
            "UPDATE model_profiles SET provider = 'nvidia' WHERE id = ?",
            (party["model_profile_id"],),
        )
        connection.commit()
    retired = client.post(
        f"/api/parties/{party_id}/start",
        json={"idempotency_key": "retired-binding"},
    )
    assert retired.status_code == 409, retired.text
    assert provider_calls == []
    with sqlite3.connect(integration_settings.sqlite_path) as connection:
        connection.execute(
            "UPDATE model_profiles SET provider = 'openrouter' WHERE id = ?",
            (party["model_profile_id"],),
        )
        connection.commit()

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
    assert legacy_mutation.status_code == 410, legacy_mutation.text
