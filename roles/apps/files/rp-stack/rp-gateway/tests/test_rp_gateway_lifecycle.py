from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

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
    state_path = tmp_path / "state" / "current.json"
    state_path.parent.mkdir(parents=True)
    source_state = Path(__file__).resolve().parents[2] / "state" / "campaign.example.json"
    state_path.write_text(source_state.read_text(encoding="utf-8"), encoding="utf-8")
    return Settings(
        app_env="test",
        campaign_id="default",
        database_url=f"sqlite:///{tmp_path / 'rp_gateway.db'}",
        rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
        world_state_path=str(state_path),
        party_state_root=str(tmp_path / "state" / "parties"),
        showroom_cover_dir=str(tmp_path / "showroom-covers"),
        worldpacks_path=str(tmp_path / "worldpacks"),
        auth_enabled=False,
        local_llm_enabled=False,
        rp_rebuild_enabled=True,
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


def test_rebuild_lifespan_owns_runner_and_recovery_does_not_spend_attempts(
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
    ("enabled_role", "expected_role"),
    (
        ("atomic", "atomic service"),
        ("administrator", "Administrator"),
    ),
)
def test_rebuild_startup_rejects_enabled_but_unavailable_role_model(
    tmp_path: Path,
    enabled_role: str,
    expected_role: str,
) -> None:
    settings = replace(
        _settings(tmp_path),
        service_model_choice="or-qwen-3.5-flash",
        rp_administrator_model_choice="or-qwen-3.5-flash",
        service_openrouter_api_key="",
        rp_atomic_service_enabled=enabled_role == "atomic",
        rp_administrator_enabled=enabled_role == "administrator",
    )

    with pytest.raises(
        ValueError,
        match=rf"{expected_role} model choice is unavailable",
    ):
        app = create_app(settings)
        with TestClient(app):
            pass


class _LegacyShowroomPathReached(RuntimeError):
    pass


def test_showroom_http_wrappers_keep_start_message_and_history_on_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path))
    legacy_calls: list[str] = []

    def legacy_party(party_id: str, **_: object) -> object:
        legacy_calls.append(party_id)
        raise _LegacyShowroomPathReached(party_id)

    def rebuilt_path_called(*_: object, **__: object) -> object:
        raise AssertionError("Showroom request reached the rebuilt RP engine")

    monkeypatch.setattr(app.state.party_store, "get_party", legacy_party)
    monkeypatch.setattr(
        app.state.showroom_store,
        "visitor_id",
        lambda _token: "visitor-one",
    )
    monkeypatch.setattr(
        app.state.showroom_store,
        "party_id_for_run",
        lambda *_args, **_kwargs: "legacy-showroom-party",
    )
    monkeypatch.setattr(app.state.rp_engine, "get_party", rebuilt_path_called)
    monkeypatch.setattr(app.state.rp_engine, "list_turns", rebuilt_path_called)

    with TestClient(app) as client:
        with pytest.raises(_LegacyShowroomPathReached):
            client.get("/api/showroom/runs/run-one/history")
        with pytest.raises(_LegacyShowroomPathReached):
            client.post("/api/showroom/runs/run-one/start", json={})
        client.post(
            "/api/showroom/runs/run-one/messages",
            json={"content": "Я осматриваюсь."},
        )

    assert legacy_calls == ["legacy-showroom-party"] * 3


