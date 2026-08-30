from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.rp.content import (
    SCENARIO_SNAPSHOT_SCHEMA_VERSION,
    WORLD_SNAPSHOT_SCHEMA_VERSION,
    ScenarioSnapshot,
    WorldSnapshot,
)
from app.rp.schema import RP_DATABASE_APPLICATION_ID, RP_SCHEMA_VERSION, RPSchemaError
from app.rp.turn_engine import (
    RPIdempotencyConflict,
    RPPartyNotFound,
    RPPartyVersionConflict,
    RPTurnEngine,
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


def test_fresh_database_is_clean_and_reopens_without_legacy_tables(tmp_path: Path) -> None:
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    created = engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()

    assert tables == {"rp_parties", "rp_turns", "rp_story_memory_snapshots"}
    assert application_id == RP_DATABASE_APPLICATION_ID
    assert schema_version == RP_SCHEMA_VERSION
    assert foreign_key_errors == []
    reopened = RPTurnEngine(database)
    assert reopened.get_party(owner_user_id="owner-one", party_id="party-one") == created
    assert reopened.list_turns(owner_user_id="owner-one", party_id="party-one") == ()


def test_turns_preserve_exact_raw_text_and_are_isolated_by_party(tmp_path: Path) -> None:
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )
    engine.create_party(
        owner_user_id="owner-one", party_id="party-two", **_party_source()
    )

    first = engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-one",
        request_id="shared-request",
        idempotency_key="shared-key",
        expected_version=0,
        player_text="  Я остаюсь.  ",
        narrator_text="  Дождь усиливается.  ",
    )
    other = engine.commit_turn(
        owner_user_id="owner-one",
        party_id="party-two",
        request_id="shared-request",
        idempotency_key="shared-key",
        expected_version=0,
        player_text="Я ухожу.",
        narrator_text="Дверь закрывается.",
    )

    assert first.committed_version == 1
    assert first.player_text == "  Я остаюсь.  "
    assert first.narrator_text == "  Дождь усиливается.  "
    assert other.committed_version == 1
    assert [
        turn.id
        for turn in engine.list_turns(owner_user_id="owner-one", party_id="party-one")
    ] == [first.id]
    assert [
        turn.id
        for turn in engine.list_turns(owner_user_id="owner-one", party_id="party-two")
    ] == [other.id]
    with pytest.raises(RPPartyNotFound):
        engine.list_turns(owner_user_id="owner-two", party_id="party-one")

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE rp_turns SET narrator_text = 'переписано' WHERE id = ?",
                (first.id,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute("DELETE FROM rp_turns WHERE id = ?", (first.id,))


def test_exact_retry_returns_one_turn_and_changed_payload_conflicts(tmp_path: Path) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )
    arguments = {
        "owner_user_id": "owner-one",
        "party_id": "party-one",
        "request_id": "request-one",
        "idempotency_key": "key-one",
        "expected_version": 0,
        "player_text": "Я жду.",
        "narrator_text": "Время идёт.",
    }

    first = engine.commit_turn(**arguments)
    second = engine.commit_turn(
        **{
            **arguments,
            "request_id": "request-two",
            "idempotency_key": "key-two",
            "expected_version": 1,
            "player_text": "Я продолжаю ждать.",
            "narrator_text": "Наступает вечер.",
        }
    )
    repeated = engine.commit_turn(**arguments)

    assert repeated == first
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == (
        first,
        second,
    )
    with pytest.raises(RPIdempotencyConflict):
        engine.commit_turn(**{**arguments, "narrator_text": "Другой исход."})
    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 2


def test_stale_party_version_does_not_create_a_turn(tmp_path: Path) -> None:
    engine = RPTurnEngine(tmp_path / "rp-clean.db")
    engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )

    with pytest.raises(RPPartyVersionConflict):
        engine.commit_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="request-one",
            idempotency_key="key-one",
            expected_version=1,
            player_text="Я действую.",
            narrator_text="Мир отвечает.",
        )

    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 0
    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()


def test_failed_atomic_commit_keeps_turn_and_counter_unchanged(tmp_path: Path) -> None:
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TRIGGER force_party_counter_failure
            BEFORE UPDATE OF current_version ON rp_parties
            BEGIN
                SELECT RAISE(FAIL, 'forced party counter failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced party counter failure"):
        engine.commit_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="request-one",
            idempotency_key="key-one",
            expected_version=0,
            player_text="Я действую.",
            narrator_text="Мир отвечает.",
        )

    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()
    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 0


