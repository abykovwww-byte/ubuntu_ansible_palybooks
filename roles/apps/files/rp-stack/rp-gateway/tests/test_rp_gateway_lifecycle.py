from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.turn_engine import RPTurnEngine


OWNER_ID = "owner-one"
PARTY_ID = "party-one"


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=f"sqlite:///{tmp_path / 'rp_gateway.db'}",
        rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
        worldpacks_path=str(tmp_path / "worldpacks"),
        auth_enabled=False,
        local_llm_enabled=False,
        rp_narrator_enabled=False,
        rp_atomic_service_enabled=False,
        rp_administrator_enabled=False,
        rp_runner_poll_interval_seconds=0.001,
    )


def _party_source() -> dict[str, WorldSnapshot | ScenarioSnapshot]:
    world = WorldSnapshot(
        schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
        world_id="day-watch-moscow-v2",
        title="Дневной Дозор",
        language="ru",
        premise="Москва после Великого договора.",
        canon=("Канон мира.",),
        setting_rules="Законы мира.",
        characters="npc-one: Базовый NPC.",
        relationship_ontology={"axes": ["trust"]},
        seed_lore_cards=({"cards": [{"id": "world-card"}]},),
    )
    scenario = ScenarioSnapshot(
        schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
        scenario_id="test-scenario",
        title="Тестовый сценарий",
        world_id=world.world_id,
        source="preset",
        player_role="Новый сотрудник.",
        style="book",
        format="plain_scene_text",
        difficulty=None,
        detail_level="default",
        narrator_system="Веди сцену.",
        narrator_note="Сохраняй агентность игрока.",
        opening="Начинается смена.",
        initial_state={
            "player": {},
            "characters": {"npc-one": {}},
            "factions": {},
            "locations": {},
            "relationships": {},
        },
        active_character_ids=("npc-one",),
        starting_relationships={},
    )
    return {"world_snapshot": world, "scenario_snapshot": scenario}


def test_lifespan_owns_runner_and_recovery_does_not_spend_attempts(
    tmp_path: Path,
) -> None:
    app = create_app(_settings(tmp_path))
    engine = app.state.rp_engine
    runner = app.state.rp_runner
    assert engine is not None
    assert runner is not None
    engine.create_party(
        owner_user_id=OWNER_ID,
        party_id=PARTY_ID,
        **_party_source(),
    )
    engine.commit_turn(
        owner_user_id=OWNER_ID,
        party_id=PARTY_ID,
        request_id="request-one",
        idempotency_key="key-one",
        expected_version=0,
        player_text="Я жду.",
        narrator_text="Время идёт.",
    )
    claimed_service = engine.claim_service_job()
    claimed_administrator = engine.claim_administrator_job()
    assert claimed_service is not None
    assert claimed_administrator is not None
    assert claimed_service.attempts == claimed_administrator.attempts == 0

    with TestClient(app):
        assert runner.running is True
        recovered_service = next(
            job
            for job in engine.list_service_jobs(
                owner_user_id=OWNER_ID, party_id=PARTY_ID
            )
            if job.id == claimed_service.id
        )
        recovered_administrator = engine.list_administrator_jobs(
            owner_user_id=OWNER_ID, party_id=PARTY_ID
        )[0]
        assert (
            recovered_service.status,
            recovered_service.attempts,
            recovered_service.claim_token,
        ) == ("pending", 0, None)
        assert (
            recovered_administrator.status,
            recovered_administrator.attempts,
            recovered_administrator.claim_token,
        ) == ("pending", 0, None)

    assert runner.running is False


@pytest.mark.parametrize(
    "enabled_role",
    (
        "atomic",
        "administrator",
    ),
)
def test_startup_rejects_enabled_but_unavailable_local_role(
    tmp_path: Path,
    enabled_role: str,
) -> None:
    settings = replace(
        _settings(tmp_path),
        rp_atomic_service_enabled=enabled_role == "atomic",
        rp_administrator_enabled=enabled_role == "administrator",
    )

    with pytest.raises(ValueError, match="LOCAL_LLM_ENABLED=true"):
        create_app(settings)


def test_user_delete_stays_blocked_by_clean_party(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    RPTurnEngine(settings.rp_sqlite_path).create_party(
        owner_user_id=OWNER_ID,
        party_id=PARTY_ID,
        **_party_source(),
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.request(
            "DELETE",
            f"/api/admin/users/{OWNER_ID}",
            json={"delete_data": False},
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "user still owns RP parties"
