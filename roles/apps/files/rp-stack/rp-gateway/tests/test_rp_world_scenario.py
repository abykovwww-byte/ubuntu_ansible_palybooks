from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.rp.content import (
    SUPPORTED_WORLD_ID,
    WorldDefinition,
    WorldScenarioLoader,
    WorldSourceError,
    snapshot_hash,
)
from app.rp.turn_engine import RPPartySnapshotConflict, RPTurnEngine


WORLD_ROOT = (
    Path(__file__).resolve().parents[2]
    / "worldpacks"
    / "day-watch-moscow-v2"
)
EXPECTED_STYLES = {"book", "action", "strategic"}
EXPECTED_STARTS = {
    "independent",
    "night-trainee",
    "day-witch",
    "inquisition-observer",
}


def test_committed_v2_world_and_all_scenarios_materialize() -> None:
    assert sorted(
        path.parent.name for path in WORLD_ROOT.parent.glob("*/world.json")
    ) == [SUPPORTED_WORLD_ID]
    loader = WorldScenarioLoader(WORLD_ROOT)
    world = loader.materialize_world()
    presets = loader.load_presets()

    assert world.world_id == SUPPORTED_WORLD_ID
    assert len(world.canon) == 2
    assert len(world.seed_lore_cards[0]["cards"]) == 20
    assert len(presets) == 12
    assert {len(preset.active_character_ids) for preset in presets} == {11}
    assert {(preset.style, preset.id.removeprefix(f"{preset.style}-")) for preset in presets} == {
        (style, start) for style in EXPECTED_STYLES for start in EXPECTED_STARTS
    }

    scenarios = [loader.materialize_preset(preset.id) for preset in presets]
    assert len({scenario.narrator_system for scenario in scenarios}) == len(EXPECTED_STYLES)
    assert len({scenario.narrator_note for scenario in scenarios}) == len(EXPECTED_STYLES)
    for style in EXPECTED_STYLES:
        assert len(
            {scenario.narrator_system for scenario in scenarios if scenario.style == style}
        ) == 1

    for preset in presets:
        scenario = loader.materialize_preset(preset.id)
        assert scenario.world_id == world.world_id
        assert scenario.source == "preset"
        assert set(scenario.active_character_ids) <= set(
            scenario.initial_state["characters"]
        )
        assert len(snapshot_hash(scenario)) == 64


@pytest.mark.parametrize(
    "forbidden_field",
    ("player_role", "openings", "presets", "state_seed", "rp_supervisor"),
)
def test_world_definition_rejects_scenario_and_runtime_fields(
    forbidden_field: str,
) -> None:
    source = json.loads((WORLD_ROOT / "world.json").read_text(encoding="utf-8"))
    source[forbidden_field] = {}

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        WorldDefinition.model_validate(source)


def test_loader_never_falls_back_to_a_legacy_manifest(tmp_path: Path) -> None:
    legacy_root = tmp_path / SUPPORTED_WORLD_ID
    legacy_root.mkdir()
    shutil.copyfile(WORLD_ROOT / "manifest.json", legacy_root / "manifest.json")

    with pytest.raises(WorldSourceError, match="world.json"):
        WorldScenarioLoader(legacy_root).load_world_definition()


