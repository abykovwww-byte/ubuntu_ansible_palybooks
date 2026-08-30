from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.main as main_module
from app.core.config import Settings
from app.main import create_app, settings_for_party
from app.models.schemas import PartyCreate, PlayerCharacterCreate
from app.services.adjudicator import Adjudicator
from app.services.narrative import NarrativeClient


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def production_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "production",
        "campaign_id": "default",
        "scenario_type": "rp",
        "database_url": f"sqlite:///{tmp_path / 'rp_gateway.db'}",
        "world_state_path": str(tmp_path / "state" / "current.json"),
        "party_state_root": str(tmp_path / "state" / "parties"),
        "state_schema_path": str(PROJECT_ROOT / "state" / "schema.json"),
        "worldpacks_path": str(PROJECT_ROOT / "worldpacks"),
        "llm_api_base": "mock://success",
        "llm_api_key": "test-key",
        "gemini_api_base": "mock://success",
        "gemini_api_key": "test-key",
        "openrouter_api_base": "mock://success",
        "service_openrouter_api_key": "test-key",
        "openrouter_model_catalog_live": False,
        "gemini_model_catalog_live": False,
        "local_llm_enabled": False,
        "post_turn_helpers_inline": True,
        "auth_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


def table_counts(database_path: Path, *tables: str) -> dict[str, int]:
    with sqlite3.connect(database_path) as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def database_dump(database_path: Path) -> str:
    with sqlite3.connect(database_path) as connection:
        return "\n".join(connection.iterdump())


