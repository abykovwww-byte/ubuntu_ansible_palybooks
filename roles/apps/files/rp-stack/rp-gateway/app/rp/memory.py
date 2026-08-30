"""Five-section story memory for the isolated RP engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RP_MEMORY_SCHEMA_VERSION = "rp-story-memory.v1"
RP_MEMORY_PROMPT_MAX_CHARS = 24_000
RP_MEMORY_SECTION_KEYS = (
    "situation",
    "threads",
    "characters",
    "assets_and_rules",
    "chronology_and_hooks",
)


class _ClosedMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RPMemoryFact(_ClosedMemoryModel):
    """One service-model fact grounded in committed Party RAW."""

    fact_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
    text: str = Field(min_length=1, max_length=2_000)
    status: Literal["active", "superseded", "retracted"] = "active"
    authority: Literal["player", "narrator", "inference"]
    source_turn_versions: tuple[int, ...] = Field(min_length=1, max_length=20)

    @field_validator("text")
    @classmethod
    def text_has_content(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("memory fact text must contain non-whitespace text")
        return value

    @model_validator(mode="after")
    def source_versions_are_ordered_and_unique(self) -> RPMemoryFact:
        if any(
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            for version in self.source_turn_versions
        ):
            raise ValueError("source_turn_versions must contain positive integers")
        if tuple(sorted(set(self.source_turn_versions))) != self.source_turn_versions:
            raise ValueError("source_turn_versions must be sorted and unique")
        return self


class _MemorySection(_ClosedMemoryModel):
    coverage: int = Field(ge=0)
    status: Literal["fresh", "stale", "failed"]

    def facts(self) -> tuple[RPMemoryFact, ...]:
        raise NotImplementedError

    @model_validator(mode="after")
    def facts_do_not_claim_future_turns(self) -> _MemorySection:
        if self.status == "failed" and self.coverage != 0:
            raise ValueError("failed memory sections cannot claim safe coverage")
        if any(
            version > self.coverage
            for fact in self.facts()
            for version in fact.source_turn_versions
        ):
            raise ValueError("memory facts cannot cite turns beyond section coverage")
        return self


class RPSituationMemory(_MemorySection):
    current_situation: RPMemoryFact | None = None
    canon: tuple[RPMemoryFact, ...] = ()

    def facts(self) -> tuple[RPMemoryFact, ...]:
        current = (self.current_situation,) if self.current_situation else ()
        return (*current, *self.canon)


class RPThreadsMemory(_MemorySection):
    active_threads: tuple[RPMemoryFact, ...] = ()
    resolved_threads: tuple[RPMemoryFact, ...] = ()

    def facts(self) -> tuple[RPMemoryFact, ...]:
        return (*self.active_threads, *self.resolved_threads)


class RPCharactersMemory(_MemorySection):
    characters: tuple[RPMemoryFact, ...] = ()

    def facts(self) -> tuple[RPMemoryFact, ...]:
        return self.characters


class RPAssetsAndRulesMemory(_MemorySection):
    inventory_and_assets: tuple[RPMemoryFact, ...] = ()
    rules_and_abilities: tuple[RPMemoryFact, ...] = ()

    def facts(self) -> tuple[RPMemoryFact, ...]:
        return (*self.inventory_and_assets, *self.rules_and_abilities)


class RPChronologyAndHooksMemory(_MemorySection):
    chronology: tuple[RPMemoryFact, ...] = ()
    unresolved_hooks: tuple[RPMemoryFact, ...] = ()

    def facts(self) -> tuple[RPMemoryFact, ...]:
        return (*self.chronology, *self.unresolved_hooks)


class RPStoryMemorySnapshot(_ClosedMemoryModel):
    """A complete append-only view whose safe coverage is the weakest section."""

    schema_version: Literal[RP_MEMORY_SCHEMA_VERSION]
    observed_through_version: int = Field(ge=0)
    situation: RPSituationMemory
    threads: RPThreadsMemory
    characters: RPCharactersMemory
    assets_and_rules: RPAssetsAndRulesMemory
    chronology_and_hooks: RPChronologyAndHooksMemory

    @property
    def safe_coverage(self) -> int:
        return min(section.coverage for section in self.sections().values())

    def sections(self) -> dict[str, _MemorySection]:
        return {
            "situation": self.situation,
            "threads": self.threads,
            "characters": self.characters,
            "assets_and_rules": self.assets_and_rules,
            "chronology_and_hooks": self.chronology_and_hooks,
        }

    @model_validator(mode="after")
    def coverages_and_fact_ids_are_consistent(self) -> RPStoryMemorySnapshot:
        sections = self.sections()
        if max(section.coverage for section in sections.values()) > self.observed_through_version:
            raise ValueError("section coverage cannot exceed observed Party version")
        fact_ids = [
            fact.fact_id
            for section in sections.values()
            for fact in section.facts()
        ]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("memory fact_id values must be unique across all sections")
        return self


@dataclass(frozen=True, slots=True)
class RPStoryMemoryRecord:
    id: int
    party_id: str
    revision: int
    base_snapshot_id: int | None
    update_id: str
    snapshot: RPStoryMemorySnapshot
    created_at: int


def canonical_memory_json(snapshot: RPStoryMemorySnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def memory_prompt_text(snapshot: RPStoryMemorySnapshot) -> str:
    """Render the single prompt representation whose persisted size is bounded."""
    lines = [
        "RP_STORY_MEMORY",
        f"safe_coverage={snapshot.safe_coverage}",
    ]
    sections = (
        (
            "situation",
            snapshot.situation,
            (
                ("current_situation", snapshot.situation.current_situation),
                ("canon", snapshot.situation.canon),
            ),
        ),
        (
            "threads",
            snapshot.threads,
            (
                ("active_threads", snapshot.threads.active_threads),
                ("resolved_threads", snapshot.threads.resolved_threads),
            ),
        ),
        (
            "characters",
            snapshot.characters,
            (("characters", snapshot.characters.characters),),
        ),
        (
            "assets_and_rules",
            snapshot.assets_and_rules,
            (
                ("inventory_and_assets", snapshot.assets_and_rules.inventory_and_assets),
                ("rules_and_abilities", snapshot.assets_and_rules.rules_and_abilities),
            ),
        ),
        (
            "chronology_and_hooks",
            snapshot.chronology_and_hooks,
            (
                ("chronology", snapshot.chronology_and_hooks.chronology),
                ("unresolved_hooks", snapshot.chronology_and_hooks.unresolved_hooks),
            ),
        ),
    )
    for section_key, section, section_fields in sections:
        lines.append(
            f"## {section_key} status={section.status} coverage={section.coverage}"
        )
        for field_name, value in section_fields:
            facts = (
                (value,)
                if value is not None and not isinstance(value, tuple)
                else value
            )
            for fact in facts or ():
                if fact.status == "active":
                    lines.append(f"- {field_name}: {fact.text}")
    return "\n".join(lines)
