"""Offline turn boundary for the rebuilt RP stack."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from app.rp.content import (
    ScenarioSnapshot,
    WorldSnapshot,
    canonical_snapshot_json,
    snapshot_hash,
)
from app.rp.memory import (
    RP_MEMORY_PROMPT_MAX_CHARS,
    RPStoryMemoryRecord,
    RPStoryMemorySnapshot,
    canonical_memory_json,
    memory_prompt_text,
)
from app.rp.schema import RPSchemaError, initialize_schema


class RPPartyNotFound(LookupError):
    """The requested party does not exist in the clean RP database."""


class RPIdempotencyConflict(RuntimeError):
    """A request identifier was reused for different immutable input."""


class RPPartyVersionConflict(RuntimeError):
    """The turn was based on an older or future party version."""


class RPPartySnapshotConflict(RuntimeError):
    """A party identifier was reused with different immutable source snapshots."""


class RPMemoryVersionConflict(RuntimeError):
    """Story memory was built from a snapshot that is no longer current."""


class RPMemoryIdempotencyConflict(RuntimeError):
    """A memory update identifier was reused for different immutable input."""


@dataclass(frozen=True, slots=True)
class RPParty:
    id: str
    owner_user_id: str
    world_snapshot: WorldSnapshot
    world_hash: str
    scenario_snapshot: ScenarioSnapshot
    scenario_hash: str
    current_version: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RPTurn:
    id: int
    party_id: str
    turn_kind: str
    request_id: str
    idempotency_key: str
    expected_version: int
    committed_version: int
    player_text: str
    narrator_text: str
    created_at: int


class RPTurnEngine:
    """Own a clean SQLite database and append complete offline RP turns atomically."""

    def __init__(self, sqlite_path: str | Path):
        self.sqlite_path = Path(sqlite_path)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            initialize_schema(connection)

    def create_party(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        world_snapshot: WorldSnapshot,
        scenario_snapshot: ScenarioSnapshot,
    ) -> RPParty:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        if world_snapshot.world_id != scenario_snapshot.world_id:
            raise ValueError("World and Scenario snapshots must target the same World")
        world_snapshot_json = canonical_snapshot_json(world_snapshot)
        world_snapshot_hash = snapshot_hash(world_snapshot)
        scenario_snapshot_json = canonical_snapshot_json(scenario_snapshot)
        scenario_snapshot_hash = snapshot_hash(scenario_snapshot)
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO rp_parties(
                        id, owner_user_id,
                        world_snapshot_json, world_hash,
                        scenario_snapshot_json, scenario_hash,
                        current_version, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (
                        party_id,
                        owner_user_id,
                        world_snapshot_json,
                        world_snapshot_hash,
                        scenario_snapshot_json,
                        scenario_snapshot_hash,
                        timestamp,
                        timestamp,
                    ),
                )
                party_row = _owned_party(connection, owner_user_id, party_id)
                if party_row is None:
                    raise RPPartyNotFound(party_id)
                party = _party_from_row(party_row)
                if (
                    party.world_snapshot != world_snapshot
                    or party.scenario_snapshot != scenario_snapshot
                ):
                    raise RPPartySnapshotConflict(
                        f"party {party_id!r} already owns different source snapshots"
                    )
                connection.commit()
                return party
            except Exception:
                connection.rollback()
                raise

    def get_party(self, *, owner_user_id: str, party_id: str) -> RPParty:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            party = _owned_party(connection, owner_user_id, party_id)
        if party is None:
            raise RPPartyNotFound(party_id)
        return _party_from_row(party)

    def commit_turn(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        player_text: str,
        narrator_text: str,
    ) -> RPTurn:
        """Append one complete RAW pair, returning the prior row on an exact retry."""
        return self._commit_raw(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind="narrative",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            player_text=player_text,
            narrator_text=narrator_text,
        )

    def commit_opening(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        request_id: str,
        idempotency_key: str,
        narrator_text: str,
    ) -> RPTurn:
        """Append the assistant-only opening as the first complete RAW unit."""
        return self._commit_raw(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind="opening_scene",
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=0,
            player_text="",
            narrator_text=narrator_text,
        )

    def _commit_raw(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        turn_kind: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        player_text: str,
        narrator_text: str,
    ) -> RPTurn:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        if turn_kind not in {"opening_scene", "narrative"}:
            raise ValueError("turn_kind must be opening_scene or narrative")
        request_id = _required_text(request_id, "request_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected_version must be a non-negative integer")
        if expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        if turn_kind == "narrative":
            player_text = _required_text(player_text, "player_text", preserve=True)
        elif player_text != "":
            raise ValueError("opening_scene cannot contain player_text")
        narrator_text = _required_text(narrator_text, "narrator_text", preserve=True)

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                party = _owned_party(connection, owner_user_id, party_id)
                if party is None:
                    raise RPPartyNotFound(party_id)

                existing = connection.execute(
                    """
                    SELECT turn.*
                    FROM rp_turns AS turn
                    JOIN rp_parties AS party ON party.id = turn.party_id
                    WHERE turn.party_id = ? AND party.owner_user_id = ?
                      AND turn.idempotency_key = ?
                    """,
                    (party_id, owner_user_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    turn = _turn_from_row(existing)
                    if (
                        turn.turn_kind != turn_kind
                        or turn.request_id != request_id
                        or turn.expected_version != expected_version
                        or turn.player_text != player_text
                        or turn.narrator_text != narrator_text
                    ):
                        raise RPIdempotencyConflict(
                            f"idempotency key {idempotency_key!r} already owns another turn"
                        )
                    connection.commit()
                    return turn

                request_owner = connection.execute(
                    "SELECT idempotency_key FROM rp_turns WHERE party_id = ? AND request_id = ?",
                    (party_id, request_id),
                ).fetchone()
                if request_owner is not None:
                    raise RPIdempotencyConflict(
                        f"request {request_id!r} is already committed with another key"
                    )

                current_version = int(party["current_version"])
                if current_version != expected_version:
                    raise RPPartyVersionConflict(
                        f"party {party_id!r} is at version {current_version}, "
                        f"not {expected_version}"
                    )
                committed_version = expected_version + 1
                created_at = time.time_ns()
                cursor = connection.execute(
                    """
                    INSERT INTO rp_turns(
                        party_id, turn_kind, request_id, idempotency_key,
                        expected_version, committed_version,
                        player_text, narrator_text, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_id,
                        turn_kind,
                        request_id,
                        idempotency_key,
                        expected_version,
                        committed_version,
                        player_text,
                        narrator_text,
                        created_at,
                    ),
                )
                advanced = connection.execute(
                    """
                    UPDATE rp_parties
                    SET current_version = ?, updated_at = ?
                    WHERE id = ? AND owner_user_id = ? AND current_version = ?
                    """,
                    (
                        committed_version,
                        created_at,
                        party_id,
                        owner_user_id,
                        expected_version,
                    ),
                ).rowcount
                if advanced != 1:
                    raise RuntimeError("party turn counter changed during commit")
                saved = connection.execute(
                    "SELECT * FROM rp_turns WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                if saved is None:
                    raise RuntimeError("committed RP turn could not be read back")
                connection.commit()
                return _turn_from_row(saved)
            except Exception:
                connection.rollback()
                raise

    def list_turns(self, *, owner_user_id: str, party_id: str) -> tuple[RPTurn, ...]:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            rows = connection.execute(
                "SELECT * FROM rp_turns WHERE party_id = ? ORDER BY committed_version",
                (party_id,),
            ).fetchall()
        return tuple(_turn_from_row(row) for row in rows)

    def latest_story_memory(
        self, *, owner_user_id: str, party_id: str
    ) -> RPStoryMemoryRecord | None:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            row = connection.execute(
                """
                SELECT * FROM rp_story_memory_snapshots
                WHERE party_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (party_id,),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def append_story_memory(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        expected_base_snapshot_id: int | None,
        update_id: str,
        snapshot: RPStoryMemorySnapshot,
    ) -> RPStoryMemoryRecord:
        """Append one validated memory view with an optimistic base pointer."""
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        update_id = _required_text(update_id, "update_id")
        if expected_base_snapshot_id is not None and (
            not isinstance(expected_base_snapshot_id, int)
            or isinstance(expected_base_snapshot_id, bool)
            or expected_base_snapshot_id <= 0
        ):
            raise ValueError("expected_base_snapshot_id must be a positive integer or None")
        snapshot_json = canonical_memory_json(snapshot)
        prompt_text = memory_prompt_text(snapshot)
        if len(prompt_text) > RP_MEMORY_PROMPT_MAX_CHARS:
            raise ValueError(
                "story memory exceeds its prompt budget: "
                f"{len(prompt_text)} > {RP_MEMORY_PROMPT_MAX_CHARS}"
            )
        coverages = {
            key: section.coverage for key, section in snapshot.sections().items()
        }

        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                party = _owned_party(connection, owner_user_id, party_id)
                if party is None:
                    raise RPPartyNotFound(party_id)
                if snapshot.observed_through_version > int(party["current_version"]):
                    raise ValueError("memory cannot observe beyond the current Party version")

                existing = connection.execute(
                    """
                    SELECT * FROM rp_story_memory_snapshots
                    WHERE party_id = ? AND update_id = ?
                    """,
                    (party_id, update_id),
                ).fetchone()
                if existing is not None:
                    record = _memory_from_row(existing)
                    if (
                        record.base_snapshot_id != expected_base_snapshot_id
                        or canonical_memory_json(record.snapshot) != snapshot_json
                    ):
                        raise RPMemoryIdempotencyConflict(
                            f"memory update {update_id!r} already owns another snapshot"
                        )
                    connection.commit()
                    return record

                latest_row = connection.execute(
                    """
                    SELECT * FROM rp_story_memory_snapshots
                    WHERE party_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (party_id,),
                ).fetchone()
                latest = _memory_from_row(latest_row) if latest_row is not None else None
                latest_id = latest.id if latest is not None else None
                if latest_id != expected_base_snapshot_id:
                    raise RPMemoryVersionConflict(
                        f"memory base is {latest_id!r}, not {expected_base_snapshot_id!r}"
                    )
                latest_sections = (
                    latest.snapshot.sections() if latest is not None else {}
                )
                for key, section in snapshot.sections().items():
                    if section.status != "stale":
                        continue
                    previous = latest_sections.get(key)
                    if previous is None:
                        if section.coverage != 0:
                            raise ValueError(
                                f"stale memory section {key!r} cannot advance without a base"
                            )
                    elif section.model_dump(exclude={"status"}) != previous.model_dump(
                        exclude={"status"}
                    ):
                        raise ValueError(
                            f"stale memory section {key!r} must preserve its base content"
                        )
                if latest is not None:
                    if (
                        snapshot.observed_through_version
                        < latest.snapshot.observed_through_version
                    ):
                        raise ValueError(
                            "memory observed Party version cannot regress"
                        )
                    previous_coverages = {
                        key: section.coverage
                        for key, section in latest_sections.items()
                    }
                    regressed = [
                        key
                        for key in coverages
                        if coverages[key] < previous_coverages[key]
                    ]
                    if regressed:
                        raise ValueError(
                            "memory section coverage cannot regress: "
                            + ", ".join(sorted(regressed))
                        )

                revision = (latest.revision if latest is not None else 0) + 1
                created_at = time.time_ns()
                cursor = connection.execute(
                    """
                    INSERT INTO rp_story_memory_snapshots(
                        party_id, revision, base_snapshot_id, update_id,
                        snapshot_json, observed_through_version,
                        situation_coverage, threads_coverage, characters_coverage,
                        assets_and_rules_coverage, chronology_and_hooks_coverage,
                        created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_id,
                        revision,
                        latest_id,
                        update_id,
                        snapshot_json,
                        snapshot.observed_through_version,
                        coverages["situation"],
                        coverages["threads"],
                        coverages["characters"],
                        coverages["assets_and_rules"],
                        coverages["chronology_and_hooks"],
                        created_at,
                    ),
                )
                saved = connection.execute(
                    "SELECT * FROM rp_story_memory_snapshots WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                if saved is None:
                    raise RuntimeError("story-memory snapshot could not be read back")
                connection.commit()
                return _memory_from_row(saved)
            except Exception:
                connection.rollback()
                raise

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.sqlite_path, isolation_level=None, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _required_text(value: str, field: str, *, preserve: bool = False) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value if preserve else value.strip()


def _owned_party(
    connection: sqlite3.Connection, owner_user_id: str, party_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM rp_parties WHERE id = ? AND owner_user_id = ?",
        (party_id, owner_user_id),
    ).fetchone()


def _party_from_row(row: sqlite3.Row) -> RPParty:
    world_snapshot_json = str(row["world_snapshot_json"])
    scenario_snapshot_json = str(row["scenario_snapshot_json"])
    try:
        world_snapshot = WorldSnapshot.model_validate_json(world_snapshot_json)
        scenario_snapshot = ScenarioSnapshot.model_validate_json(scenario_snapshot_json)
    except ValidationError as exc:
        raise RPSchemaError("stored RP source snapshot is invalid") from exc
    world_snapshot_hash = str(row["world_hash"])
    scenario_snapshot_hash = str(row["scenario_hash"])
    if (
        canonical_snapshot_json(world_snapshot) != world_snapshot_json
        or snapshot_hash(world_snapshot) != world_snapshot_hash
    ):
        raise RPSchemaError("stored World snapshot does not match its hash")
    if (
        canonical_snapshot_json(scenario_snapshot) != scenario_snapshot_json
        or snapshot_hash(scenario_snapshot) != scenario_snapshot_hash
    ):
        raise RPSchemaError("stored Scenario snapshot does not match its hash")
    return RPParty(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]),
        world_snapshot=world_snapshot,
        world_hash=world_snapshot_hash,
        scenario_snapshot=scenario_snapshot,
        scenario_hash=scenario_snapshot_hash,
        current_version=int(row["current_version"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> RPTurn:
    return RPTurn(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        turn_kind=str(row["turn_kind"]),
        request_id=str(row["request_id"]),
        idempotency_key=str(row["idempotency_key"]),
        expected_version=int(row["expected_version"]),
        committed_version=int(row["committed_version"]),
        player_text=str(row["player_text"]),
        narrator_text=str(row["narrator_text"]),
        created_at=int(row["created_at"]),
    )


def _memory_from_row(row: sqlite3.Row) -> RPStoryMemoryRecord:
    snapshot_json = str(row["snapshot_json"])
    try:
        snapshot = RPStoryMemorySnapshot.model_validate_json(snapshot_json)
    except ValidationError as exc:
        raise RPSchemaError("stored RP story-memory snapshot is invalid") from exc
    if canonical_memory_json(snapshot) != snapshot_json:
        raise RPSchemaError("stored RP story-memory snapshot is not canonical")
    expected = {
        "observed_through_version": snapshot.observed_through_version,
        **{
            f"{key}_coverage": section.coverage
            for key, section in snapshot.sections().items()
        },
    }
    if any(int(row[column]) != value for column, value in expected.items()):
        raise RPSchemaError("stored RP story-memory coverage columns do not match JSON")
    return RPStoryMemoryRecord(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        revision=int(row["revision"]),
        base_snapshot_id=(
            int(row["base_snapshot_id"])
            if row["base_snapshot_id"] is not None
            else None
        ),
        update_id=str(row["update_id"]),
        snapshot=snapshot,
        created_at=int(row["created_at"]),
    )