def insert_legacy_training_resources(
    app: object,
    *,
    stored_scenario_type: str = "training",
    supported_modes: tuple[str, ...] = ("training",),
) -> tuple[str, str, str]:
    party_store = app.state.party_store
    party_id = "party_legacy_training"
    branch_id = "branch_legacy_training"
    run_id = "autotest_legacy_training"
    model_profile_id = party_store.list_model_profiles()[0].id
    timestamp = "2026-08-28T00:00:00Z"
    manifest = {
        "id": "legacy-training",
        "scenario_types": {"recommended": supported_modes[0], "supported": list(supported_modes)},
    }
    with party_store.connect() as connection:
        connection.execute(
            """
            INSERT INTO worldpacks(
                id, title, slug, status, premise, manifest_path, state_seed_path,
                manifest_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-training",
                "Legacy training",
                "legacy-training",
                "playable",
                "Persisted before the split",
                str(PROJECT_ROOT / "worldpacks" / "awareness" / "manifest.json"),
                str(PROJECT_ROOT / "worldpacks" / "awareness" / "state-seed.json"),
                json.dumps(manifest, ensure_ascii=False),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO player_characters(
                id, worldpack_id, name, description, status, profile_json, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "pc_legacy_training",
                "legacy-training",
                "Legacy learner",
                "Persisted before the split",
                "active",
                "{}",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO parties(
                id, title, scenario_type, worldpack_id, player_character_id,
                model_profile_id, state_campaign_id, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                party_id,
                "Legacy training party",
                stored_scenario_type,
                "legacy-training",
                "pc_legacy_training",
                model_profile_id,
                party_id,
                "active",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO party_branches(
                id, party_id, label, source_checkpoint_id, state_campaign_id,
                status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                branch_id,
                party_id,
                "Legacy training branch",
                1,
                branch_id,
                "active",
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO autotest_runs(
                id, source_party_id, test_party_id, branch_id, player_model_profile_id,
                player_prompt, requested_turns, status, created_at, updated_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                party_id,
                f"branch:{branch_id}",
                branch_id,
                model_profile_id,
                "Legacy player",
                2,
                "running",
                timestamp,
                timestamp,
            ),
        )
    return party_id, branch_id, run_id


def test_non_rp_process_fails_before_storage_creation(tmp_path: Path) -> None:
    settings = production_settings(tmp_path, scenario_type="training")

    with pytest.raises(RuntimeError, match="SCENARIO_TYPE=rp"):
        create_app(settings)

    assert not Path(settings.sqlite_path).exists()
    assert not Path(settings.world_state_path).exists()
    assert not Path(settings.party_state_root).exists()


def test_catalog_exposes_only_rp_compatible_worldpacks(tmp_path: Path) -> None:
    client = TestClient(create_app(production_settings(tmp_path)))

    response = client.get("/api/worldpacks")

    assert response.status_code == 200
    packs = response.json()["worldpacks"]
    assert packs
    assert all("rp" in pack["manifest"]["scenario_types"]["supported"] for pack in packs)
    assert {"awareness", "awareness-one-day"}.isdisjoint(pack["id"] for pack in packs)
    assert client.app.state.party_store.is_rp_worldpack_manifest(
        {"id": "prompt-generated", "rp_contract": {"schema_version": "rp-core.v2", "revision": 6}}
    )
    assert client.app.state.party_store.is_rp_worldpack_manifest(
        {"id": "legacy-prompt-generated", "mode": "prompt world"}
    )
    assert client.get("/api/worldpacks/awareness").status_code == 404
    with sqlite3.connect(client.app.state.settings.sqlite_path) as connection:
        registered = {row[0] for row in connection.execute("SELECT id FROM worldpacks")}
    assert "awareness" not in registered
    assert "awareness-one-day" not in registered


def test_direct_store_rejects_training_create_before_worldpack_or_state_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(production_settings(tmp_path))
    party_store = app.state.party_store
    request = PartyCreate.model_construct(
        title="Forbidden training",
        scenario_type="training",
        worldpack_id="awareness",
        player_character_id="pc_missing",
        model_profile_id="profile_missing",
    )
    monkeypatch.setattr(
        party_store,
        "get_worldpack",
        lambda *args, **kwargs: pytest.fail("worldpack lookup must not run for training"),
    )
    before = table_counts(Path(app.state.settings.sqlite_path), "parties", "campaigns", "state_versions")

    with pytest.raises(ValueError, match="scenario_type=rp"):
        party_store.create_party(request)

    assert table_counts(Path(app.state.settings.sqlite_path), *before) == before
    assert list(Path(app.state.settings.party_state_root).iterdir()) == []


@pytest.mark.parametrize("stored_scenario_type", ["training", "rp"])
def test_persisted_non_rp_resources_are_hidden_and_cannot_resume_or_mutate(
    tmp_path: Path,
    stored_scenario_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(production_settings(tmp_path))
    party_store = app.state.party_store
    party_id, branch_id, run_id = insert_legacy_training_resources(
        app,
        stored_scenario_type=stored_scenario_type,
    )
    state_path = party_store.state_path_for(party_id)
    owner_user_id = "legacy-training-owner"
    with party_store.connect() as connection:
        for table in ("player_characters", "parties", "party_branches", "autotest_runs"):
            connection.execute(
                f"UPDATE {table} SET owner_user_id = ?",
                (owner_user_id,),
            )

    database_path = Path(app.state.settings.sqlite_path)
    before_rejected_read = database_dump(database_path)
    with monkeypatch.context() as guard:
        guard.setattr(
            party_store,
            "scan_worldpacks",
            lambda: pytest.fail("scan_worldpacks must not run for a hidden party"),
        )
        with pytest.raises(ValueError, match="party not found"):
            party_store.get_party(party_id)
    assert database_dump(database_path) == before_rejected_read
    with pytest.raises(ValueError, match="party not found"):
        party_store.store_for_party(party_id)
    with pytest.raises(ValueError, match="party not found"):
        party_store.get_party_branch(party_id, branch_id)
    with pytest.raises(ValueError, match="player character not found"):
        party_store.get_player_character("pc_legacy_training")
    with pytest.raises(ValueError, match="player character not found"):
        party_store.delete_player_character("pc_legacy_training")
    with pytest.raises(ValueError, match="worldpack not found"):
        party_store.set_worldpack_visibility("legacy-training", "private")
    with pytest.raises(ValueError, match="autotest run not found"):
        party_store.get_autotest_run(run_id)
    with pytest.raises(ValueError, match="autotest run not found"):
        party_store.update_autotest_run(run_id, status="failed")

    assert all(party.id != party_id for party in party_store.list_parties())
    assert all(character.id != "pc_legacy_training" for character in party_store.list_player_characters())
    assert all(branch["id"] != branch_id for branch in party_store.list_all_party_branches())
    assert all(run["id"] != run_id for run in party_store.list_autotest_runs())
    assert all(run["id"] != run_id for run in party_store.resumable_autotest_runs())
    assert not state_path.exists()
    assert party_store.has_retired_non_rp_user_data(owner_user_id) is True
    with pytest.raises(ValueError, match="cleanup requires explicit O2"):
        party_store.delete_user_data(owner_user_id)
    with party_store.connect() as connection:
        assert connection.execute("SELECT status FROM autotest_runs WHERE id = ?", (run_id,)).fetchone()[0] == "running"
        assert connection.execute(
            "SELECT visibility FROM worldpacks WHERE id = ?", ("legacy-training",)
        ).fetchone()[0] == "public"
        assert connection.execute(
            "SELECT COUNT(*) FROM player_characters WHERE id = ?", ("pc_legacy_training",)
        ).fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM parties WHERE id = ?", (party_id,)).fetchone()[0] == 1


@pytest.mark.parametrize("delete_data", [False, True])
def test_admin_user_delete_refuses_retired_training_data_without_reporting_success(
    tmp_path: Path,
    delete_data: bool,
) -> None:
    client = TestClient(create_app(production_settings(tmp_path)))
    user = client.app.state.auth_store.create_user("legacy-training-user", "temporary-password", "user")
    party_id, _, _ = insert_legacy_training_resources(client.app, stored_scenario_type="rp")
    with client.app.state.party_store.connect() as connection:
        for table in ("player_characters", "parties", "party_branches", "autotest_runs"):
            connection.execute("UPDATE " + table + " SET owner_user_id = ?", (user.id,))

    response = client.request(
        "DELETE",
        f"/api/admin/users/{user.id}",
        json={"delete_data": delete_data},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "user owns retired non-RP data; cleanup requires explicit O2"
    assert client.app.state.auth_store.get_user(user.id).id == user.id
    with client.app.state.party_store.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM parties WHERE id = ?", (party_id,)).fetchone()[0] == 1


@pytest.mark.parametrize(
    ("stored_scenario_type", "stored_status"),
    (("rp", "active"), ("novel", "archived")),
)
def test_legacy_showroom_link_hides_party_and_preserves_all_rows(
    tmp_path: Path,
    stored_scenario_type: str,
    stored_status: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(production_settings(tmp_path))
    client = TestClient(app)
    party_store = app.state.party_store
    party_id, branch_id, run_id = insert_legacy_training_resources(
        app,
        stored_scenario_type=stored_scenario_type,
        supported_modes=("rp",),
    )
    owner_user_id = "legacy-showroom-owner"
    with party_store.connect() as connection:
        for table in ("player_characters", "parties", "party_branches", "autotest_runs"):
            connection.execute("UPDATE " + table + " SET owner_user_id = ?", (owner_user_id,))
        connection.execute("UPDATE parties SET status = ? WHERE id = ?", (stored_status, party_id))
        connection.execute("CREATE TABLE showroom_runs(party_id TEXT NOT NULL UNIQUE)")
        connection.execute("INSERT INTO showroom_runs(party_id) VALUES(?)", (party_id,))
    guarded_tables = ("player_characters", "parties", "party_branches", "autotest_runs", "showroom_runs")
    before = table_counts(Path(app.state.settings.sqlite_path), *guarded_tables)
    before_rejected_read = database_dump(Path(app.state.settings.sqlite_path))

    with monkeypatch.context() as guard:
        guard.setattr(
            party_store,
            "scan_worldpacks",
            lambda: pytest.fail("scan_worldpacks must not run for a Showroom-linked party"),
        )
        with pytest.raises(ValueError, match="party not found"):
            party_store.get_party(party_id, allow_retired_read=True)
    assert database_dump(Path(app.state.settings.sqlite_path)) == before_rejected_read
    with pytest.raises(ValueError, match="party not found"):
        party_store.get_party(party_id)
    with pytest.raises(ValueError, match="party not found"):
        party_store.get_party_branch(party_id, branch_id)
    with pytest.raises(ValueError, match="autotest run not found"):
        party_store.get_autotest_run(run_id)
    assert all(party.id != party_id for party in party_store.list_parties())
    assert all(branch["id"] != branch_id for branch in party_store.list_all_party_branches())
    assert all(run["id"] != run_id for run in party_store.list_autotest_runs())
    assert all(run["id"] != run_id for run in party_store.resumable_autotest_runs())
    assert party_store.export_dataset_records(
        owner_user_id=None,
        scenario_type=stored_scenario_type,
    )["approved_turns"] == 0
    assert client.get(f"/api/parties/{party_id}").status_code == 404
    assert client.get(f"/api/parties/{party_id}/state").status_code == 404
    assert client.get(f"/api/parties/{party_id}/history").status_code == 404
    assert client.get(f"/api/parties/{party_id}/branches").status_code == 404
    assert client.get(f"/api/admin/datasets/export.jsonl?scenario_type={stored_scenario_type}").text == ""
    assert party_store.has_retired_non_rp_user_data(owner_user_id) is True
    with pytest.raises(ValueError, match="cleanup requires explicit O2"):
        party_store.delete_user_data(owner_user_id)

    assert table_counts(Path(app.state.settings.sqlite_path), *guarded_tables) == before


def test_rebuilt_dataset_export_excludes_all_legacy_party_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        production_settings(
            tmp_path,
            rp_rebuild_enabled=True,
            rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
            rp_atomic_service_enabled=False,
            rp_administrator_enabled=False,
        )
    )
    captured: dict[str, object] = {}

    def export_dataset_records(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"records": [], "approved_turns": 0, "skipped_missing_prompt": 0}

    monkeypatch.setattr(
        app.state.party_store,
        "export_dataset_records",
        export_dataset_records,
    )

    response = TestClient(app).get("/api/admin/datasets/export.jsonl")

    assert response.status_code == 200
    assert response.text == ""
    assert captured["party_ids"] == set()


def test_rebuilt_legacy_party_store_endpoints_fail_before_read_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = create_app(
        production_settings(
            tmp_path,
            rp_rebuild_enabled=True,
            rp_database_url=f"sqlite:///{tmp_path / 'rp_engine.db'}",
            rp_atomic_service_enabled=False,
            rp_administrator_enabled=False,
        )
    )
    party_store = app.state.party_store
    database_path = Path(app.state.settings.sqlite_path)
    before = database_dump(database_path)

    def unexpected_legacy_access(*args: object, **kwargs: object) -> None:
        pytest.fail("rebuilt runtime must reject the endpoint before legacy PartyStore access")

    for method_name in (
        "get_party",
        "get_party_branch",
        "store_for_party",
        "store_for_branch",
        "update_party_dataset",
        "list_dataset_turns",
        "set_turn_dataset_label",
    ):
        monkeypatch.setattr(party_store, method_name, unexpected_legacy_access)

    requests = (
        ("GET", "/api/parties/party_legacy/turn-traces", None),
        ("GET", "/api/parties/party_legacy/turn-traces/request_legacy", None),
        (
            "POST",
            "/api/parties/party_legacy/turn-traces/request_legacy/annotations",
            {
                "annotation_id": "annotation-legacy",
                "phase_key": "gateway_assembly",
                "body": "Must remain blocked.",
            },
        ),
        (
            "PATCH",
            "/api/admin/datasets/parties/party_legacy",
            {"review_status": "approved", "tags": ["blocked"]},
        ),
        ("GET", "/api/admin/datasets/parties/party_legacy/turns", None),
        (
            "PUT",
            "/api/admin/datasets/parties/party_legacy/turns/1",
            {"review_status": "approved", "tags": ["blocked"], "notes": "Must not persist."},
        ),
    )
    client = TestClient(app)

    for method, path, payload in requests:
        response = client.request(method, path, json=payload)
        assert response.status_code == 410, (method, path, response.text)
        assert response.json()["detail"] == (
            "legacy PartyStore operation is unavailable after rebuilt cutover"
        )

    assert database_dump(database_path) == before


def test_direct_autotest_with_hidden_test_party_cannot_resume_or_update(tmp_path: Path) -> None:
    app = create_app(production_settings(tmp_path))
    party_store = app.state.party_store
    owner_user_id = "legacy-direct-autotest-owner"
    character = party_store.create_player_character(
        PlayerCharacterCreate(
            worldpack_id="incident-50",
            name="RP source character",
            description="Valid RP source",
        ),
        owner_user_id=owner_user_id,
    )
    source_party = party_store.create_party(
        PartyCreate(
            title="Valid RP source",
            scenario_type="rp",
            worldpack_id="incident-50",
            player_character_id=character.id,
            model_profile_id=party_store.list_model_profiles()[0].id,
        ),
        owner_user_id=owner_user_id,
    )
    hidden_party_id, _, run_id = insert_legacy_training_resources(app)
    with party_store.connect() as connection:
        connection.execute(
            "UPDATE parties SET owner_user_id = ? WHERE id = ?",
            (owner_user_id, hidden_party_id),
        )
        connection.execute(
            "UPDATE player_characters SET owner_user_id = ? WHERE id = ?",
            (owner_user_id, "pc_legacy_training"),
        )
        connection.execute(
            """
            UPDATE autotest_runs
            SET owner_user_id = ?, source_party_id = ?, test_party_id = ?, branch_id = NULL
            WHERE id = ?
            """,
            (owner_user_id, source_party.id, hidden_party_id, run_id),
        )
    guarded_tables = ("parties", "party_branches", "autotest_runs", "turns", "state_versions", "audit_events")
    before = table_counts(Path(app.state.settings.sqlite_path), *guarded_tables)

    with pytest.raises(ValueError, match="autotest run not found"):
        party_store.get_autotest_run(run_id)
    with pytest.raises(ValueError, match="autotest run not found"):
        party_store.update_autotest_run(run_id, status="failed")
    assert all(run["id"] != run_id for run in party_store.list_autotest_runs())
    assert all(run["id"] != run_id for run in party_store.resumable_autotest_runs())
    assert party_store.active_autotest_for_party(hidden_party_id) is None

    assert table_counts(Path(app.state.settings.sqlite_path), *guarded_tables) == before
    with party_store.connect() as connection:
        assert connection.execute("SELECT status FROM autotest_runs WHERE id = ?", (run_id,)).fetchone()[0] == "running"


def test_runtime_settings_reject_persisted_non_rp_party() -> None:
    settings = Settings(app_env="production", scenario_type="rp")
    party = SimpleNamespace(scenario_type="training")

    with pytest.raises(ValueError, match="scenario_type=rp"):
        settings_for_party(settings, party)


def test_retired_training_http_routes_are_absent_and_non_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(production_settings(tmp_path)))
    party_id, _, _ = insert_legacy_training_resources(client.app)
    database_path = Path(client.app.state.settings.sqlite_path)
    guarded_tables = ("campaigns", "state_versions", "turn_requests", "turns", "audit_events", "parties")
    before = table_counts(database_path, *guarded_tables)
    provider_called = False

    async def unexpected_provider_call(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal provider_called
        provider_called = True
        return {}

    monkeypatch.setattr(Adjudicator, "handle_chat", unexpected_provider_call)

    attempts = (
        ("get", "/api/showroom/scenarios", None),
        ("get", "/api/admin/showroom/scenarios", None),
        ("post", "/api/showroom/scenarios/missing/runs", {}),
        ("post", f"/api/parties/{party_id}/artifact-events", {}),
        ("get", f"/api/parties/{party_id}/workspace", None),
        ("post", f"/api/parties/{party_id}/workspace-events", {}),
        ("get", f"/api/parties/{party_id}/workspace/files/file/content", None),
    )
    for method, path, payload in attempts:
        response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert response.status_code == 404, (method, path, response.text)

    foreign_party_attempts = (
        ("get", f"/api/parties/{party_id}", None),
        ("post", f"/api/parties/{party_id}/start", None),
        ("post", f"/api/parties/{party_id}/messages", {"content": "bypass"}),
    )
    for method, path, payload in foreign_party_attempts:
        response = getattr(client, method)(path, json=payload) if payload is not None else getattr(client, method)(path)
        assert response.status_code in {400, 404}, (method, path, response.text)
        assert "party not found" in response.text

    forbidden_paths = {
        "/api/parties/{party_id}/artifact-events",
        "/api/parties/{party_id}/workspace",
        "/api/parties/{party_id}/workspace-events",
        "/api/parties/{party_id}/workspace/files/{file_id}/content",
    }
    openapi_paths = set(client.app.openapi()["paths"])
    assert forbidden_paths.isdisjoint(openapi_paths)
    assert not any(path.startswith("/api/showroom") for path in openapi_paths)
    assert not any(path.startswith("/api/admin/showroom") for path in openapi_paths)
    assert table_counts(database_path, *guarded_tables) == before
    assert provider_called is False


def test_api_rejects_training_create_before_worldpack_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(create_app(production_settings(tmp_path)))
    monkeypatch.setattr(
        client.app.state.party_store,
        "get_worldpack",
        lambda *args, **kwargs: pytest.fail("worldpack lookup must not run for training"),
    )

    response = client.post(
        "/api/parties",
        json={
            "title": "Forbidden training",
            "scenario_type": "training",
            "worldpack_id": "awareness",
            "player_character_id": "pc_missing",
            "model_profile_id": "profile_missing",
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"][0]
    assert detail["loc"][0] == "body"
    assert detail["loc"][-1] == "scenario_type"
    assert detail["input"] == "training"


def test_production_rp_start_and_message_use_only_rp_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls: list[str] = []
    provider_text = "The incident response team reviews the evidence and waits for the player's decision."

    async def provider_complete(*args: object, **kwargs: object) -> dict[str, object]:
        provider_calls.append(str(kwargs.get("request_id") or "provider-call"))
        return {
            "id": f"rp-direct-{len(provider_calls)}",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": provider_text},
                    "finish_reason": "stop",
                }
            ],
        }

    monkeypatch.setattr(NarrativeClient, "complete", provider_complete)
    client = TestClient(create_app(production_settings(tmp_path)))
    assert client.app.state.adjudicator.validator is not None
    model_id = client.get("/api/model-profiles").json()["model_profiles"][0]["id"]
    character_response = client.post(
        "/api/player-characters",
        json={
            "worldpack_id": "incident-50",
            "name": "RP Operator",
            "description": "Incident responder",
            "profile": {},
        },
    )
    assert character_response.status_code == 200, character_response.text
    party_response = client.post(
        "/api/parties",
        json={
            "title": "C1 RP smoke",
            "scenario_type": "rp",
            "worldpack_id": "incident-50",
            "player_character_id": character_response.json()["player_character"]["id"],
            "model_profile_id": model_id,
        },
    )
    assert party_response.status_code == 200, party_response.text
    party_id = party_response.json()["party"]["id"]

    started = client.post(
        f"/api/parties/{party_id}/start",
        json={"idempotency_key": "c1-rp-start"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["message"]["content"] == provider_text
    message = client.post(
        f"/api/parties/{party_id}/messages",
        json={"content": "Проверяю журналы и фиксирую цепочку хранения.", "idempotency_key": "c1-rp-turn"},
    )
    assert message.status_code == 200, message.text
    assert message.json()["choices"][0]["message"]["content"] == provider_text
    assert len(provider_calls) == 2

    with sqlite3.connect(client.app.state.settings.sqlite_path) as connection:
        rows = connection.execute(
            "SELECT metadata_json FROM turns WHERE campaign_id = ? ORDER BY id",
            (party_id,),
        ).fetchall()
    assert len(rows) == 2
    for row in rows:
        metadata = json.loads(row[0])
        expected_validation = None if metadata["rp_contract_revision"] < 3 else True
        assert metadata["validator_valid"] is expected_validation
        assert metadata["repaired"] is False
        assert metadata["fallback"] is False
        assert metadata["llm_calls"] == 1
        assert "training_runtime_contract_hash" not in metadata
        assert "training_capabilities" not in metadata


def test_production_rp_provider_timeout_has_no_fallback_or_turn_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    async def provider_timeout(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        raise httpx.ReadTimeout(
            "provider timed out",
            request=httpx.Request("POST", "https://provider.example/v1/chat/completions"),
        )

    monkeypatch.setattr(NarrativeClient, "complete", provider_timeout)
    client = TestClient(create_app(production_settings(tmp_path)))
    model_id = client.get("/api/model-profiles").json()["model_profiles"][0]["id"]
    character_response = client.post(
        "/api/player-characters",
        json={
            "worldpack_id": "incident-50",
            "name": "RP Failure Probe",
            "description": "Incident responder",
            "profile": {},
        },
    )
    assert character_response.status_code == 200, character_response.text
    party_response = client.post(
        "/api/parties",
        json={
            "title": "C1 provider failure",
            "scenario_type": "rp",
            "worldpack_id": "incident-50",
            "player_character_id": character_response.json()["player_character"]["id"],
            "model_profile_id": model_id,
        },
    )
    assert party_response.status_code == 200, party_response.text
    party_id = party_response.json()["party"]["id"]
    party_state_store = client.app.state.party_store.store_for_party(party_id)
    before = table_counts(
        Path(client.app.state.settings.sqlite_path),
        "turns",
        "state_versions",
    )
    version_before = party_state_store.current_version()

    response = client.post(
        f"/api/parties/{party_id}/start",
        json={"idempotency_key": "c1-rp-provider-timeout"},
    )

    assert response.status_code == 504, response.text
    assert provider_calls == 1
    assert table_counts(Path(client.app.state.settings.sqlite_path), *before) == before
    assert party_state_store.current_version() == version_before
    assert party_state_store.turn_history() == []
    with sqlite3.connect(client.app.state.settings.sqlite_path) as connection:
        fallback_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE campaign_id = ? AND event_type = 'llm_safe_fallback'",
            (party_id,),
        ).fetchone()[0]
    assert fallback_audits == 0


@pytest.mark.parametrize("provider_mode", ["success", "timeout"])
def test_production_rp_autotest_fails_closed_on_provider_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provider_mode: str,
) -> None:
    async def next_action(*args: object, **kwargs: object) -> str:
        return "I inspect the incident timeline."

    provider_calls = 0

    async def provider_complete(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_mode == "timeout":
            raise httpx.ReadTimeout(
                "provider timed out",
                request=httpx.Request("POST", "https://provider.example/v1/chat/completions"),
            )
        return {
            "id": "rp-autotest-direct",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "The investigation continues."},
                    "finish_reason": "stop",
                }
            ],
        }

    monkeypatch.setattr(main_module.AutoPlayerClient, "next_action", next_action)
    monkeypatch.setattr(NarrativeClient, "complete", provider_complete)
    client = TestClient(create_app(production_settings(tmp_path)))
    assert client.app.state.adjudicator.validator is not None
    model_id = client.get("/api/model-profiles").json()["model_profiles"][0]["id"]
    character_response = client.post(
        "/api/player-characters",
        json={
            "worldpack_id": "incident-50",
            "name": "RP Autotest Operator",
            "description": "Incident responder",
            "profile": {},
        },
    )
    assert character_response.status_code == 200, character_response.text
    party_response = client.post(
        "/api/parties",
        json={
            "title": f"C1 RP autotest {provider_mode}",
            "scenario_type": "rp",
            "worldpack_id": "incident-50",
            "player_character_id": character_response.json()["player_character"]["id"],
            "model_profile_id": model_id,
        },
    )
    assert party_response.status_code == 200, party_response.text
    party_id = party_response.json()["party"]["id"]
    player_profiles = client.get("/api/admin/autotests/models").json()["model_profiles"]
    assert player_profiles

    started = client.post(
        "/api/admin/autotests",
        json={
            "source_party_id": party_id,
            "player_prompt": "Take one in-world action.",
            "turn_count": 1,
            "player_model_profile_id": player_profiles[0]["id"],
            "rp_contract_revision": 6,
        },
    )
    assert started.status_code == 200, started.text
    run = started.json()["run"]
    branch = started.json()["branch"]
    checkpoint = started.json()["checkpoint"]

    deadline = time.time() + 3
    while time.time() < deadline:
        run = next(
            item
            for item in client.get("/api/admin/autotests").json()["runs"]
            if item["id"] == run["id"]
        )
        if run["status"] in {"completed", "failed"}:
            break
        time.sleep(0.02)

    branch_store = client.app.state.party_store.store_for_branch(party_id, branch["id"])
    assert provider_calls == 1
    if provider_mode == "success":
        assert run["status"] == "completed", run
        assert run["completed_turns"] == 1
        assert run["fallback_turns"] == 0
        history = branch_store.turn_history(limit=10)
        assert len(history) == 1
        metadata = branch_store.turn_metadata(int(history[0]["id"]))
        assert metadata["validator_valid"] is True
        assert metadata["repaired"] is False
        assert metadata["fallback"] is False
        assert metadata["llm_calls"] == 1
        assert "training_runtime_contract_hash" not in metadata
        assert "training_capabilities" not in metadata
        return

    assert run["status"] == "failed", run
    assert run["completed_turns"] == 0
    assert run["fallback_turns"] == 0
    assert "Narrative provider timed out" in run["error"]
    assert branch_store.current_version() == checkpoint["state_version"]
    assert branch_store.turn_history(limit=10) == []
    with sqlite3.connect(client.app.state.settings.sqlite_path) as connection:
        branch_state_versions = connection.execute(
            "SELECT COUNT(*) FROM state_versions WHERE campaign_id = ?",
            (branch["state_campaign_id"],),
        ).fetchone()[0]
        fallback_audits = connection.execute(
            "SELECT COUNT(*) FROM audit_events WHERE campaign_id = ? AND event_type = 'llm_safe_fallback'",
            (branch["state_campaign_id"],),
        ).fetchone()[0]
    assert branch_state_versions == 1
    assert fallback_audits == 0
