"""Strict World/Scenario source and immutable snapshot materialization."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)


WORLD_SCHEMA_VERSION = "rp-world.v1"
SCENARIO_PRESET_SCHEMA_VERSION = "rp-scenario-preset.v1"
WORLD_SNAPSHOT_SCHEMA_VERSION = "rp-world-snapshot.v1"
SCENARIO_SNAPSHOT_SCHEMA_VERSION = "rp-scenario-snapshot.v1"
SUPPORTED_WORLD_ID = "day-watch-moscow-v2"


def _non_empty(value: str) -> str:
    if not value.strip():
        raise ValueError("value must contain non-whitespace text")
    return value


Text = Annotated[str, AfterValidator(_non_empty)]
Slug = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$"),
]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
]
AssetPath = Annotated[str, AfterValidator(_non_empty)]


class WorldSourceError(ValueError):
    """The authored World/Scenario source is invalid or unsafe to materialize."""


class ScenarioPresetNotFound(LookupError):
    """The requested scenario preset does not exist in the selected World."""


class _ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ScenarioLoreCard(_ClosedModel):
    key: Text = Field(max_length=160)
    title: Text = Field(max_length=160)
    keywords: tuple[Text, ...] = Field(min_length=1, max_length=40)
    content: Text = Field(max_length=12_000)
    always_on: bool = False
    enabled: bool = True

    @model_validator(mode="after")
    def keywords_are_unique(self) -> ScenarioLoreCard:
        if len(self.keywords) != len(set(self.keywords)):
            raise ValueError("Scenario Lore keywords must be unique")
        return self


class ScenarioLocalOverrides(_ClosedModel):
    lore_cards: tuple[ScenarioLoreCard, ...] = ()

    @model_validator(mode="after")
    def lore_card_keys_are_unique(self) -> ScenarioLocalOverrides:
        keys = [card.key for card in self.lore_cards]
        if len(keys) != len(set(keys)):
            raise ValueError("Scenario Lore card keys must be unique")
        return self


class WorldDefinition(_ClosedModel):
    schema_version: Literal[WORLD_SCHEMA_VERSION]
    id: Slug
    title: Text
    language: Text
    premise: Text
    canon_files: tuple[AssetPath, ...] = Field(min_length=1)
    setting_rules_file: AssetPath
    characters_file: AssetPath
    relationship_ontology_file: AssetPath
    lore_card_files: tuple[AssetPath, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def source_files_are_unique(self) -> WorldDefinition:
        files = (
            *self.canon_files,
            self.setting_rules_file,
            self.characters_file,
            self.relationship_ontology_file,
            *self.lore_card_files,
        )
        if len(files) != len(set(files)):
            raise ValueError("World source files must be unique")
        return self


class ScenarioPresetDefinition(_ClosedModel):
    schema_version: Literal[SCENARIO_PRESET_SCHEMA_VERSION]
    id: Identifier
    title: Text
    world_id: Slug
    player_role: Text
    style: Text
    format: Text
    difficulty: Text | None = None
    detail_level: Text
    world_system_prompt_file: AssetPath
    world_authors_note_file: AssetPath
    opening_file: AssetPath
    initial_state_file: AssetPath
    active_character_ids: tuple[Identifier, ...] = Field(min_length=1)
    local_overrides: ScenarioLocalOverrides = Field(
        default_factory=ScenarioLocalOverrides
    )

    @model_validator(mode="after")
    def active_character_ids_are_unique(self) -> ScenarioPresetDefinition:
        if len(self.active_character_ids) != len(set(self.active_character_ids)):
            raise ValueError("active_character_ids must be unique")
        return self


class WorldSnapshot(_ClosedModel):
    schema_version: Literal[WORLD_SNAPSHOT_SCHEMA_VERSION]
    world_id: Slug
    title: Text
    language: Text
    premise: Text
    canon: tuple[Text, ...] = Field(min_length=1)
    setting_rules: Text
    characters: Text
    relationship_ontology: dict[str, JsonValue]
    seed_lore_cards: tuple[dict[str, JsonValue], ...] = Field(min_length=1)


class ScenarioSnapshot(_ClosedModel):
    schema_version: Literal[SCENARIO_SNAPSHOT_SCHEMA_VERSION]
    scenario_id: Identifier
    title: Text
    world_id: Slug
    source: Literal["preset", "free"]
    player_role: Text
    style: Text
    format: Text
    difficulty: Text | None = None
    detail_level: Text
    narrator_system: Text
    narrator_note: Text
    opening: Text
    initial_state: dict[str, JsonValue]
    active_character_ids: tuple[Identifier, ...] = Field(min_length=1)
    starting_relationships: dict[str, JsonValue]
    local_overrides: ScenarioLocalOverrides = Field(
        default_factory=ScenarioLocalOverrides
    )

    @model_validator(mode="after")
    def state_references_are_consistent(self) -> ScenarioSnapshot:
        characters = self.initial_state.get("characters")
        if not isinstance(characters, dict):
            raise ValueError("initial_state.characters must be an object")
        unknown = set(self.active_character_ids) - set(characters)
        if unknown:
            raise ValueError(f"unknown active_character_ids: {sorted(unknown)}")
        relationships = self.initial_state.get("relationships")
        if not isinstance(relationships, dict):
            raise ValueError("initial_state.relationships must be an object")
        if relationships != self.starting_relationships:
            raise ValueError("starting_relationships must match initial_state.relationships")
        for key in ("player", "factions", "locations"):
            if not isinstance(self.initial_state.get(key), dict):
                raise ValueError(f"initial_state.{key} must be an object")
        return self


Snapshot = WorldSnapshot | ScenarioSnapshot


def canonical_snapshot_json(snapshot: Snapshot) -> str:
    """Serialize a snapshot independently from source formatting and host paths."""
    return json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def snapshot_hash(snapshot: Snapshot) -> str:
    return hashlib.sha256(canonical_snapshot_json(snapshot).encode("utf-8")).hexdigest()


class WorldScenarioLoader:
    """Load only the Decision 043 World/Scenario source, never legacy manifests."""

    def __init__(self, world_root: str | Path):
        self.world_root = Path(world_root).resolve()
        if not self.world_root.is_dir():
            raise WorldSourceError(f"World root does not exist: {self.world_root}")

    def load_world_definition(self) -> WorldDefinition:
        definition = self._load_model("world.json", WorldDefinition)
        if definition.id != SUPPORTED_WORLD_ID:
            raise WorldSourceError(
                f"unsupported RP World {definition.id!r}; expected {SUPPORTED_WORLD_ID!r}"
            )
        if self.world_root.name != definition.id:
            raise WorldSourceError(
                f"World directory {self.world_root.name!r} does not match id {definition.id!r}"
            )
        return definition

    def materialize_world(self) -> WorldSnapshot:
        definition = self.load_world_definition()
        return WorldSnapshot(
            schema_version=WORLD_SNAPSHOT_SCHEMA_VERSION,
            world_id=definition.id,
            title=definition.title,
            language=definition.language,
            premise=definition.premise,
            canon=tuple(self._read_text(path) for path in definition.canon_files),
            setting_rules=self._read_text(definition.setting_rules_file),
            characters=self._read_text(definition.characters_file),
            relationship_ontology=self._read_json_object(
                definition.relationship_ontology_file
            ),
            seed_lore_cards=tuple(
                self._read_json_object(path) for path in definition.lore_card_files
            ),
        )

    def load_presets(self) -> tuple[ScenarioPresetDefinition, ...]:
        preset_root = self._resolve_directory("scenario-presets")
        definitions: list[ScenarioPresetDefinition] = []
        seen_ids: set[str] = set()
        for path in sorted(preset_root.glob("*.json")):
            relative_path = path.relative_to(self.world_root).as_posix()
            definition = self._load_model(relative_path, ScenarioPresetDefinition)
            if definition.id != path.stem:
                raise WorldSourceError(
                    f"scenario preset filename {path.stem!r} does not match id "
                    f"{definition.id!r}"
                )
            if definition.world_id != SUPPORTED_WORLD_ID:
                raise WorldSourceError(
                    f"scenario preset {definition.id!r} targets World "
                    f"{definition.world_id!r}"
                )
            if definition.id in seen_ids:
                raise WorldSourceError(f"duplicate scenario preset id {definition.id!r}")
            seen_ids.add(definition.id)
            definitions.append(definition)
        if not definitions:
            raise WorldSourceError("World must contain at least one scenario preset")
        return tuple(definitions)

    def load_preset(self, preset_id: str) -> ScenarioPresetDefinition:
        for definition in self.load_presets():
            if definition.id == preset_id:
                return definition
        raise ScenarioPresetNotFound(preset_id)

    def materialize_preset(self, preset_id: str) -> ScenarioSnapshot:
        definition = self.load_preset(preset_id)
        initial_state = self._read_json_object(definition.initial_state_file)
        relationships = initial_state.get("relationships")
        if not isinstance(relationships, dict):
            raise WorldSourceError(
                f"scenario preset {definition.id!r} initial state lacks relationships"
            )
        return ScenarioSnapshot(
            schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
            scenario_id=definition.id,
            title=definition.title,
            world_id=definition.world_id,
            source="preset",
            player_role=definition.player_role,
            style=definition.style,
            format=definition.format,
            difficulty=definition.difficulty,
            detail_level=definition.detail_level,
            narrator_system=self._read_text(definition.world_system_prompt_file),
            narrator_note=self._read_text(definition.world_authors_note_file),
            opening=self._read_text(definition.opening_file),
            initial_state=initial_state,
            active_character_ids=definition.active_character_ids,
            starting_relationships=relationships,
            local_overrides=definition.local_overrides,
        )

    def materialize_free_scenario(
        self,
        *,
        scenario_id: str,
        title: str,
        player_role: str,
        style: str,
        format: str,
        difficulty: str | None,
        detail_level: str,
        narrator_system: str,
        narrator_note: str,
        opening: str,
        initial_state: dict[str, JsonValue],
        active_character_ids: tuple[str, ...],
        local_overrides: ScenarioLocalOverrides | dict[str, JsonValue] | None = None,
    ) -> ScenarioSnapshot:
        world = self.load_world_definition()
        relationships = initial_state.get("relationships")
        if not isinstance(relationships, dict):
            raise WorldSourceError("free scenario initial state lacks relationships")
        return ScenarioSnapshot(
            schema_version=SCENARIO_SNAPSHOT_SCHEMA_VERSION,
            scenario_id=scenario_id,
            title=title,
            world_id=world.id,
            source="free",
            player_role=player_role,
            style=style,
            format=format,
            difficulty=difficulty,
            detail_level=detail_level,
            narrator_system=narrator_system,
            narrator_note=narrator_note,
            opening=opening,
            initial_state=initial_state,
            active_character_ids=active_character_ids,
            starting_relationships=relationships,
            local_overrides=local_overrides or ScenarioLocalOverrides(),
        )

    def _load_model(self, relative_path: str, model: type[BaseModel]):
        path = self._resolve_file(relative_path)
        try:
            return model.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            if isinstance(exc, WorldSourceError):
                raise
            raise WorldSourceError(f"invalid authored source {relative_path}: {exc}") from exc

    def _read_text(self, relative_path: str) -> str:
        path = self._resolve_file(relative_path)
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise WorldSourceError(f"authored text is empty: {relative_path}")
        return text

    def _read_json_object(self, relative_path: str) -> dict[str, JsonValue]:
        path = self._resolve_file(relative_path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise WorldSourceError(f"invalid JSON asset {relative_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise WorldSourceError(f"JSON asset must be an object: {relative_path}")
        return value

    def _resolve_file(self, relative_path: str) -> Path:
        if "\\" in relative_path:
            raise WorldSourceError(f"asset path must use forward slashes: {relative_path!r}")
        raw = Path(relative_path)
        if raw.is_absolute() or ".." in raw.parts:
            raise WorldSourceError(f"asset path escapes the World root: {relative_path!r}")
        try:
            resolved = (self.world_root / raw).resolve(strict=True)
            resolved.relative_to(self.world_root)
        except (OSError, ValueError) as exc:
            raise WorldSourceError(f"unsafe or missing World asset: {relative_path!r}") from exc
        if not resolved.is_file():
            raise WorldSourceError(f"World asset is not a file: {relative_path!r}")
        return resolved

    def _resolve_directory(self, relative_path: str) -> Path:
        path = (self.world_root / relative_path).resolve()
        try:
            path.relative_to(self.world_root)
        except ValueError as exc:
            raise WorldSourceError(f"unsafe World directory: {relative_path!r}") from exc
        if not path.is_dir():
            raise WorldSourceError(f"World directory does not exist: {relative_path!r}")
        return path