def test_loader_rejects_an_asset_path_outside_the_world(tmp_path: Path) -> None:
    copied_world_root = tmp_path / SUPPORTED_WORLD_ID
    shutil.copytree(WORLD_ROOT, copied_world_root)
    source_path = copied_world_root / "world.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["canon_files"][0] = "../outside.md"
    source_path.write_text(
        json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    with pytest.raises(WorldSourceError, match="escapes the World root"):
        WorldScenarioLoader(copied_world_root).materialize_world()


def test_preset_and_free_scenario_use_the_same_snapshot_boundary() -> None:
    loader = WorldScenarioLoader(WORLD_ROOT)
    preset = loader.materialize_preset("action-independent")
    free = loader.materialize_free_scenario(
        scenario_id="free-independent",
        title="Свободный старт",
        player_role=preset.player_role,
        style=preset.style,
        format=preset.format,
        difficulty=preset.difficulty,
        detail_level=preset.detail_level,
        narrator_system=preset.narrator_system,
        narrator_note=preset.narrator_note,
        opening=preset.opening,
        initial_state=preset.initial_state,
        active_character_ids=preset.active_character_ids,
        local_overrides=preset.local_overrides,
    )

    assert free.source == "free"
    assert free.world_id == preset.world_id
    assert free.initial_state == preset.initial_state
    assert free.starting_relationships == preset.starting_relationships


def test_scenario_lore_materializes_and_unknown_override_fails_closed(
    tmp_path: Path,
) -> None:
    copied_world_root = tmp_path / SUPPORTED_WORLD_ID
    shutil.copytree(WORLD_ROOT, copied_world_root)
    preset_path = copied_world_root / "scenario-presets" / "action-independent.json"
    source = json.loads(preset_path.read_text(encoding="utf-8"))
    source["local_overrides"] = {
        "lore_cards": [
            {
                "key": "scenario:test-location",
                "title": "Закрытый кабинет",
                "keywords": ["кабинет"],
                "content": "В этом Scenario кабинет опечатан до полуночи.",
                "always_on": False,
                "enabled": True,
            }
        ]
    }
    preset_path.write_text(
        json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    scenario = WorldScenarioLoader(copied_world_root).materialize_preset(
        "action-independent"
    )
    assert scenario.local_overrides.lore_cards[0].key == "scenario:test-location"

    source["local_overrides"]["unknown"] = True
    preset_path.write_text(
        json.dumps(source, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with pytest.raises(WorldSourceError, match="Extra inputs are not permitted"):
        WorldScenarioLoader(copied_world_root).materialize_preset(
            "action-independent"
        )


def test_party_snapshots_survive_source_edits_and_cannot_be_rebound(
    tmp_path: Path,
) -> None:
    copied_world_root = tmp_path / SUPPORTED_WORLD_ID
    shutil.copytree(WORLD_ROOT, copied_world_root)
    loader = WorldScenarioLoader(copied_world_root)
    original_world = loader.materialize_world()
    action = loader.materialize_preset("action-independent")
    database = tmp_path / "rp-clean.db"
    engine = RPTurnEngine(database)
    created = engine.create_party(
        owner_user_id="owner-one",
        party_id="party-action",
        world_snapshot=original_world,
        scenario_snapshot=action,
    )

    campaign_bible = copied_world_root / "campaign-bible.md"
    campaign_bible.write_text(
        campaign_bible.read_text(encoding="utf-8") + "\nНовая редакция source.\n",
        encoding="utf-8",
    )
    changed_world = loader.materialize_world()
    stored = engine.get_party(owner_user_id="owner-one", party_id="party-action")

    assert snapshot_hash(changed_world) != created.world_hash
    assert stored.world_snapshot == original_world
    assert stored.world_hash == created.world_hash
    assert stored.scenario_snapshot == action
    with pytest.raises(RPPartySnapshotConflict):
        engine.create_party(
            owner_user_id="owner-one",
            party_id="party-action",
            world_snapshot=changed_world,
            scenario_snapshot=action,
        )

    current_action = engine.create_party(
        owner_user_id="owner-one",
        party_id="party-action-current",
        world_snapshot=changed_world,
        scenario_snapshot=action,
    )
    book = loader.materialize_preset("book-independent")
    current_book = engine.create_party(
        owner_user_id="owner-one",
        party_id="party-book",
        world_snapshot=changed_world,
        scenario_snapshot=book,
    )
    assert current_action.world_hash == current_book.world_hash
    assert current_action.scenario_hash != current_book.scenario_hash

    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="snapshots are immutable"):
            connection.execute(
                "UPDATE rp_parties SET world_hash = ? WHERE id = ?",
                ("0" * 64, created.id),
            )
