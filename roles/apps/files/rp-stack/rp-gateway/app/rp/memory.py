"""Bounded narrative story memory for the isolated RP engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator


RP_MEMORY_SCHEMA_VERSION = "rp-story-memory.v2"
RP_MEMORY_RAW_WINDOW_TURNS = 50
RP_MEMORY_BATCH_TURNS = 8
RP_MEMORY_RAW_CHUNK_MAX_CHARS = 2_000
RP_MEMORY_CHUNK_MAX_CHARS = 6_000
RP_MEMORY_PROMPT_MAX_CHARS = 130_000
RP_MEMORY_HIERARCHY_CONTEXT_CHARS = 130_000
RP_MEMORY_ARCHIVE_SOURCE_MAX_CHARS = 800_000

_NarrativeText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=RP_MEMORY_CHUNK_MAX_CHARS,
    ),
]


class _ClosedMemoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RPStoryMemoryChunk(_ClosedMemoryModel):
    """One prose summary covering one complete base-eight history span."""

    start_version: int = Field(gt=0)
    end_version: int = Field(gt=0)
    level: int = Field(gt=0)
    narrative: _NarrativeText

    @model_validator(mode="after")
    def span_matches_level(self) -> RPStoryMemoryChunk:
        expected_size = RP_MEMORY_BATCH_TURNS**self.level
        if self.end_version - self.start_version + 1 != expected_size:
            raise ValueError(
                "story-memory chunk span must match its base-eight level"
            )
        return self


class RPStoryMemorySnapshot(_ClosedMemoryModel):
    """The active prose-summary frontier; committed RAW remains authoritative."""

    schema_version: Literal[RP_MEMORY_SCHEMA_VERSION]
    covered_through_version: int = Field(ge=0)
    chunks: tuple[RPStoryMemoryChunk, ...] = ()

    @property
    def safe_coverage(self) -> int:
        return self.covered_through_version

    @model_validator(mode="after")
    def chunks_are_ordered_contiguous_and_match_coverage(
        self,
    ) -> RPStoryMemorySnapshot:
        expected_start = 1
        for chunk in self.chunks:
            if chunk.start_version != expected_start:
                raise ValueError(
                    "story-memory chunks must cover committed versions contiguously"
                )
            expected_start = chunk.end_version + 1
        expected_coverage = self.chunks[-1].end_version if self.chunks else 0
        if self.covered_through_version != expected_coverage:
            raise ValueError(
                "story-memory coverage must equal the active chunk frontier"
            )
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
    """Render only the active narrative chunks for the Narrator prompt."""
    lines = [
        "RP_STORY_MEMORY",
        f"covered_through_version={snapshot.covered_through_version}",
    ]
    for chunk in snapshot.chunks:
        lines.extend(
            (
                "",
                (
                    f"[SUMMARY turns {chunk.start_version}-{chunk.end_version}; "
                    f"level={chunk.level}]"
                ),
                chunk.narrative,
            )
        )
    return "\n".join(lines)
