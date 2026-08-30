"""Offline turn boundary for the rebuilt RP stack."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


RP_JOB_MAX_ATTEMPTS = 3
RP_ADMINISTRATOR_WINDOW_TURNS = 8


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


class RPBackgroundJobConflict(RuntimeError):
    """A background job changed state or is owned by another runner claim."""


class RPModelOutputRejected(RuntimeError):
    """A model result failed deterministic validation and must not auto-retry."""


class RPAdministratorProposalConflict(RuntimeError):
    """An Administrator proposal cannot be decided from the current Party state."""


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


@dataclass(frozen=True, slots=True)
class RPNarrationRequest:
    id: int
    party_id: str
    turn_kind: str
    request_id: str
    idempotency_key: str
    expected_version: int
    player_text: str
    status: str
    claim_token: str | None
    turn_id: int | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RPNarrationClaim:
    acquired: bool
    request: RPNarrationRequest
    turn: RPTurn | None = None


@dataclass(frozen=True, slots=True)
class RPServiceJob:
    id: int
    owner_user_id: str
    party_id: str
    job_type: str
    source_turn_id: int
    source_version: int
    status: str
    attempts: int
    max_attempts: int
    claim_token: str | None
    result: dict[str, Any] | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RPAdministratorJob:
    id: int
    owner_user_id: str
    party_id: str
    source_turn_id: int
    source_version: int
    window_start_version: int
    window_end_version: int
    window_hash: str
    evidence_versions: tuple[int, ...]
    status: str
    attempts: int
    max_attempts: int
    claim_token: str | None
    result: dict[str, Any] | None
    last_error: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RPRelationshipCause:
    id: int
    party_id: str
    service_job_id: int
    source_turn_id: int
    source_version: int
    candidate_key: str
    character_id: str
    direction: str
    event_id: str
    axis: str
    delta: int
    evidence_span_ids: tuple[int, ...]
    created_at: int


@dataclass(frozen=True, slots=True)
class RPRuntimeLoreCard:
    id: int
    party_id: str
    service_job_id: int
    source_turn_id: int
    source_version: int
    card_key: str
    kind: str
    origin: str
    title: str
    content: str
    keywords: tuple[str, ...]
    evidence_span_ids: tuple[int, ...]
    enabled: bool
    created_at: int


@dataclass(frozen=True, slots=True)
class RPAdministratorProposal:
    id: int
    party_id: str
    administrator_job_id: int
    kind: str
    target_slot: str
    before_text: str
    after_text: str
    base_party_version: int
    evidence_versions: tuple[int, ...]
    window_hash: str
    status: str
    applied_party_version: int | None
    created_at: int
    decided_at: int | None


@dataclass(frozen=True, slots=True)
class RPAdministratorGuidance:
    id: int
    party_id: str
    proposal_id: int
    revision: int
    party_version: int
    content: str
    created_at: int


@dataclass(frozen=True, slots=True)
class RPDerivedContext:
    relationship_causes: tuple[RPRelationshipCause, ...]
    runtime_lore_cards: tuple[RPRuntimeLoreCard, ...]
    administrator_guidance: RPAdministratorGuidance | None


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

    def claim_narration(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        turn_kind: str,
        request_id: str,
        idempotency_key: str,
        expected_version: int,
        player_text: str,
    ) -> RPNarrationClaim:
        """Persist the synchronous Narrator claim before prompt/provider work."""
        (
            owner_user_id,
            party_id,
            request_id,
            idempotency_key,
            player_text,
        ) = _narration_inputs(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind=turn_kind,
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            player_text=player_text,
        )
        claim_token = uuid.uuid4().hex
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                party = _owned_party(connection, owner_user_id, party_id)
                if party is None:
                    raise RPPartyNotFound(party_id)
                existing_rows = connection.execute(
                    """
                    SELECT * FROM rp_narration_requests
                    WHERE party_id = ? AND (request_id = ? OR idempotency_key = ?)
                    ORDER BY id
                    """,
                    (party_id, request_id, idempotency_key),
                ).fetchall()
                if existing_rows:
                    if len(existing_rows) != 1:
                        raise RPIdempotencyConflict(
                            "request_id and idempotency_key belong to different narration requests"
                        )
                    existing = _narration_request_from_row(existing_rows[0])
                    _assert_same_narration_request(
                        existing,
                        turn_kind=turn_kind,
                        request_id=request_id,
                        idempotency_key=idempotency_key,
                        expected_version=expected_version,
                        player_text=player_text,
                    )
                    if existing.status == "succeeded":
                        turn_row = connection.execute(
                            "SELECT * FROM rp_turns WHERE id = ?",
                            (existing.turn_id,),
                        ).fetchone()
                        if turn_row is None:
                            raise RPSchemaError(
                                "succeeded narration request has no committed turn"
                            )
                        connection.commit()
                        return RPNarrationClaim(
                            acquired=False,
                            request=existing,
                            turn=_turn_from_row(turn_row),
                        )
                    if existing.status == "running":
                        connection.commit()
                        return RPNarrationClaim(acquired=False, request=existing)
                    reclaimed = connection.execute(
                        """
                        UPDATE rp_narration_requests
                        SET status = 'running', claim_token = ?, last_error = NULL,
                            updated_at = ?
                        WHERE id = ? AND status = 'failed'
                        RETURNING *
                        """,
                        (claim_token, timestamp, existing.id),
                    ).fetchone()
                    if reclaimed is None:
                        raise RuntimeError("narration request changed during reclaim")
                    connection.commit()
                    return RPNarrationClaim(
                        acquired=True,
                        request=_narration_request_from_row(reclaimed),
                    )

                current_version = int(party["current_version"])
                if current_version != expected_version:
                    raise RPPartyVersionConflict(
                        f"party {party_id!r} is at version {current_version}, "
                        f"not {expected_version}"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO rp_narration_requests(
                        party_id, turn_kind, request_id, idempotency_key,
                        expected_version, player_text, status, claim_token,
                        created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'running', ?, ?, ?)
                    """,
                    (
                        party_id,
                        turn_kind,
                        request_id,
                        idempotency_key,
                        expected_version,
                        player_text,
                        claim_token,
                        timestamp,
                        timestamp,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM rp_narration_requests WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("narration claim could not be read back")
                connection.commit()
                return RPNarrationClaim(
                    acquired=True,
                    request=_narration_request_from_row(row),
                )
            except Exception:
                connection.rollback()
                raise

    def get_narration_request(
        self, *, owner_user_id: str, party_id: str, idempotency_key: str
    ) -> RPNarrationRequest:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            row = connection.execute(
                """
                SELECT * FROM rp_narration_requests
                WHERE party_id = ? AND idempotency_key = ?
                """,
                (party_id, idempotency_key),
            ).fetchone()
        if row is None:
            raise LookupError(idempotency_key)
        return _narration_request_from_row(row)

    def fail_narration(self, *, request_id: int, claim_token: str, error: str) -> None:
        claim_token = _required_text(claim_token, "claim_token")
        error = _required_text(error, "error", preserve=True)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE rp_narration_requests
                SET status = 'failed', claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (error, time.time_ns(), request_id, claim_token),
            ).rowcount
        if changed != 1:
            raise RPBackgroundJobConflict("narration claim is no longer owned")

    def complete_narration(
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
        claim_token: str,
    ) -> RPTurn:
        return self._commit_raw(
            owner_user_id=owner_user_id,
            party_id=party_id,
            turn_kind=turn_kind,
            request_id=request_id,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
            player_text=player_text,
            narrator_text=narrator_text,
            narration_claim_token=_required_text(claim_token, "claim_token"),
        )

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
        narration_claim_token: str | None = None,
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

                narration_request: sqlite3.Row | None = None
                if narration_claim_token is not None:
                    narration_request = connection.execute(
                        """
                        SELECT * FROM rp_narration_requests
                        WHERE party_id = ? AND idempotency_key = ?
                          AND status = 'running' AND claim_token = ?
                        """,
                        (party_id, idempotency_key, narration_claim_token),
                    ).fetchone()
                    if narration_request is None:
                        raise RPBackgroundJobConflict(
                            "narration claim is no longer owned"
                        )
                    _assert_same_narration_request(
                        _narration_request_from_row(narration_request),
                        turn_kind=turn_kind,
                        request_id=request_id,
                        idempotency_key=idempotency_key,
                        expected_version=expected_version,
                        player_text=player_text,
                    )

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
                    if narration_request is not None:
                        connection.execute(
                            """
                            UPDATE rp_narration_requests
                            SET status = 'succeeded', claim_token = NULL, turn_id = ?,
                                last_error = NULL, updated_at = ?
                            WHERE id = ? AND status = 'running' AND claim_token = ?
                            """,
                            (
                                turn.id,
                                time.time_ns(),
                                int(narration_request["id"]),
                                narration_claim_token,
                            ),
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
                turn_id = int(cursor.lastrowid)
                _enqueue_post_turn_work(
                    connection,
                    party_id=party_id,
                    turn_id=turn_id,
                    committed_version=committed_version,
                    created_at=created_at,
                )
                if narration_request is not None:
                    completed = connection.execute(
                        """
                        UPDATE rp_narration_requests
                        SET status = 'succeeded', claim_token = NULL, turn_id = ?,
                            last_error = NULL, updated_at = ?
                        WHERE id = ? AND status = 'running' AND claim_token = ?
                        """,
                        (
                            turn_id,
                            created_at,
                            int(narration_request["id"]),
                            narration_claim_token,
                        ),
                    ).rowcount
                    if completed != 1:
                        raise RuntimeError("narration claim changed during commit")
                saved = connection.execute(
                    "SELECT * FROM rp_turns WHERE id = ?",
                    (turn_id,),
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

    def story_memory_by_update_id(
        self, *, owner_user_id: str, party_id: str, update_id: str
    ) -> RPStoryMemoryRecord | None:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        update_id = _required_text(update_id, "update_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            row = connection.execute(
                """
                SELECT * FROM rp_story_memory_snapshots
                WHERE party_id = ? AND update_id = ?
                """,
                (party_id, update_id),
            ).fetchone()
        return _memory_from_row(row) if row is not None else None

    def recover_interrupted_work(self) -> dict[str, int]:
        """Release abandoned work without counting a provider/model failure."""
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                narration = connection.execute(
                    """
                    UPDATE rp_narration_requests
                    SET status = 'failed', claim_token = NULL,
                        last_error = 'interrupted before completion', updated_at = ?
                    WHERE status = 'running'
                    """,
                    (timestamp,),
                ).rowcount
                service = connection.execute(
                    """
                    UPDATE rp_service_jobs
                    SET status = 'pending', claim_token = NULL, updated_at = ?
                    WHERE status = 'running'
                    """,
                    (timestamp,),
                ).rowcount
                administrator = connection.execute(
                    """
                    UPDATE rp_administrator_jobs
                    SET status = 'pending', claim_token = NULL, updated_at = ?
                    WHERE status = 'running'
                    """,
                    (timestamp,),
                ).rowcount
                connection.commit()
                return {
                    "narration_requests": narration,
                    "service_jobs": service,
                    "administrator_jobs": administrator,
                }
            except Exception:
                connection.rollback()
                raise

    def claim_service_job(self) -> RPServiceJob | None:
        claim_token = uuid.uuid4().hex
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    UPDATE rp_service_jobs
                    SET status = 'running', claim_token = ?, updated_at = ?
                    WHERE id = (
                        SELECT candidate.id FROM rp_service_jobs AS candidate
                        WHERE candidate.status = 'pending'
                          AND candidate.attempts < candidate.max_attempts
                          AND NOT EXISTS (
                              SELECT 1 FROM rp_service_jobs AS earlier
                              WHERE earlier.party_id = candidate.party_id
                                AND earlier.job_type = candidate.job_type
                                AND earlier.source_version < candidate.source_version
                                AND earlier.status IN ('pending', 'running')
                          )
                        ORDER BY candidate.id LIMIT 1
                    ) AND status = 'pending'
                    RETURNING *
                    """,
                    (claim_token, time.time_ns()),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                owner_user_id = _owner_for_party(connection, str(row["party_id"]))
                connection.commit()
                return _service_job_from_row(row, owner_user_id)
            except Exception:
                connection.rollback()
                raise

    def claim_administrator_job(self) -> RPAdministratorJob | None:
        claim_token = uuid.uuid4().hex
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    UPDATE rp_administrator_jobs
                    SET status = 'running', claim_token = ?, updated_at = ?
                    WHERE id = (
                        SELECT candidate.id
                        FROM rp_administrator_jobs AS candidate
                        WHERE candidate.status = 'pending'
                          AND candidate.attempts < candidate.max_attempts
                          AND NOT EXISTS (
                              SELECT 1 FROM rp_administrator_jobs AS earlier
                              WHERE earlier.party_id = candidate.party_id
                                AND earlier.source_version < candidate.source_version
                                AND earlier.status IN ('pending', 'running')
                          )
                        ORDER BY candidate.id LIMIT 1
                    ) AND status = 'pending'
                    RETURNING *
                    """,
                    (claim_token, time.time_ns()),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                owner_user_id = _owner_for_party(connection, str(row["party_id"]))
                connection.commit()
                return _administrator_job_from_row(row, owner_user_id)
            except Exception:
                connection.rollback()
                raise

    def complete_service_job(
        self, *, job_id: int, claim_token: str, result: dict[str, Any] | None = None
    ) -> RPServiceJob:
        return self._complete_service_job(
            job_id=job_id, claim_token=claim_token, result=result
        )

    def complete_administrator_job(
        self, *, job_id: int, claim_token: str, result: dict[str, Any] | None = None
    ) -> RPAdministratorJob:
        claim_token = _required_text(claim_token, "claim_token")
        result_json = _canonical_json(result) if result is not None else None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                UPDATE rp_administrator_jobs
                SET status = 'succeeded', claim_token = NULL,
                    result_json = COALESCE(?, result_json), last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                RETURNING *
                """,
                (result_json, time.time_ns(), job_id, claim_token),
            ).fetchone()
            if row is None:
                raise RPBackgroundJobConflict("administrator job is no longer owned")
            owner_user_id = _owner_for_party(connection, str(row["party_id"]))
        return _administrator_job_from_row(row, owner_user_id)

    def fail_service_job(
        self,
        *,
        job_id: int,
        claim_token: str,
        error: str,
        retryable: bool = True,
    ) -> RPServiceJob:
        claim_token = _required_text(claim_token, "claim_token")
        error = _required_text(error, "error", preserve=True)
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a boolean")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                UPDATE rp_service_jobs
                SET attempts = attempts + 1,
                    status = CASE
                        WHEN ? = 0 OR attempts + 1 >= max_attempts THEN 'failed'
                        ELSE 'pending'
                    END,
                    claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                RETURNING *
                """,
                (int(retryable), error, time.time_ns(), job_id, claim_token),
            ).fetchone()
            if row is None:
                raise RPBackgroundJobConflict("service job is no longer owned")
            owner_user_id = _owner_for_party(connection, str(row["party_id"]))
        return _service_job_from_row(row, owner_user_id)

    def fail_administrator_job(
        self,
        *,
        job_id: int,
        claim_token: str,
        error: str,
        retryable: bool = True,
    ) -> RPAdministratorJob:
        claim_token = _required_text(claim_token, "claim_token")
        error = _required_text(error, "error", preserve=True)
        if not isinstance(retryable, bool):
            raise ValueError("retryable must be a boolean")
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                UPDATE rp_administrator_jobs
                SET attempts = attempts + 1,
                    status = CASE
                        WHEN ? = 0 OR attempts + 1 >= max_attempts THEN 'failed'
                        ELSE 'pending'
                    END,
                    claim_token = NULL, last_error = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                RETURNING *
                """,
                (int(retryable), error, time.time_ns(), job_id, claim_token),
            ).fetchone()
            if row is None:
                raise RPBackgroundJobConflict("administrator job is no longer owned")
            owner_user_id = _owner_for_party(connection, str(row["party_id"]))
        return _administrator_job_from_row(row, owner_user_id)

    def release_service_job(self, *, job_id: int, claim_token: str) -> None:
        self._release_job("rp_service_jobs", job_id=job_id, claim_token=claim_token)

    def release_administrator_job(self, *, job_id: int, claim_token: str) -> None:
        self._release_job(
            "rp_administrator_jobs", job_id=job_id, claim_token=claim_token
        )

    def list_service_jobs(
        self, *, owner_user_id: str, party_id: str
    ) -> tuple[RPServiceJob, ...]:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            rows = connection.execute(
                "SELECT * FROM rp_service_jobs WHERE party_id = ? ORDER BY id",
                (party_id,),
            ).fetchall()
        return tuple(_service_job_from_row(row, owner_user_id) for row in rows)

    def list_administrator_jobs(
        self, *, owner_user_id: str, party_id: str
    ) -> tuple[RPAdministratorJob, ...]:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            rows = connection.execute(
                "SELECT * FROM rp_administrator_jobs WHERE party_id = ? ORDER BY id",
                (party_id,),
            ).fetchall()
        return tuple(_administrator_job_from_row(row, owner_user_id) for row in rows)

    def source_turn_for_service_job(self, job: RPServiceJob) -> RPTurn:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM rp_turns WHERE id = ? AND party_id = ?",
                (job.source_turn_id, job.party_id),
            ).fetchone()
        if row is None:
            raise RPSchemaError("service job source turn is missing")
        return _turn_from_row(row)

    def source_turns_for_administrator_job(
        self, job: RPAdministratorJob
    ) -> tuple[RPTurn, ...]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM rp_turns
                WHERE party_id = ? AND committed_version BETWEEN ? AND ?
                ORDER BY committed_version
                """,
                (job.party_id, job.window_start_version, job.window_end_version),
            ).fetchall()
        turns = tuple(_turn_from_row(row) for row in rows)
        if tuple(turn.committed_version for turn in turns) != job.evidence_versions:
            raise RPSchemaError("administrator job evidence window changed")
        return turns

    def _complete_service_job(
        self, *, job_id: int, claim_token: str, result: dict[str, Any] | None
    ) -> RPServiceJob:
        claim_token = _required_text(claim_token, "claim_token")
        result_json = _canonical_json(result) if result is not None else None
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                UPDATE rp_service_jobs
                SET status = 'succeeded', claim_token = NULL,
                    result_json = COALESCE(?, result_json), last_error = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                RETURNING *
                """,
                (result_json, time.time_ns(), job_id, claim_token),
            ).fetchone()
            if row is None:
                raise RPBackgroundJobConflict("service job is no longer owned")
            owner_user_id = _owner_for_party(connection, str(row["party_id"]))
        return _service_job_from_row(row, owner_user_id)

    def _release_job(self, table: str, *, job_id: int, claim_token: str) -> None:
        if table not in {"rp_service_jobs", "rp_administrator_jobs"}:
            raise ValueError("unknown RP job table")
        claim_token = _required_text(claim_token, "claim_token")
        with closing(self._connect()) as connection:
            changed = connection.execute(
                f"""
                UPDATE {table}
                SET status = 'pending', claim_token = NULL, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (time.time_ns(), job_id, claim_token),
            ).rowcount
        if changed != 1:
            raise RPBackgroundJobConflict("job is no longer owned")

    def record_service_job_result(
        self, *, job: RPServiceJob, result: dict[str, Any]
    ) -> dict[str, Any]:
        result_json = _canonical_json(result)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE rp_service_jobs SET result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (result_json, time.time_ns(), job.id, job.claim_token),
            ).rowcount
        if changed != 1:
            raise RPBackgroundJobConflict("service job is no longer owned")
        return json.loads(result_json)

    def persist_relationship_result(
        self,
        *,
        job: RPServiceJob,
        causes: tuple[dict[str, Any], ...],
        rejected: tuple[dict[str, Any], ...],
    ) -> dict[str, Any]:
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owned = _owned_running_job(
                    connection, "rp_service_jobs", job.id, job.claim_token
                )
                if owned["result_json"] is not None:
                    connection.commit()
                    return _json_object(str(owned["result_json"]), "service job result")
                inserted = 0
                accepted = 0
                persisted_rejected = list(rejected)
                for cause in causes:
                    character_id = str(cause["character_id"])
                    axis = str(cause["axis"])
                    minimum = int(cause["axis_min"])
                    maximum = int(cause["axis_max"])
                    if minimum >= maximum:
                        raise ValueError("invalid persisted relationship axis range")
                    row = connection.execute(
                        """
                        SELECT COALESCE(SUM(delta), 0) AS total
                        FROM rp_relationship_causes
                        WHERE party_id = ? AND character_id = ? AND axis = ?
                        """,
                        (job.party_id, character_id, axis),
                    ).fetchone()
                    total = int(row["total"]) if row is not None else 0
                    current = max(
                        minimum,
                        min(maximum, int(cause["seed_value"]) + total),
                    )
                    requested_delta = int(cause["delta"])
                    bounded_value = max(
                        minimum, min(maximum, current + requested_delta)
                    )
                    persisted_delta = bounded_value - current
                    if persisted_delta == 0:
                        persisted_rejected.append(
                            {
                                "index": int(cause["candidate_index"]),
                                "reason": "relationship_range_reached",
                            }
                        )
                        continue
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO rp_relationship_causes(
                            party_id, service_job_id, source_turn_id, source_version,
                            candidate_key, character_id, direction, event_id, axis,
                            delta, evidence_span_ids_json, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 'character_to_player', ?, ?, ?, ?, ?)
                        """,
                        (
                            job.party_id,
                            job.id,
                            job.source_turn_id,
                            job.source_version,
                            str(cause["candidate_key"]),
                            character_id,
                            str(cause["event_id"]),
                            axis,
                            persisted_delta,
                            _canonical_json(list(cause["evidence_span_ids"])),
                            timestamp,
                        ),
                    )
                    inserted += cursor.rowcount
                    accepted += cursor.rowcount
                result = {
                    "kind": "relationships",
                    "accepted": accepted,
                    "inserted": inserted,
                    "rejected": persisted_rejected,
                }
                result_json = _canonical_json(result)
                changed = connection.execute(
                    """
                    UPDATE rp_service_jobs SET result_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (result_json, timestamp, job.id, job.claim_token),
                ).rowcount
                if changed != 1:
                    raise RPBackgroundJobConflict("service job is no longer owned")
                connection.commit()
                return json.loads(result_json)
            except Exception:
                connection.rollback()
                raise

    def persist_runtime_lore_result(
        self, *, job: RPServiceJob, card: dict[str, Any] | None
    ) -> dict[str, Any]:
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owned = _owned_running_job(
                    connection, "rp_service_jobs", job.id, job.claim_token
                )
                if owned["result_json"] is not None:
                    connection.commit()
                    return _json_object(str(owned["result_json"]), "service job result")
                card_id: int | None = None
                if card is not None:
                    cursor = connection.execute(
                        """
                        INSERT OR IGNORE INTO rp_runtime_lore_cards(
                            party_id, service_job_id, source_turn_id, source_version,
                            card_key, kind, origin, title, content, keywords_json,
                            evidence_span_ids_json, enabled, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?, 'runtime', ?, ?, ?, ?, 1, ?)
                        """,
                        (
                            job.party_id,
                            job.id,
                            job.source_turn_id,
                            job.source_version,
                            str(card["card_key"]),
                            str(card["kind"]),
                            str(card["title"]),
                            str(card["content"]),
                            _canonical_json(list(card["keywords"])),
                            _canonical_json(list(card["evidence_span_ids"])),
                            timestamp,
                        ),
                    )
                    if cursor.rowcount == 1:
                        card_id = int(cursor.lastrowid)
                    else:
                        row = connection.execute(
                            """
                            SELECT id FROM rp_runtime_lore_cards
                            WHERE party_id = ? AND card_key = ?
                            """,
                            (job.party_id, str(card["card_key"])),
                        ).fetchone()
                        if row is not None:
                            card_id = int(row["id"])
                result = {
                    "kind": "runtime_lore",
                    "result": "draft" if card is not None else "no_candidate",
                    "card_id": card_id,
                }
                result_json = _canonical_json(result)
                changed = connection.execute(
                    """
                    UPDATE rp_service_jobs SET result_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (result_json, timestamp, job.id, job.claim_token),
                ).rowcount
                if changed != 1:
                    raise RPBackgroundJobConflict("service job is no longer owned")
                connection.commit()
                return json.loads(result_json)
            except Exception:
                connection.rollback()
                raise

    def create_administrator_proposal(
        self,
        *,
        job: RPAdministratorJob,
        after_text: str,
        expected_base_party_version: int,
        expected_before_text: str,
    ) -> RPAdministratorProposal | None:
        after_text = _required_text(after_text, "after_text", preserve=True)
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                owned = _owned_running_job(
                    connection, "rp_administrator_jobs", job.id, job.claim_token
                )
                existing = connection.execute(
                    """
                    SELECT * FROM rp_administrator_proposals
                    WHERE party_id = ? AND administrator_job_id = ?
                    """,
                    (job.party_id, job.id),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return _administrator_proposal_from_row(existing)
                party = connection.execute(
                    "SELECT * FROM rp_parties WHERE id = ?",
                    (job.party_id,),
                ).fetchone()
                if party is None:
                    raise RPPartyNotFound(job.party_id)
                guidance = connection.execute(
                    """
                    SELECT * FROM rp_administrator_guidance
                    WHERE party_id = ? ORDER BY revision DESC LIMIT 1
                    """,
                    (job.party_id,),
                ).fetchone()
                before_text = str(guidance["content"]) if guidance is not None else ""
                if (
                    int(party["current_version"]) != expected_base_party_version
                    or before_text != expected_before_text
                ):
                    result = {
                        "kind": "administrator",
                        "result": "stale_review",
                    }
                    changed = connection.execute(
                        """
                        UPDATE rp_administrator_jobs SET result_json = ?, updated_at = ?
                        WHERE id = ? AND status = 'running' AND claim_token = ?
                        """,
                        (_canonical_json(result), timestamp, job.id, job.claim_token),
                    ).rowcount
                    if changed != 1:
                        raise RPBackgroundJobConflict(
                            "administrator job is no longer owned"
                        )
                    connection.commit()
                    return None
                cursor = connection.execute(
                    """
                    INSERT INTO rp_administrator_proposals(
                        party_id, administrator_job_id, kind, target_slot,
                        before_text, after_text, base_party_version,
                        evidence_versions_json, window_hash, status, created_at
                    ) VALUES(?, ?, 'narrator_guidance', 'narrator_guidance',
                             ?, ?, ?, ?, ?, 'pending', ?)
                    """,
                    (
                        job.party_id,
                        job.id,
                        before_text,
                        after_text,
                        expected_base_party_version,
                        _canonical_json(list(job.evidence_versions)),
                        job.window_hash,
                        timestamp,
                    ),
                )
                result = {
                    "kind": "administrator",
                    "result": "suggest",
                    "proposal_id": int(cursor.lastrowid),
                }
                changed = connection.execute(
                    """
                    UPDATE rp_administrator_jobs SET result_json = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND claim_token = ?
                    """,
                    (_canonical_json(result), timestamp, job.id, job.claim_token),
                ).rowcount
                if changed != 1 or owned is None:
                    raise RPBackgroundJobConflict("administrator job is no longer owned")
                row = connection.execute(
                    "SELECT * FROM rp_administrator_proposals WHERE id = ?",
                    (int(cursor.lastrowid),),
                ).fetchone()
                if row is None:
                    raise RuntimeError("administrator proposal could not be read back")
                connection.commit()
                return _administrator_proposal_from_row(row)
            except Exception:
                connection.rollback()
                raise

    def record_administrator_no_proposal(
        self, *, job: RPAdministratorJob, reason: str | None = None
    ) -> dict[str, Any]:
        result = {"kind": "administrator", "result": "no_proposal"}
        if reason is not None:
            result["reason"] = _required_text(reason, "reason")
        result_json = _canonical_json(result)
        with closing(self._connect()) as connection:
            changed = connection.execute(
                """
                UPDATE rp_administrator_jobs SET result_json = ?, updated_at = ?
                WHERE id = ? AND status = 'running' AND claim_token = ?
                """,
                (result_json, time.time_ns(), job.id, job.claim_token),
            ).rowcount
        if changed != 1:
            raise RPBackgroundJobConflict("administrator job is no longer owned")
        return json.loads(result_json)

    def list_administrator_proposals(
        self, *, owner_user_id: str, party_id: str
    ) -> tuple[RPAdministratorProposal, ...]:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            rows = connection.execute(
                """
                SELECT * FROM rp_administrator_proposals
                WHERE party_id = ? ORDER BY id
                """,
                (party_id,),
            ).fetchall()
        return tuple(_administrator_proposal_from_row(row) for row in rows)

    def decide_administrator_proposal(
        self,
        *,
        owner_user_id: str,
        party_id: str,
        proposal_id: int,
        decision: str,
    ) -> RPAdministratorProposal:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        if decision not in {"accept", "reject"}:
            raise ValueError("decision must be accept or reject")
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                party = _owned_party(connection, owner_user_id, party_id)
                if party is None:
                    raise RPPartyNotFound(party_id)
                row = connection.execute(
                    """
                    SELECT * FROM rp_administrator_proposals
                    WHERE id = ? AND party_id = ?
                    """,
                    (proposal_id, party_id),
                ).fetchone()
                if row is None:
                    raise LookupError(proposal_id)
                proposal = _administrator_proposal_from_row(row)
                expected_status = "accepted" if decision == "accept" else "rejected"
                if proposal.status == expected_status:
                    connection.commit()
                    return proposal
                if proposal.status != "pending":
                    raise RPAdministratorProposalConflict(
                        f"proposal is already {proposal.status}"
                    )
                if decision == "reject":
                    connection.execute(
                        """
                        UPDATE rp_administrator_proposals
                        SET status = 'rejected', decided_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (timestamp, proposal_id),
                    )
                else:
                    applied_version = proposal.base_party_version + 1
                    advanced = connection.execute(
                        """
                        UPDATE rp_parties
                        SET current_version = ?, updated_at = ?
                        WHERE id = ? AND owner_user_id = ? AND current_version = ?
                        """,
                        (
                            applied_version,
                            timestamp,
                            party_id,
                            owner_user_id,
                            proposal.base_party_version,
                        ),
                    ).rowcount
                    if advanced != 1:
                        connection.execute(
                            """
                            UPDATE rp_administrator_proposals
                            SET status = 'stale', decided_at = ?
                            WHERE id = ? AND status = 'pending'
                            """,
                            (timestamp, proposal_id),
                        )
                        connection.commit()
                        raise RPAdministratorProposalConflict(
                            "proposal base Party version is stale"
                        )
                    revision_row = connection.execute(
                        """
                        SELECT COALESCE(MAX(revision), 0) + 1 AS next_revision
                        FROM rp_administrator_guidance WHERE party_id = ?
                        """,
                        (party_id,),
                    ).fetchone()
                    revision = int(revision_row["next_revision"])
                    connection.execute(
                        """
                        INSERT INTO rp_administrator_guidance(
                            party_id, proposal_id, revision, party_version,
                            content, created_at
                        ) VALUES(?, ?, ?, ?, ?, ?)
                        """,
                        (
                            party_id,
                            proposal_id,
                            revision,
                            applied_version,
                            proposal.after_text,
                            timestamp,
                        ),
                    )
                    changed = connection.execute(
                        """
                        UPDATE rp_administrator_proposals
                        SET status = 'accepted', applied_party_version = ?, decided_at = ?
                        WHERE id = ? AND status = 'pending'
                        """,
                        (applied_version, timestamp, proposal_id),
                    ).rowcount
                    if changed != 1:
                        raise RPAdministratorProposalConflict(
                            "proposal changed during acceptance"
                        )
                saved = connection.execute(
                    "SELECT * FROM rp_administrator_proposals WHERE id = ?",
                    (proposal_id,),
                ).fetchone()
                if saved is None:
                    raise RuntimeError("administrator decision could not be read back")
                connection.commit()
                return _administrator_proposal_from_row(saved)
            except RPAdministratorProposalConflict:
                if connection.in_transaction:
                    connection.rollback()
                raise
            except Exception:
                connection.rollback()
                raise

    def derived_context(
        self, *, owner_user_id: str, party_id: str
    ) -> RPDerivedContext:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        with closing(self._connect()) as connection:
            if _owned_party(connection, owner_user_id, party_id) is None:
                raise RPPartyNotFound(party_id)
            cause_rows = connection.execute(
                """
                SELECT * FROM rp_relationship_causes
                WHERE party_id = ? ORDER BY source_version, id
                """,
                (party_id,),
            ).fetchall()
            lore_rows = connection.execute(
                """
                SELECT * FROM rp_runtime_lore_cards
                WHERE party_id = ? AND enabled = 1 ORDER BY source_version, id
                """,
                (party_id,),
            ).fetchall()
            guidance_row = connection.execute(
                """
                SELECT * FROM rp_administrator_guidance
                WHERE party_id = ? ORDER BY revision DESC LIMIT 1
                """,
                (party_id,),
            ).fetchone()
        return RPDerivedContext(
            relationship_causes=tuple(
                _relationship_cause_from_row(row) for row in cause_rows
            ),
            runtime_lore_cards=tuple(
                _runtime_lore_card_from_row(row) for row in lore_rows
            ),
            administrator_guidance=(
                _administrator_guidance_from_row(guidance_row)
                if guidance_row is not None
                else None
            ),
        )

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