def test_legacy_database_is_rejected_without_mutation(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE campaigns(id TEXT PRIMARY KEY)")
        connection.execute("INSERT INTO campaigns(id) VALUES('legacy-party')")
    before = database.read_bytes()

    with pytest.raises(RPSchemaError, match="not an isolated RP engine database"):
        RPTurnEngine(database)

    assert database.read_bytes() == before
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id FROM campaigns").fetchall() == [
            ("legacy-party",)
        ]


def test_database_with_foreign_application_marker_is_rejected_without_mutation(
    tmp_path: Path,
) -> None:
    database = tmp_path / "foreign.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA application_id = 1234")
        connection.execute("CREATE TABLE foreign_data(id INTEGER PRIMARY KEY)")
    before = database.read_bytes()

    with pytest.raises(RPSchemaError, match="unexpected application id"):
        RPTurnEngine(database)

    assert database.read_bytes() == before


def test_previous_clean_schema_is_rejected_without_migration(tmp_path: Path) -> None:
    database = tmp_path / "rp-schema-v2.db"
    with sqlite3.connect(database) as connection:
        connection.execute(f"PRAGMA application_id = {RP_DATABASE_APPLICATION_ID}")
        connection.execute("PRAGMA user_version = 2")
        connection.execute("CREATE TABLE rp_parties(id TEXT PRIMARY KEY)")
        connection.execute("CREATE TABLE rp_turns(id INTEGER PRIMARY KEY)")
    before = database.read_bytes()

    with pytest.raises(RPSchemaError, match="unsupported RP schema version 2"):
        RPTurnEngine(database)

    assert database.read_bytes() == before


def test_concurrent_schema_initialization_and_turn_commit_are_serialized(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rp-clean.db"
    constructor_barrier = threading.Barrier(8)

    def construct_engine() -> RPTurnEngine:
        constructor_barrier.wait()
        return RPTurnEngine(database)

    with ThreadPoolExecutor(max_workers=8) as pool:
        engines = list(pool.map(lambda _: construct_engine(), range(8)))

    engines[0].create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )
    commit_barrier = threading.Barrier(2)

    def commit(engine: RPTurnEngine, suffix: str) -> object:
        commit_barrier.wait()
        try:
            return engine.commit_turn(
                owner_user_id="owner-one",
                party_id="party-one",
                request_id=f"request-{suffix}",
                idempotency_key=f"key-{suffix}",
                expected_version=0,
                player_text=f"Действие {suffix}.",
                narrator_text=f"Ответ {suffix}.",
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, engines[:2], ("one", "two")))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, RPPartyVersionConflict) for result in results) == 1
    assert engines[0].get_party(
        owner_user_id="owner-one", party_id="party-one"
    ).current_version == 1
    assert len(
        engines[0].list_turns(owner_user_id="owner-one", party_id="party-one")
    ) == 1


def test_deferred_commit_failure_rolls_back_raw_turn_and_party_version(
    tmp_path: Path,
) -> None:
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    engine.create_party(
        owner_user_id="owner-one", party_id="party-one", **_party_source()
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE forced_commit_parent(id INTEGER PRIMARY KEY);
            CREATE TABLE forced_commit_child(
                id INTEGER PRIMARY KEY,
                missing_parent_id INTEGER NOT NULL,
                FOREIGN KEY(missing_parent_id) REFERENCES forced_commit_parent(id)
                    DEFERRABLE INITIALLY DEFERRED
            );
            CREATE TRIGGER fail_at_turn_commit
            AFTER INSERT ON rp_turns
            BEGIN
                INSERT INTO forced_commit_child(id, missing_parent_id) VALUES(NEW.id, -1);
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY constraint failed"):
        engine.commit_turn(
            owner_user_id="owner-one",
            party_id="party-one",
            request_id="request-one",
            idempotency_key="key-one",
            expected_version=0,
            player_text="Я действую.",
            narrator_text="Мир отвечает.",
        )

    assert engine.list_turns(owner_user_id="owner-one", party_id="party-one") == ()
    assert engine.get_party(owner_user_id="owner-one", party_id="party-one").current_version == 0
