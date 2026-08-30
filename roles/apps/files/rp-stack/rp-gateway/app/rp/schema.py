"""Clean SQLite schema owned by the rebuilt RP turn engine."""

from __future__ import annotations

import sqlite3


RP_DATABASE_APPLICATION_ID = 0x5250454E  # "RPEN"
RP_SCHEMA_VERSION = 4

_EXPECTED_TABLES = frozenset(
    {
        "rp_administrator_guidance",
        "rp_administrator_jobs",
        "rp_administrator_proposals",
        "rp_narration_requests",
        "rp_parties",
        "rp_relationship_causes",
        "rp_runtime_lore_cards",
        "rp_service_jobs",
        "rp_story_memory_snapshots",
        "rp_turns",
    }
)
_EXPECTED_TRIGGERS = frozenset(
    {
        "rp_administrator_guidance_immutable",
        "rp_administrator_guidance_no_delete",
        "rp_parties_snapshots_immutable",
        "rp_relationship_causes_immutable",
        "rp_relationship_causes_no_delete",
        "rp_runtime_lore_cards_immutable",
        "rp_runtime_lore_cards_no_delete",
        "rp_turns_immutable",
        "rp_turns_no_delete",
        "rp_story_memory_snapshots_immutable",
        "rp_story_memory_snapshots_no_delete",
    }
)


class RPSchemaError(RuntimeError):
    """The selected database is not the clean schema owned by this engine."""