def _enqueue_post_turn_work(
    connection: sqlite3.Connection,
    *,
    party_id: str,
    turn_id: int,
    committed_version: int,
    created_at: int,
) -> None:
    for job_type in ("story_memory", "relationships", "runtime_lore"):
        connection.execute(
            """
            INSERT INTO rp_service_jobs(
                party_id, job_type, source_turn_id, source_version,
                status, attempts, max_attempts, created_at, updated_at
            ) VALUES(?, ?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (
                party_id,
                job_type,
                turn_id,
                committed_version,
                RP_JOB_MAX_ATTEMPTS,
                created_at,
                created_at,
            ),
        )
    window_rows = connection.execute(
        """
        SELECT committed_version, turn_kind, player_text, narrator_text
        FROM rp_turns WHERE party_id = ?
        ORDER BY committed_version DESC LIMIT ?
        """,
        (party_id, RP_ADMINISTRATOR_WINDOW_TURNS),
    ).fetchall()
    window = [
        {
            "committed_version": int(row["committed_version"]),
            "turn_kind": str(row["turn_kind"]),
            "player_text": str(row["player_text"]),
            "narrator_text": str(row["narrator_text"]),
        }
        for row in reversed(window_rows)
    ]
    versions = [int(item["committed_version"]) for item in window]
    window_json = _canonical_json(window)
    connection.execute(
        """
        INSERT INTO rp_administrator_jobs(
            party_id, source_turn_id, source_version,
            window_start_version, window_end_version, window_hash,
            evidence_versions_json, status, attempts, max_attempts,
            created_at, updated_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)
        """,
        (
            party_id,
            turn_id,
            committed_version,
            versions[0],
            versions[-1],
            hashlib.sha256(window_json.encode("utf-8")).hexdigest(),
            _canonical_json(versions),
            RP_JOB_MAX_ATTEMPTS,
            created_at,
            created_at,
        ),
    )


def _narration_inputs(
    *,
    owner_user_id: str,
    party_id: str,
    turn_kind: str,
    request_id: str,
    idempotency_key: str,
    expected_version: int,
    player_text: str,
) -> tuple[str, str, str, str, str]:
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
    if turn_kind == "opening_scene":
        if expected_version != 0 or player_text != "":
            raise ValueError("opening_scene must target version 0 without player_text")
    else:
        player_text = _required_text(player_text, "player_text", preserve=True)
    return owner_user_id, party_id, request_id, idempotency_key, player_text


def _assert_same_narration_request(
    existing: RPNarrationRequest,
    *,
    turn_kind: str,
    request_id: str,
    idempotency_key: str,
    expected_version: int,
    player_text: str,
) -> None:
    if (
        existing.turn_kind != turn_kind
        or existing.request_id != request_id
        or existing.idempotency_key != idempotency_key
        or existing.expected_version != expected_version
        or existing.player_text != player_text
    ):
        raise RPIdempotencyConflict(
            "narration request identifier already owns different immutable input"
        )


def _owner_for_party(connection: sqlite3.Connection, party_id: str) -> str:
    row = connection.execute(
        "SELECT owner_user_id FROM rp_parties WHERE id = ?", (party_id,)
    ).fetchone()
    if row is None:
        raise RPSchemaError("background job Party is missing")
    return str(row["owner_user_id"])


def _owned_running_job(
    connection: sqlite3.Connection, table: str, job_id: int, claim_token: str | None
) -> sqlite3.Row:
    if table not in {"rp_service_jobs", "rp_administrator_jobs"}:
        raise ValueError("unknown RP job table")
    if claim_token is None:
        raise RPBackgroundJobConflict("job has no active claim")
    row = connection.execute(
        f"SELECT * FROM {table} WHERE id = ? AND status = 'running' AND claim_token = ?",
        (job_id, claim_token),
    ).fetchone()
    if row is None:
        raise RPBackgroundJobConflict("job is no longer owned")
    return row


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_object(value: str, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RPSchemaError(f"stored {field} is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise RPSchemaError(f"stored {field} must be an object")
    return parsed


def _json_int_tuple(value: str, field: str) -> tuple[int, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RPSchemaError(f"stored {field} is invalid JSON") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, int) or isinstance(item, bool) for item in parsed
    ):
        raise RPSchemaError(f"stored {field} must be an integer array")
    return tuple(parsed)


def _json_str_tuple(value: str, field: str) -> tuple[str, ...]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RPSchemaError(f"stored {field} is invalid JSON") from exc
    if not isinstance(parsed, list) or any(
        not isinstance(item, str) or not item.strip() for item in parsed
    ):
        raise RPSchemaError(f"stored {field} must be a non-empty string array")
    return tuple(parsed)


def _narration_request_from_row(row: sqlite3.Row) -> RPNarrationRequest:
    return RPNarrationRequest(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        turn_kind=str(row["turn_kind"]),
        request_id=str(row["request_id"]),
        idempotency_key=str(row["idempotency_key"]),
        expected_version=int(row["expected_version"]),
        player_text=str(row["player_text"]),
        status=str(row["status"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] is not None else None,
        turn_id=int(row["turn_id"]) if row["turn_id"] is not None else None,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _service_job_from_row(row: sqlite3.Row, owner_user_id: str) -> RPServiceJob:
    result = (
        _json_object(str(row["result_json"]), "service job result")
        if row["result_json"] is not None
        else None
    )
    return RPServiceJob(
        id=int(row["id"]),
        owner_user_id=owner_user_id,
        party_id=str(row["party_id"]),
        job_type=str(row["job_type"]),
        source_turn_id=int(row["source_turn_id"]),
        source_version=int(row["source_version"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] is not None else None,
        result=result,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _administrator_job_from_row(
    row: sqlite3.Row, owner_user_id: str
) -> RPAdministratorJob:
    result = (
        _json_object(str(row["result_json"]), "administrator job result")
        if row["result_json"] is not None
        else None
    )
    return RPAdministratorJob(
        id=int(row["id"]),
        owner_user_id=owner_user_id,
        party_id=str(row["party_id"]),
        source_turn_id=int(row["source_turn_id"]),
        source_version=int(row["source_version"]),
        window_start_version=int(row["window_start_version"]),
        window_end_version=int(row["window_end_version"]),
        window_hash=str(row["window_hash"]),
        evidence_versions=_json_int_tuple(
            str(row["evidence_versions_json"]), "administrator evidence versions"
        ),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        claim_token=str(row["claim_token"]) if row["claim_token"] is not None else None,
        result=result,
        last_error=str(row["last_error"]) if row["last_error"] is not None else None,
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _relationship_cause_from_row(row: sqlite3.Row) -> RPRelationshipCause:
    return RPRelationshipCause(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        service_job_id=int(row["service_job_id"]),
        source_turn_id=int(row["source_turn_id"]),
        source_version=int(row["source_version"]),
        candidate_key=str(row["candidate_key"]),
        character_id=str(row["character_id"]),
        direction=str(row["direction"]),
        event_id=str(row["event_id"]),
        axis=str(row["axis"]),
        delta=int(row["delta"]),
        evidence_span_ids=_json_int_tuple(
            str(row["evidence_span_ids_json"]), "relationship evidence spans"
        ),
        created_at=int(row["created_at"]),
    )


def _runtime_lore_card_from_row(row: sqlite3.Row) -> RPRuntimeLoreCard:
    return RPRuntimeLoreCard(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        service_job_id=int(row["service_job_id"]),
        source_turn_id=int(row["source_turn_id"]),
        source_version=int(row["source_version"]),
        card_key=str(row["card_key"]),
        kind=str(row["kind"]),
        origin=str(row["origin"]),
        title=str(row["title"]),
        content=str(row["content"]),
        keywords=_json_str_tuple(str(row["keywords_json"]), "runtime Lore keywords"),
        evidence_span_ids=_json_int_tuple(
            str(row["evidence_span_ids_json"]), "runtime Lore evidence spans"
        ),
        enabled=bool(row["enabled"]),
        created_at=int(row["created_at"]),
    )


def _administrator_proposal_from_row(row: sqlite3.Row) -> RPAdministratorProposal:
    return RPAdministratorProposal(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        administrator_job_id=int(row["administrator_job_id"]),
        kind=str(row["kind"]),
        target_slot=str(row["target_slot"]),
        before_text=str(row["before_text"]),
        after_text=str(row["after_text"]),
        base_party_version=int(row["base_party_version"]),
        evidence_versions=_json_int_tuple(
            str(row["evidence_versions_json"]), "administrator proposal evidence"
        ),
        window_hash=str(row["window_hash"]),
        status=str(row["status"]),
        applied_party_version=(
            int(row["applied_party_version"])
            if row["applied_party_version"] is not None
            else None
        ),
        created_at=int(row["created_at"]),
        decided_at=int(row["decided_at"]) if row["decided_at"] is not None else None,
    )


def _administrator_guidance_from_row(row: sqlite3.Row) -> RPAdministratorGuidance:
    return RPAdministratorGuidance(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        proposal_id=int(row["proposal_id"]),
        revision=int(row["revision"]),
        party_version=int(row["party_version"]),
        content=str(row["content"]),
        created_at=int(row["created_at"]),
    )
