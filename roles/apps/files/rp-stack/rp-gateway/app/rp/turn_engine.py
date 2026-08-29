"""Offline turn boundary for the rebuilt RP stack."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from app.rp.schema import initialize_schema


class RPPartyNotFound(LookupError):
    """The requested party does not exist in the clean RP database."""


class RPIdempotencyConflict(RuntimeError):
    """A request identifier was reused for different immutable input."""


class RPPartyVersionConflict(RuntimeError):
    """The turn was based on an older or future party version."""


@dataclass(frozen=True, slots=True)
class RPParty:
    id: str
    owner_user_id: str
    current_version: int
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class RPTurn:
    id: int
    party_id: str
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

    def create_party(self, *, owner_user_id: str, party_id: str) -> RPParty:
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        timestamp = time.time_ns()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO rp_parties(
                        id, owner_user_id, current_version, created_at, updated_at
                    ) VALUES(?, ?, 0, ?, ?)
                    ON CONFLICT(id) DO NOTHING
                    """,
                    (party_id, owner_user_id, timestamp, timestamp),
                )
                party = _owned_party(connection, owner_user_id, party_id)
                if party is None:
                    raise RPPartyNotFound(party_id)
                connection.commit()
                return _party_from_row(party)
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
        owner_user_id = _required_text(owner_user_id, "owner_user_id")
        party_id = _required_text(party_id, "party_id")
        request_id = _required_text(request_id, "request_id")
        idempotency_key = _required_text(idempotency_key, "idempotency_key")
        if not isinstance(expected_version, int) or isinstance(expected_version, bool):
            raise ValueError("expected_version must be a non-negative integer")
        if expected_version < 0:
            raise ValueError("expected_version must be a non-negative integer")
        player_text = _required_text(player_text, "player_text", preserve=True)
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
                        turn.request_id != request_id
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
                        party_id, request_id, idempotency_key,
                        expected_version, committed_version,
                        player_text, narrator_text, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        party_id,
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
    return RPParty(
        id=str(row["id"]),
        owner_user_id=str(row["owner_user_id"]),
        current_version=int(row["current_version"]),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def _turn_from_row(row: sqlite3.Row) -> RPTurn:
    return RPTurn(
        id=int(row["id"]),
        party_id=str(row["party_id"]),
        request_id=str(row["request_id"]),
        idempotency_key=str(row["idempotency_key"]),
        expected_version=int(row["expected_version"]),
        committed_version=int(row["committed_version"]),
        player_text=str(row["player_text"]),
        narrator_text=str(row["narrator_text"]),
        created_at=int(row["created_at"]),
    )