def initialize_schema(connection: sqlite3.Connection) -> None:
    """Create a fresh RP database or validate an existing one without migrating it."""
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("BEGIN IMMEDIATE")
    try:
        tables = _object_names(connection, "table")
        application_id = int(connection.execute("PRAGMA application_id").fetchone()[0])
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if application_id == 0 and schema_version == 0 and not tables:
            _create_schema(connection)
        else:
            _validate_schema(connection, tables, application_id, schema_version)
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE rp_parties (
            id TEXT PRIMARY KEY CHECK(length(trim(id)) > 0),
            owner_user_id TEXT NOT NULL CHECK(length(trim(owner_user_id)) > 0),
            world_snapshot_json TEXT NOT NULL CHECK(length(trim(world_snapshot_json)) > 0),
            world_hash TEXT NOT NULL CHECK(length(world_hash) = 64),
            scenario_snapshot_json TEXT NOT NULL CHECK(length(trim(scenario_snapshot_json)) > 0),
            scenario_hash TEXT NOT NULL CHECK(length(scenario_hash) = 64),
            current_version INTEGER NOT NULL DEFAULT 0 CHECK(current_version >= 0),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        ) STRICT
        """,
        """
        CREATE TABLE rp_turns (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            turn_kind TEXT NOT NULL CHECK(turn_kind IN ('opening_scene', 'narrative')),
            request_id TEXT NOT NULL CHECK(length(trim(request_id)) > 0),
            idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) > 0),
            expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
            committed_version INTEGER NOT NULL CHECK(committed_version > 0),
            player_text TEXT NOT NULL,
            narrator_text TEXT NOT NULL CHECK(length(trim(narrator_text)) > 0),
            created_at INTEGER NOT NULL,
            CHECK(committed_version = expected_version + 1),
            CHECK(
                (turn_kind = 'opening_scene' AND player_text = '') OR
                (turn_kind = 'narrative' AND length(trim(player_text)) > 0)
            ),
            UNIQUE(party_id, committed_version),
            UNIQUE(party_id, request_id),
            UNIQUE(party_id, idempotency_key)
        ) STRICT
        """,
        """
        CREATE TABLE rp_story_memory_snapshots (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            base_snapshot_id INTEGER,
            update_id TEXT NOT NULL CHECK(length(trim(update_id)) > 0),
            snapshot_json TEXT NOT NULL CHECK(length(trim(snapshot_json)) > 0),
            observed_through_version INTEGER NOT NULL CHECK(observed_through_version >= 0),
            situation_coverage INTEGER NOT NULL CHECK(situation_coverage >= 0),
            threads_coverage INTEGER NOT NULL CHECK(threads_coverage >= 0),
            characters_coverage INTEGER NOT NULL CHECK(characters_coverage >= 0),
            assets_and_rules_coverage INTEGER NOT NULL CHECK(assets_and_rules_coverage >= 0),
            chronology_and_hooks_coverage INTEGER NOT NULL CHECK(chronology_and_hooks_coverage >= 0),
            created_at INTEGER NOT NULL,
            CHECK(situation_coverage <= observed_through_version),
            CHECK(threads_coverage <= observed_through_version),
            CHECK(characters_coverage <= observed_through_version),
            CHECK(assets_and_rules_coverage <= observed_through_version),
            CHECK(chronology_and_hooks_coverage <= observed_through_version),
            CHECK(
                (revision = 1 AND base_snapshot_id IS NULL) OR
                (revision > 1 AND base_snapshot_id IS NOT NULL)
            ),
            UNIQUE(party_id, id),
            UNIQUE(party_id, revision),
            UNIQUE(party_id, update_id),
            FOREIGN KEY(party_id, base_snapshot_id)
                REFERENCES rp_story_memory_snapshots(party_id, id) ON DELETE RESTRICT
        ) STRICT
        """,
        """
        CREATE TABLE rp_narration_requests (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            turn_kind TEXT NOT NULL CHECK(turn_kind IN ('opening_scene', 'narrative')),
            request_id TEXT NOT NULL CHECK(length(trim(request_id)) > 0),
            idempotency_key TEXT NOT NULL CHECK(length(trim(idempotency_key)) > 0),
            expected_version INTEGER NOT NULL CHECK(expected_version >= 0),
            player_text TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('running', 'succeeded', 'failed')),
            claim_token TEXT CHECK(claim_token IS NULL OR length(trim(claim_token)) > 0),
            turn_id INTEGER REFERENCES rp_turns(id) ON DELETE RESTRICT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            CHECK(
                (turn_kind = 'opening_scene' AND player_text = '' AND expected_version = 0) OR
                (turn_kind = 'narrative' AND length(trim(player_text)) > 0)
            ),
            CHECK(
                (status = 'running' AND claim_token IS NOT NULL AND turn_id IS NULL) OR
                (status = 'succeeded' AND claim_token IS NULL AND turn_id IS NOT NULL) OR
                (status = 'failed' AND claim_token IS NULL AND turn_id IS NULL)
            ),
            UNIQUE(party_id, request_id),
            UNIQUE(party_id, idempotency_key)
        ) STRICT
        """,
        """
        CREATE TABLE rp_service_jobs (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            job_type TEXT NOT NULL CHECK(
                job_type IN ('story_memory', 'relationships', 'runtime_lore')
            ),
            source_turn_id INTEGER NOT NULL REFERENCES rp_turns(id) ON DELETE RESTRICT,
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
            claim_token TEXT CHECK(claim_token IS NULL OR length(trim(claim_token)) > 0),
            result_json TEXT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            CHECK(attempts <= max_attempts),
            CHECK(
                (status = 'running' AND claim_token IS NOT NULL) OR
                (status != 'running' AND claim_token IS NULL)
            ),
            UNIQUE(party_id, job_type, source_turn_id)
        ) STRICT
        """,
        """
        CREATE TABLE rp_administrator_jobs (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            source_turn_id INTEGER NOT NULL REFERENCES rp_turns(id) ON DELETE RESTRICT,
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            window_start_version INTEGER NOT NULL CHECK(window_start_version > 0),
            window_end_version INTEGER NOT NULL CHECK(window_end_version >= window_start_version),
            window_hash TEXT NOT NULL CHECK(length(window_hash) = 64),
            evidence_versions_json TEXT NOT NULL CHECK(length(trim(evidence_versions_json)) > 0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'running', 'succeeded', 'failed')),
            attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
            max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
            claim_token TEXT CHECK(claim_token IS NULL OR length(trim(claim_token)) > 0),
            result_json TEXT,
            last_error TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            CHECK(attempts <= max_attempts),
            CHECK(
                (status = 'running' AND claim_token IS NOT NULL) OR
                (status != 'running' AND claim_token IS NULL)
            ),
            UNIQUE(party_id, source_turn_id)
        ) STRICT
        """,
        """
        CREATE TABLE rp_relationship_causes (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            service_job_id INTEGER NOT NULL REFERENCES rp_service_jobs(id) ON DELETE RESTRICT,
            source_turn_id INTEGER NOT NULL REFERENCES rp_turns(id) ON DELETE RESTRICT,
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            candidate_key TEXT NOT NULL CHECK(length(candidate_key) = 64),
            character_id TEXT NOT NULL CHECK(length(trim(character_id)) > 0),
            direction TEXT NOT NULL CHECK(direction = 'character_to_player'),
            event_id TEXT NOT NULL CHECK(length(trim(event_id)) > 0),
            axis TEXT NOT NULL CHECK(length(trim(axis)) > 0),
            delta INTEGER NOT NULL,
            evidence_span_ids_json TEXT NOT NULL CHECK(length(trim(evidence_span_ids_json)) > 0),
            created_at INTEGER NOT NULL,
            UNIQUE(party_id, candidate_key)
        ) STRICT
        """,
        """
        CREATE TABLE rp_runtime_lore_cards (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            service_job_id INTEGER NOT NULL REFERENCES rp_service_jobs(id) ON DELETE RESTRICT,
            source_turn_id INTEGER NOT NULL REFERENCES rp_turns(id) ON DELETE RESTRICT,
            source_version INTEGER NOT NULL CHECK(source_version > 0),
            card_key TEXT NOT NULL CHECK(length(card_key) = 64),
            kind TEXT NOT NULL CHECK(kind IN ('character', 'event', 'location')),
            origin TEXT NOT NULL CHECK(origin = 'runtime'),
            title TEXT NOT NULL CHECK(length(trim(title)) > 0),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            keywords_json TEXT NOT NULL CHECK(length(trim(keywords_json)) > 0),
            evidence_span_ids_json TEXT NOT NULL CHECK(length(trim(evidence_span_ids_json)) > 0),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            created_at INTEGER NOT NULL,
            UNIQUE(party_id, card_key)
        ) STRICT
        """,
        """
        CREATE TABLE rp_administrator_proposals (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            administrator_job_id INTEGER NOT NULL
                REFERENCES rp_administrator_jobs(id) ON DELETE RESTRICT,
            kind TEXT NOT NULL CHECK(kind = 'narrator_guidance'),
            target_slot TEXT NOT NULL CHECK(target_slot = 'narrator_guidance'),
            before_text TEXT NOT NULL,
            after_text TEXT NOT NULL CHECK(length(trim(after_text)) > 0),
            base_party_version INTEGER NOT NULL CHECK(base_party_version >= 0),
            evidence_versions_json TEXT NOT NULL CHECK(length(trim(evidence_versions_json)) > 0),
            window_hash TEXT NOT NULL CHECK(length(window_hash) = 64),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending', 'accepted', 'rejected', 'stale')),
            applied_party_version INTEGER CHECK(applied_party_version > base_party_version),
            created_at INTEGER NOT NULL,
            decided_at INTEGER,
            CHECK(
                (status = 'accepted' AND applied_party_version IS NOT NULL AND decided_at IS NOT NULL) OR
                (status IN ('rejected', 'stale') AND applied_party_version IS NULL AND decided_at IS NOT NULL) OR
                (status = 'pending' AND applied_party_version IS NULL AND decided_at IS NULL)
            ),
            UNIQUE(party_id, administrator_job_id)
        ) STRICT
        """,
        """
        CREATE TABLE rp_administrator_guidance (
            id INTEGER PRIMARY KEY,
            party_id TEXT NOT NULL REFERENCES rp_parties(id) ON DELETE RESTRICT,
            proposal_id INTEGER NOT NULL UNIQUE
                REFERENCES rp_administrator_proposals(id) ON DELETE RESTRICT,
            revision INTEGER NOT NULL CHECK(revision > 0),
            party_version INTEGER NOT NULL CHECK(party_version > 0),
            content TEXT NOT NULL CHECK(length(trim(content)) > 0),
            created_at INTEGER NOT NULL,
            UNIQUE(party_id, revision),
            UNIQUE(party_id, party_version)
        ) STRICT
        """,
        """
        CREATE TRIGGER rp_turns_immutable
        BEFORE UPDATE ON rp_turns
        BEGIN
            SELECT RAISE(ABORT, 'committed RP turns are immutable');
        END
        """,
        """
        CREATE TRIGGER rp_parties_snapshots_immutable
        BEFORE UPDATE OF
            world_snapshot_json, world_hash, scenario_snapshot_json, scenario_hash
        ON rp_parties
        BEGIN
            SELECT RAISE(ABORT, 'RP source snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER rp_turns_no_delete
        BEFORE DELETE ON rp_turns
        BEGIN
            SELECT RAISE(ABORT, 'committed RP turns cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER rp_story_memory_snapshots_immutable
        BEFORE UPDATE ON rp_story_memory_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'RP story-memory snapshots are immutable');
        END
        """,
        """
        CREATE TRIGGER rp_story_memory_snapshots_no_delete
        BEFORE DELETE ON rp_story_memory_snapshots
        BEGIN
            SELECT RAISE(ABORT, 'RP story-memory snapshots cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER rp_relationship_causes_immutable
        BEFORE UPDATE ON rp_relationship_causes
        BEGIN
            SELECT RAISE(ABORT, 'RP relationship causes are immutable');
        END
        """,
        """
        CREATE TRIGGER rp_relationship_causes_no_delete
        BEFORE DELETE ON rp_relationship_causes
        BEGIN
            SELECT RAISE(ABORT, 'RP relationship causes cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER rp_runtime_lore_cards_immutable
        BEFORE UPDATE ON rp_runtime_lore_cards
        BEGIN
            SELECT RAISE(ABORT, 'RP runtime Lore cards are immutable');
        END
        """,
        """
        CREATE TRIGGER rp_runtime_lore_cards_no_delete
        BEFORE DELETE ON rp_runtime_lore_cards
        BEGIN
            SELECT RAISE(ABORT, 'RP runtime Lore cards cannot be deleted');
        END
        """,
        """
        CREATE TRIGGER rp_administrator_guidance_immutable
        BEFORE UPDATE ON rp_administrator_guidance
        BEGIN
            SELECT RAISE(ABORT, 'accepted RP administrator guidance is immutable');
        END
        """,
        """
        CREATE TRIGGER rp_administrator_guidance_no_delete
        BEFORE DELETE ON rp_administrator_guidance
        BEGIN
            SELECT RAISE(ABORT, 'accepted RP administrator guidance cannot be deleted');
        END
        """,
    )
    for statement in statements:
        connection.execute(statement)
    connection.execute(f"PRAGMA application_id = {RP_DATABASE_APPLICATION_ID}")
    connection.execute(f"PRAGMA user_version = {RP_SCHEMA_VERSION}")


def _validate_schema(
    connection: sqlite3.Connection,
    tables: frozenset[str],
    application_id: int,
    schema_version: int,
) -> None:
    if application_id != RP_DATABASE_APPLICATION_ID:
        raise RPSchemaError(
            "database is not an isolated RP engine database: "
            f"unexpected application id {application_id}"
        )
    if schema_version != RP_SCHEMA_VERSION:
        raise RPSchemaError(
            f"unsupported RP schema version {schema_version}; expected {RP_SCHEMA_VERSION}"
        )
    if tables != _EXPECTED_TABLES:
        raise RPSchemaError(
            "database is not an isolated RP engine database: "
            f"expected tables {sorted(_EXPECTED_TABLES)}, found {sorted(tables)}"
        )
    triggers = _object_names(connection, "trigger")
    if triggers != _EXPECTED_TRIGGERS:
        raise RPSchemaError(
            "RP schema trigger set does not match version "
            f"{RP_SCHEMA_VERSION}: found {sorted(triggers)}"
        )


def _object_names(connection: sqlite3.Connection, object_type: str) -> frozenset[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_schema WHERE type = ? AND name NOT LIKE 'sqlite_%'",
        (object_type,),
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)