def test_rebuild_lifespan_resumes_training_and_showroom_legacy_work_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path))
    parties = {
        "ordinary-rp": SimpleNamespace(
            id="ordinary-rp", status="active", scenario_type="rp"
        ),
        "showroom-rp": SimpleNamespace(
            id="showroom-rp", status="active", scenario_type="rp"
        ),
        "training": SimpleNamespace(
            id="training", status="active", scenario_type="training"
        ),
    }
    for party in parties.values():
        party.model_dump = (
            lambda mode, current=party: {"id": current.id, "scenario_type": current.scenario_type}
        )
    recovered_parties: list[str] = []
    recovered_branches: list[str] = []

    class RecoverableStore:
        def __init__(self, key: str, sink: list[str]):
            self.key = key
            self.sink = sink

        def recover_interrupted_work(self) -> dict[str, int]:
            self.sink.append(self.key)
            return {"turn_requests": 0, "service_jobs": 0}

        def service_jobs(self, *, limit: int) -> list[dict[str, object]]:
            assert limit == 20
            return []

    party_store = app.state.party_store
    monkeypatch.setattr(
        party_store, "list_parties", lambda **_kwargs: tuple(parties.values())
    )
    monkeypatch.setattr(
        party_store,
        "get_party",
        lambda party_id, **_kwargs: parties[party_id],
    )
    monkeypatch.setattr(
        party_store,
        "store_for_party",
        lambda party_id, **_kwargs: RecoverableStore(party_id, recovered_parties),
    )
    monkeypatch.setattr(
        party_store,
        "list_all_party_branches",
        lambda: tuple(
            {"party_id": party_id, "id": f"branch-{party_id}"}
            for party_id in parties
        ),
    )
    monkeypatch.setattr(
        party_store,
        "store_for_branch",
        lambda _party_id, branch_id, **_kwargs: RecoverableStore(
            branch_id, recovered_branches
        ),
    )
    monkeypatch.setattr(party_store, "resumable_autotest_runs", lambda: ())
    monkeypatch.setattr(
        app.state.showroom_store,
        "capabilities_for_party",
        lambda party_id: (
            {
                "interactive_links_enabled": False,
                "interactive_workspace_enabled": False,
            }
            if party_id == "showroom-rp"
            else None
        ),
    )

    with TestClient(app) as client:
        trace_parties = client.get("/api/turn-traces/parties")
        assert trace_parties.status_code == 200, trace_parties.text
        assert {item["id"] for item in trace_parties.json()["parties"]} == {
            "showroom-rp",
            "training",
        }
        assert (
            client.get("/api/turn-traces/parties/ordinary-rp/branches").status_code
            == 410
        )
        assert client.get("/api/state").status_code == 410
        legacy_chat = client.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "legacy"}]},
        )
        assert legacy_chat.status_code == 410, legacy_chat.text

    assert recovered_parties == ["showroom-rp", "training"]
    assert recovered_branches == ["branch-showroom-rp", "branch-training"]


def test_showroom_admin_reads_only_retained_legacy_worldpacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(_settings(tmp_path))

    class Pack:
        def __init__(self, pack_id: str, supported: list[str]):
            self.id = pack_id
            self.manifest = {"scenario_types": {"supported": supported}}

        def model_dump(self, *, mode: str) -> dict[str, object]:
            assert mode == "json"
            return {"id": self.id}

    packs = [
        Pack("training-pack", ["training"]),
        Pack("showroom-rp-pack", ["rp"]),
        Pack("ordinary-rp-pack", ["rp"]),
    ]
    monkeypatch.setattr(
        app.state.party_store,
        "list_worldpacks",
        lambda **_kwargs: packs,
    )
    monkeypatch.setattr(
        app.state.showroom_store,
        "list_scenarios",
        lambda **_kwargs: [{"worldpack_id": "showroom-rp-pack"}],
    )

    with TestClient(app) as client:
        response = client.get("/api/admin/showroom/worldpacks")

    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["worldpacks"]] == [
        "training-pack",
        "showroom-rp-pack",
    ]


class _LegacyTrainingPathReached(AssertionError):
    pass


def test_direct_training_party_stays_on_legacy_http_routes_after_rp_cutover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = replace(
        _settings(tmp_path),
        worldpacks_path=str(Path(__file__).resolve().parents[2] / "worldpacks"),
    )
    app = create_app(settings)
    training = SimpleNamespace(
        id="training-party",
        title="Training party",
        status="active",
        scenario_type="training",
        model_profile_id="training-profile",
    )
    training.model_dump = lambda mode: {
        "id": training.id,
        "title": training.title,
        "status": training.status,
        "scenario_type": training.scenario_type,
    }
    training_worldpack_id = "awareness-one-day"
    ordinary_worldpack_id = "ellinoid"

    class HistoryStore:
        def turn_history(self, *, limit: int) -> list[object]:
            assert limit == 50
            return []

        def history(self, *, limit: int) -> list[object]:
            assert limit == 50
            return []

    history_only = {"enabled": True}

    def legacy_store(*_args: object, **_kwargs: object) -> HistoryStore:
        if history_only["enabled"]:
            return HistoryStore()
        raise _LegacyTrainingPathReached

    party_store = app.state.party_store
    with TestClient(app) as client:
        monkeypatch.setattr(
            party_store, "list_parties", lambda **_kwargs: (training,)
        )
        monkeypatch.setattr(
            party_store, "get_party", lambda *_args, **_kwargs: training
        )
        monkeypatch.setattr(
            party_store, "create_party", lambda *_args, **_kwargs: training
        )
        monkeypatch.setattr(
            party_store,
            "require_active_model_profile",
            lambda *_args, **_kwargs: SimpleNamespace(),
        )
        monkeypatch.setattr(party_store, "store_for_party", legacy_store)

        listed = client.get("/api/parties")
        assert listed.status_code == 200, listed.text
        assert listed.json()["parties"] == [training.model_dump(mode="json")]

        clean_worlds = client.get("/api/worldpacks")
        assert clean_worlds.status_code == 200, clean_worlds.text
        assert [item["id"] for item in clean_worlds.json()["worldpacks"]] == [
            "day-watch-moscow-v2"
        ]

        training_worlds = client.get("/api/worldpacks?scenario_type=training")
        assert training_worlds.status_code == 200, training_worlds.text
        training_worldpack_ids = {
            item["id"] for item in training_worlds.json()["worldpacks"]
        }
        assert training_worldpack_id in training_worldpack_ids
        assert ordinary_worldpack_id not in training_worldpack_ids

        training_world = client.get(f"/api/worldpacks/{training_worldpack_id}")
        assert training_world.status_code == 200, training_world.text
        assert training_world.json()["worldpack"]["id"] == training_worldpack_id
        assert client.get(f"/api/worldpacks/{ordinary_worldpack_id}").status_code == 410
        assert (
            client.get(
                f"/api/worldpacks/{ordinary_worldpack_id}/player-templates"
            ).status_code
            == 410
        )
        assert (
            client.get(
                f"/api/player-characters?worldpack_id={ordinary_worldpack_id}"
            ).status_code
            == 410
        )

        templates = client.get(
            f"/api/worldpacks/{training_worldpack_id}/player-templates"
        )
        assert templates.status_code == 200, templates.text
        assert templates.json()["templates"]

        draft = client.post(
            "/api/player-characters/draft",
            json={
                "worldpack_id": training_worldpack_id,
                "name": "Ученик",
                "concept": "Проходит вводный курс.",
            },
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["draft"]["worldpack_id"] == training_worldpack_id

        character = client.post(
            "/api/player-characters",
            json={
                "worldpack_id": training_worldpack_id,
                "name": "Ученик",
                "description": "Проходит вводный курс.",
            },
        )
        assert character.status_code == 200, character.text
        character_id = character.json()["player_character"]["id"]
        characters = client.get(
            f"/api/player-characters?worldpack_id={training_worldpack_id}"
        )
        assert characters.status_code == 200, characters.text
        assert [
            item["id"] for item in characters.json()["player_characters"]
        ] == [character_id]
        assert client.get("/api/player-characters").status_code == 400

        ordinary_party = client.post(
            "/api/parties",
            json={
                "title": "Legacy RP party",
                "scenario_type": "training",
                "worldpack_id": ordinary_worldpack_id,
                "player_character_id": character_id,
                "model_profile_id": "training-profile",
            },
        )
        assert ordinary_party.status_code == 410

        created = client.post(
            "/api/parties",
            json={
                "title": "Training party",
                "scenario_type": "training",
                "worldpack_id": training_worldpack_id,
                "player_character_id": character_id,
                "model_profile_id": "training-profile",
            },
        )
        assert created.status_code == 200, created.text
        assert created.json()["party"]["scenario_type"] == "training"

        fetched = client.get(f"/api/parties/{training.id}")
        assert fetched.status_code == 200, fetched.text
        assert fetched.json()["party"]["id"] == training.id

        history = client.get(f"/api/parties/{training.id}/history")
        assert history.status_code == 200, history.text
        assert history.json()["turns"] == []

        history_only["enabled"] = False
        with pytest.raises(_LegacyTrainingPathReached):
            client.post(f"/api/parties/{training.id}/start", json={})
        with pytest.raises(_LegacyTrainingPathReached):
            client.post(
                f"/api/parties/{training.id}/messages",
                json={"content": "Продолжаю обучение.", "channel": "scene"},
            )


def test_user_delete_stays_blocked_by_clean_party_during_runtime_rollback(
    tmp_path: Path,
) -> None:
    settings = replace(_settings(tmp_path), rp_rebuild_enabled=False)
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
            json={"delete_data": True},
        )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "user still owns rebuilt RP parties"
