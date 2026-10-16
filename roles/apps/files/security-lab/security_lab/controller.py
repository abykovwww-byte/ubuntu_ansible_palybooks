"""Single-writer cases, immutable plans and delegated operator task activation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
from typing import Iterator

from .contracts import ActionPlan, Denied, Scope, canonical, digest, hostname
from .discovery_contracts import SourceResult
from .client_identity import delegate_client


class Controller:
    def __init__(self, directory: Path, principal: str, clock=time.time):
        if not principal or len(principal) > 200:
            raise ValueError("Configured operator identity required")
        self.root = directory.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.principal, self.clock = principal, clock
        self.lock = threading.RLock()
        # Do not allow independent MCP instances to become competing writers.
        self.owner_file = (self.root / "controller.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                # Do not read another owner's locked byte during discovery.
                self.owner_file.seek(0, os.SEEK_END)
                if self.owner_file.tell() == 0:
                    self.owner_file.write(b"0")
                    self.owner_file.flush()
                self.owner_file.seek(0)
                msvcrt.locking(self.owner_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            self.owner_file.close()
            raise
        self.db = sqlite3.connect(self.root / "control.db", timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1, 2}:
            self.close()
            raise Denied("Unsupported database schema; explicit migration required")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS cases (
          id TEXT PRIMARY KEY, owner TEXT NOT NULL, seed TEXT NOT NULL,
          state TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
          created REAL NOT NULL, closed REAL, spent REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS manifests (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          kind TEXT NOT NULL, version INTEGER NOT NULL, body TEXT NOT NULL,
          hash TEXT NOT NULL, status TEXT NOT NULL, approved_by TEXT, approved_at REAL,
          UNIQUE(case_id, kind, version)
        );
        CREATE TABLE IF NOT EXISTS audit (
          sequence INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          at REAL NOT NULL, event TEXT NOT NULL, actor TEXT NOT NULL, details TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS evidence (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id), source TEXT NOT NULL,
          at REAL NOT NULL, status TEXT NOT NULL, body TEXT NOT NULL, hash TEXT NOT NULL,
          synthetic INTEGER NOT NULL, purged INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS surface (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          kind TEXT NOT NULL, value TEXT NOT NULL, attribution TEXT NOT NULL,
          evidence_ids TEXT NOT NULL, UNIQUE(case_id, kind, value)
        );
        CREATE TABLE IF NOT EXISTS jobs (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          generation INTEGER NOT NULL, scope_id TEXT NOT NULL REFERENCES manifests(id),
          profile TEXT NOT NULL, worker TEXT NOT NULL, status TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE, lease_until REAL NOT NULL,
          deadline REAL NOT NULL, duration INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS outbox (
          id INTEGER PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          generation INTEGER NOT NULL, event TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS discovery_runs (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          status TEXT NOT NULL, started REAL NOT NULL, finished REAL,
          reason TEXT NOT NULL DEFAULT '', schema_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS source_receipts (
          id TEXT PRIMARY KEY REFERENCES evidence(id), case_id TEXT NOT NULL REFERENCES cases(id),
          run_id TEXT NOT NULL REFERENCES discovery_runs(id), source TEXT NOT NULL,
          category TEXT NOT NULL, request_url TEXT NOT NULL, subject TEXT NOT NULL,
          parser TEXT NOT NULL, parser_version TEXT NOT NULL, collected_at REAL NOT NULL,
          status TEXT NOT NULL, http_status INTEGER, body TEXT NOT NULL, body_hash TEXT NOT NULL,
          reason TEXT NOT NULL, truncated INTEGER NOT NULL, synthetic INTEGER NOT NULL,
          schema_version INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS observation_history (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          run_id TEXT NOT NULL REFERENCES discovery_runs(id), node_id TEXT NOT NULL REFERENCES surface(id),
          receipt_id TEXT NOT NULL REFERENCES source_receipts(id), attribution TEXT NOT NULL,
          freshness TEXT NOT NULL, pointer TEXT NOT NULL, collected_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS surface_links (
          id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          source_id TEXT NOT NULL REFERENCES surface(id), target_id TEXT NOT NULL REFERENCES surface(id),
          relation TEXT NOT NULL, evidence_ids TEXT NOT NULL,
          UNIQUE(case_id,source_id,target_id,relation)
        );
        CREATE TABLE IF NOT EXISTS link_observations (
          id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(id),
          link_id TEXT NOT NULL REFERENCES surface_links(id), receipt_id TEXT NOT NULL REFERENCES source_receipts(id),
          basis TEXT NOT NULL, freshness TEXT NOT NULL, pointer TEXT NOT NULL, collected_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS discovery_entities (
          run_id TEXT NOT NULL REFERENCES discovery_runs(id), node_id TEXT NOT NULL REFERENCES surface(id),
          PRIMARY KEY(run_id,node_id)
        );
        CREATE TABLE IF NOT EXISTS job_runtime (
          job_id TEXT PRIMARY KEY REFERENCES jobs(id), policy_hash TEXT,
          lease_sequence INTEGER NOT NULL DEFAULT 1, acknowledged_sequence INTEGER NOT NULL DEFAULT 0,
          plan TEXT NOT NULL DEFAULT '{}', started REAL, finished REAL,
          outcome TEXT, evidence_id TEXT REFERENCES evidence(id), charged INTEGER NOT NULL DEFAULT 1
        );
        """)
        # Additive schema v1 preserves existing immutable manifests and audit.
        # A process restart never silently replays unfinished Internet requests.
        with self.transaction() as db:
            # Existing v1 jobs had no accounting. Charge their entire reserved
            # window once, including uncertain outcomes; never refund on restart.
            for job in db.execute("SELECT j.* FROM jobs j LEFT JOIN job_runtime r ON r.job_id=j.id WHERE r.job_id IS NULL").fetchall():
                db.execute("INSERT INTO job_runtime(job_id) VALUES(?)", (job["id"],))
                db.execute("UPDATE cases SET spent=spent+? WHERE id=?", (job["duration"], job["case_id"]))
            db.execute("PRAGMA user_version=2")
            active_cases = db.execute("SELECT DISTINCT case_id FROM jobs WHERE status IN ('prepared','running')").fetchall()
            for active in active_cases:
                self.revoke_in(db, active["case_id"], "controller_restarted")
                db.execute("UPDATE job_runtime SET finished=?,outcome='interrupted' WHERE job_id IN "
                           "(SELECT id FROM jobs WHERE case_id=? AND status='revoked') AND finished IS NULL",
                           (self.clock(), active["case_id"]))
                db.execute("UPDATE cases SET state='paused' WHERE id=?", (active["case_id"],))
            interrupted = db.execute("SELECT id,case_id FROM discovery_runs WHERE status IN ('pending','running')").fetchall()
            for run in interrupted:
                db.execute("UPDATE discovery_runs SET status='interrupted',finished=?,reason=? WHERE id=?",
                           (self.clock(), "Controller restarted; partial receipts preserved", run["id"]))
                db.execute("UPDATE cases SET state='discovery_review' WHERE id=? AND state='discovering'", (run["case_id"],))
                self.audit(db, run["case_id"], "discovery_interrupted", {"run_id": run["id"]})

    def close(self) -> None:
        if hasattr(self, "db"):
            self.db.close()
        self.owner_file.close()

    @contextlib.contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def audit(self, db: sqlite3.Connection, case_id: str, event: str, details: object) -> None:
        # Keep the historical actor/owner contract; add authenticated client
        # provenance without rewriting old audit or immutable manifests.
        if isinstance(details, dict):
            details = {**details, "client": delegate_client.get()}
        db.execute("INSERT INTO audit(case_id,at,event,actor,details) VALUES(?,?,?,?,?)",
                   (case_id, self.clock(), event, self.principal, canonical(details)))

    def case(self, db: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM cases WHERE id=? AND owner=?", (case_id, self.principal)).fetchone()
        if row is None:
            raise Denied("Case not found or not owned by this principal")
        return row

    def list_cases(self, *, offset: int = 0, limit: int = 50) -> dict:
        if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 100:
            raise Denied("Case pagination requires offset >= 0 and limit between 1 and 100")
        with self.lock:
            total = self.db.execute("SELECT COUNT(*) FROM cases WHERE owner=?", (self.principal,)).fetchone()[0]
            rows = self.db.execute(
                "SELECT id,seed,state,generation,created,spent FROM cases WHERE owner=? "
                "ORDER BY created DESC,id LIMIT ? OFFSET ?", (self.principal, limit, offset)).fetchall()
            return {"cases": [dict(row) for row in rows], "total": total,
                    "next_offset": offset + len(rows) if offset + len(rows) < total else None}

    def check_revision(self, case_id: str, expected_revision: int | None) -> None:
        """Compare the audit cursor under the caller's transaction/lock."""
        with self.lock:
            self.case(self.db, case_id)
            actual = self.db.execute("SELECT COALESCE(MAX(sequence),0) FROM audit WHERE case_id=?", (case_id,)).fetchone()[0]
            if type(expected_revision) is not int or expected_revision != actual:
                raise Denied("Case revision conflict; read get_case again before issuing a new command")

    def record_checkpoint(self, case_id: str, checkpoint: dict, *, expected_revision: int) -> dict:
        from .case_history import CaseCheckpoint
        value = CaseCheckpoint.model_validate(checkpoint).model_dump(mode='json')
        if len(canonical(value).encode('utf-8')) > 48000:
            raise Denied('Case checkpoint exceeds 48000 bytes')
        if len({item['id'] for item in value['items']}) != len(value['items']):
            raise Denied('Checkpoint item IDs must be distinct')
        with self.transaction() as db:
            self.check_revision(case_id, expected_revision)
            for item in value['items']:
                if any(len(ref) > 1000 for ref in item['artifact_refs']):
                    raise Denied('Artifact reference too long')
                for ref in item['evidence_ids']:
                    if not db.execute('SELECT 1 FROM evidence WHERE id=? AND case_id=?', (ref, case_id)).fetchone():
                        raise Denied('Checkpoint evidence not found in this case')
            # Text can include tool output: sanitize before durable storage.
            from .dispatcher import redact_worker_output
            def sanitize(item):
                if isinstance(item, str):
                    return redact_worker_output(item)
                if isinstance(item, list):
                    return [sanitize(v) for v in item]
                if isinstance(item, dict):
                    return {key: sanitize(v) for key, v in item.items()}
                return item
            value = sanitize(value)
            self.audit(db, case_id, 'case_checkpoint', {'checkpoint': value})
            return self.get_case_history(case_id, limit=1, after_sequence=self.get_case(case_id)['revision'] - 1)

    def get_case_history(self, case_id: str, *, after_sequence: int = 0, limit: int = 50) -> dict:
        from .case_history import continuation
        if type(after_sequence) is not int or after_sequence < 0 or type(limit) is not int or not 1 <= limit <= 100:
            raise Denied('History requires a nonnegative cursor and limit between 1 and 100')
        with self.lock:
            self.case(self.db, case_id)
            revision = self.db.execute('SELECT COALESCE(MAX(sequence),0) FROM audit WHERE case_id=?', (case_id,)).fetchone()[0]
            candidates = self.db.execute(
                'SELECT sequence,at,event,actor,details FROM audit WHERE case_id=? AND sequence>? '
                'ORDER BY sequence LIMIT ?', (case_id, after_sequence, limit + 1))
            rows, size, more = [], 0, False
            for row in candidates:
                item = dict(row)
                length = len(canonical(item).encode('utf-8'))
                if rows and (len(rows) == limit or size + length > 64000):
                    more = True
                    break
                rows.append(item)
                size += length
            checkpoint = self.db.execute("SELECT sequence,at,event,details FROM audit WHERE case_id=? "
                "AND event='case_checkpoint' ORDER BY sequence DESC LIMIT 1", (case_id,)).fetchone()
            return {'case_id': case_id, 'revision': revision, 'events': rows,
                    'next_after_sequence': rows[-1]['sequence'] if more else None,
                    'continuation': continuation({'audit': [dict(checkpoint)] if checkpoint else []})}

    def get_manifest(self, case_id: str, manifest_id: str) -> dict:
        with self.lock:
            self.case(self.db, case_id)
            row = self.db.execute('SELECT * FROM manifests WHERE case_id=? AND id=?', (case_id, manifest_id)).fetchone()
            if row is None:
                raise Denied('Manifest not found in this case')
            result = dict(row)
            result['body'] = json.loads(result['body'])
            return result

    def create_case(self, seed: str) -> dict:
        seed, case_id = hostname(seed), uuid.uuid4().hex
        with self.transaction() as db:
            db.execute("INSERT INTO cases(id,owner,seed,state,created) VALUES(?,?,?,?,?)",
                       (case_id, self.principal, seed, "created", self.clock()))
            self.audit(db, case_id, "case_created", {"seed": seed})
        return self.get_case(case_id)

    def get_case(self, case_id: str) -> dict:
        with self.lock:
            row = dict(self.case(self.db, case_id))
            row["revision"] = self.db.execute("SELECT COALESCE(MAX(sequence),0) FROM audit WHERE case_id=?", (case_id,)).fetchone()[0]
            row["surface"] = [dict(v) for v in self.db.execute("SELECT * FROM surface WHERE case_id=?", (case_id,))]
            row["manifests"] = [dict(v) for v in self.db.execute(
                "SELECT id,kind,version,hash,status,approved_by,approved_at FROM manifests WHERE case_id=?", (case_id,))]
            row["jobs"] = [dict(v) for v in self.db.execute(
                "SELECT j.id,j.profile,j.status,j.generation,j.lease_until,j.deadline,r.policy_hash,"
                "r.lease_sequence,r.acknowledged_sequence,r.started,r.finished,r.outcome,r.evidence_id "
                "FROM jobs j LEFT JOIN job_runtime r ON r.job_id=j.id WHERE case_id=?", (case_id,))]
            row["discovery_runs"] = [dict(v) for v in self.db.execute(
                "SELECT * FROM discovery_runs WHERE case_id=? ORDER BY started,id", (case_id,))]
            row["source_receipts"] = [dict(v) for v in self.db.execute(
                "SELECT id,run_id,source,category,request_url,subject,parser,parser_version,collected_at,status,http_status,"
                "body_hash,reason,truncated,synthetic,schema_version FROM source_receipts WHERE case_id=? ORDER BY rowid", (case_id,))]
            row["coverage"] = [dict(v) for v in self.db.execute(
                "SELECT source,category,status,COUNT(*) AS receipts,SUM(truncated) AS truncated,"
                "SUM(synthetic) AS synthetic FROM source_receipts WHERE case_id=? GROUP BY source,category,status", (case_id,))]
            row["observations"] = [dict(v) for v in self.db.execute(
                "SELECT * FROM observation_history WHERE case_id=? ORDER BY rowid", (case_id,))]
            for node in row["surface"]:
                history = [o for o in row["observations"] if o["node_id"] == node["id"]]
                node["attribution_history"] = sorted({o["attribution"] for o in history})
                node["freshness_history"] = sorted({o["freshness"] for o in history})
                # The legacy first-row cache must not hide contradictory claims.
                if len(node["attribution_history"]) > 1:
                    node["attribution"] = "unknown"
                elif node["attribution_history"]:
                    node["attribution"] = node["attribution_history"][0]
                node["ownership_proven"] = False
            row["links"] = [dict(v) for v in self.db.execute("SELECT * FROM surface_links WHERE case_id=?", (case_id,))]
            row["link_observations"] = [dict(v) for v in self.db.execute(
                "SELECT o.* FROM link_observations o JOIN surface_links l ON l.id=o.link_id WHERE l.case_id=? ORDER BY o.rowid", (case_id,))]
            row["schema_version"] = 2
            return row

    def begin_discovery(self, case_id: str) -> dict:
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c["state"] not in {"created", "discovery_review", "scope_approved", "checks_review"}:
                raise Denied("Case is not ready for discovery")
            if db.execute("SELECT 1 FROM jobs WHERE case_id=? AND status IN ('prepared','running')", (case_id,)).fetchone():
                raise Denied("Discovery cannot overlap an active checks worker")
            run_id = uuid.uuid4().hex
            db.execute("INSERT INTO discovery_runs(id,case_id,status,started) VALUES(?,?,?,?)",
                       (run_id, case_id, "running", self.clock()))
            db.execute("UPDATE cases SET state='discovering' WHERE id=?", (case_id,))
            self.audit(db, case_id, "discovery_started", {"run_id": run_id, "scope_approval_granted": False})
        return {"id": run_id, "case_id": case_id, "status": "running"}

    def run_active(self, case_id: str, run_id: str) -> bool:
        with self.lock:
            c = self.case(self.db, case_id)
            run = self.db.execute("SELECT status FROM discovery_runs WHERE id=? AND case_id=?", (run_id, case_id)).fetchone()
            return bool(run and run["status"] == "running" and c["state"] == "discovering")

    def record_source_result(self, case_id: str, run_id: str, result: SourceResult | dict) -> str:
        result = SourceResult.model_validate(result) if isinstance(result, dict) else result
        if not isinstance(result, SourceResult):
            raise Denied("SourceResult contract required")
        if hashlib.sha256(result.body.encode("utf-8")).hexdigest() != result.body_hash:
            raise Denied("Source receipt body hash mismatch")
        if result.status not in {"success", "partial"} and (result.observations or result.relations):
            raise Denied("Failed or empty source cannot publish observations")
        with self.transaction() as db:
            self.case(db, case_id)
            if not self.run_active(case_id, run_id):
                raise Denied("Discovery run is no longer active")
            if db.execute("SELECT COUNT(*) FROM source_receipts WHERE run_id=?", (run_id,)).fetchone()[0] >= 100:
                raise Denied("Discovery receipt budget exhausted")
            receipt_id = uuid.uuid4().hex
            db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                       (receipt_id, case_id, result.request.source, result.collected_at, result.status,
                        result.body, result.body_hash, int(result.synthetic), 0))
            db.execute("INSERT INTO source_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (receipt_id, case_id, run_id, result.request.source, result.request.category,
                        result.request.url, result.request.subject, result.request.parser, result.parser_version,
                        result.collected_at, result.status, result.http_status, result.body, result.body_hash,
                        result.reason, int(result.truncated), int(result.synthetic), result.schema_version))

            def node(kind: str, value: str, attribution: str = "unknown") -> str:
                existing = db.execute("SELECT id,evidence_ids FROM surface WHERE case_id=? AND kind=? AND value=?",
                                      (case_id, kind, value)).fetchone()
                node_id = existing["id"] if existing else uuid.uuid4().hex
                if existing:
                    ids = json.loads(existing["evidence_ids"])
                    if receipt_id not in ids:
                        db.execute("UPDATE surface SET evidence_ids=? WHERE id=?", (canonical(ids + [receipt_id]), node_id))
                else:
                    db.execute("INSERT INTO surface VALUES(?,?,?,?,?,?)",
                               (node_id, case_id, kind, value, attribution, canonical([receipt_id])))
                db.execute("INSERT OR IGNORE INTO discovery_entities VALUES(?,?)", (run_id, node_id))
                return node_id

            for o in result.observations:
                node_id = node(o.kind, o.value, o.attribution)
                db.execute("INSERT INTO observation_history VALUES(?,?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, case_id, run_id, node_id, receipt_id, o.attribution,
                            o.freshness, o.pointer, result.collected_at))
            for relation in result.relations:
                source_id, target_id = node(relation.source_kind, relation.source_value), node(relation.target_kind, relation.target_value)
                existing = db.execute("SELECT id,evidence_ids FROM surface_links WHERE case_id=? AND source_id=? AND target_id=? AND relation=?",
                                      (case_id, source_id, target_id, relation.relation)).fetchone()
                link_id = existing["id"] if existing else uuid.uuid4().hex
                if existing:
                    ids = json.loads(existing["evidence_ids"])
                    if receipt_id not in ids:
                        db.execute("UPDATE surface_links SET evidence_ids=? WHERE id=?", (canonical(ids + [receipt_id]), link_id))
                else:
                    db.execute("INSERT INTO surface_links VALUES(?,?,?,?,?,?)",
                               (link_id, case_id, source_id, target_id, relation.relation, canonical([receipt_id])))
                db.execute("INSERT INTO link_observations VALUES(?,?,?,?,?,?,?,?)",
                           (uuid.uuid4().hex, run_id, link_id, receipt_id, relation.basis,
                            relation.freshness, relation.pointer, result.collected_at))
            if db.execute("SELECT COUNT(*) FROM discovery_entities WHERE run_id=?", (run_id,)).fetchone()[0] > 2000:
                raise Denied("Discovery entity budget exhausted")
            if db.execute("SELECT COUNT(DISTINCT link_id) FROM link_observations WHERE run_id=?", (run_id,)).fetchone()[0] > 4000:
                raise Denied("Discovery link budget exhausted")
            self.audit(db, case_id, "source_receipt_recorded",
                       {"run_id": run_id, "receipt_id": receipt_id, "status": result.status, "body_hash": result.body_hash})
        return receipt_id

    def finish_discovery(self, case_id: str, run_id: str, status: str = "completed", reason: str = "") -> dict:
        if status not in {"completed", "partial", "failed", "cancelled", "interrupted"}:
            raise Denied("Invalid discovery completion status")
        if len(reason) > 2000:
            raise Denied("Discovery completion reason too long")
        with self.transaction() as db:
            self.case(db, case_id)
            run = db.execute("SELECT * FROM discovery_runs WHERE id=? AND case_id=?", (run_id, case_id)).fetchone()
            if run is None:
                raise Denied("Discovery run not found in this case")
            if run["status"] not in {"pending", "running"}:
                return dict(run)
            if not self.run_active(case_id, run_id):
                raise Denied("Discovery run is no longer active")
            db.execute("UPDATE discovery_runs SET status=?,finished=?,reason=? WHERE id=?", (status, self.clock(), reason, run_id))
            db.execute("UPDATE cases SET state='discovery_review' WHERE id=?", (case_id,))
            self.audit(db, case_id, "discovery_finished", {"run_id": run_id, "status": status, "reason": reason})
            return dict(db.execute("SELECT * FROM discovery_runs WHERE id=?", (run_id,)).fetchone())

    def collect_fixture(self, case_id: str) -> dict:
        """Offline fixture; its synthetic data is never a live discovery claim."""
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c["state"] not in {"created", "discovery_review"}:
                raise Denied("Discovery fixture is unavailable in this stage")
            db.execute("UPDATE cases SET state='discovering' WHERE id=?", (case_id,))
            for prefix, attribution in [("", "seed"), ("www.", "candidate"), ("api.", "candidate")]:
                value, evidence_id = prefix + c["seed"], uuid.uuid4().hex
                body = {"hostname": value, "note": "SYNTHETIC LAB FIXTURE; not an Internet observation"}
                db.execute("INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?)",
                           (evidence_id, case_id, "fixture:ct", self.clock(), "success", canonical(body), digest(body), 1, 0))
                existing = db.execute("SELECT id,evidence_ids FROM surface WHERE case_id=? AND kind='hostname' AND value=?",
                                      (case_id, value)).fetchone()
                if existing:
                    ids = json.loads(existing["evidence_ids"]) + [evidence_id]
                    db.execute("UPDATE surface SET evidence_ids=? WHERE id=?", (canonical(ids), existing["id"]))
                else:
                    db.execute("INSERT INTO surface VALUES(?,?,?,?,?,?)",
                               (uuid.uuid4().hex, case_id, "hostname", value, attribution, canonical([evidence_id])))
            db.execute("UPDATE cases SET state='discovery_review' WHERE id=?", (case_id,))
            self.audit(db, case_id, "fixture_collected", {"synthetic": True, "network_requests": 0})
        return self.get_case(case_id)

    def propose(self, case_id: str, kind: str, body: dict, *, expected_revision: int | None = None) -> dict:
        if kind not in {"scope", "actions"}:
            raise Denied("Unknown manifest kind")
        model = Scope.model_validate(body) if kind == "scope" else ActionPlan.model_validate(body)
        data = model.model_dump(mode="json")
        with self.transaction() as db:
            if expected_revision is not None:
                self.check_revision(case_id, expected_revision)
            c = self.case(db, case_id)
            if c["state"] in {"cancelled", "completed", "paused", "discovering"}:
                raise Denied("Case is not accepting proposals")
            if kind == "scope":
                model.check_window(self.clock())
            else:
                scope_row, scope = self.approved_scope(db, case_id)
                if model.scope_version != scope_row["version"] or "poc" not in scope.authorization.actions:
                    raise Denied("Actions not covered by the approved scope/authorization")
                if model.expires_at > scope.expires_at or model.expires_at <= self.clock():
                    raise Denied("Invalid action expiration")
                if any(a.host not in {t.host for t in [*scope.targets, *scope.network_targets]} for a in model.actions):
                    raise Denied("Action target outside scope")
                if any(a.request is not None and not any(t.permits(a.request.url, a.request.method)
                       for t in scope.targets) for a in model.actions):
                    raise Denied('Action request outside approved service, path or method')
                if c["state"] != "checks_review":
                    raise Denied("Actions can only be proposed after checks review")
            version = db.execute("SELECT COALESCE(MAX(version),0)+1 FROM manifests WHERE case_id=? AND kind=?",
                                 (case_id, kind)).fetchone()[0]
            manifest_id, manifest_hash = uuid.uuid4().hex, digest(data)
            db.execute("UPDATE manifests SET status='superseded' WHERE case_id=? AND kind=? AND status='pending'", (case_id, kind))
            db.execute("INSERT INTO manifests(id,case_id,kind,version,body,hash,status) VALUES(?,?,?,?,?,?,?)",
                       (manifest_id, case_id, kind, version, canonical(data), manifest_hash, "pending"))
            if kind == "scope":
                # A change never leaves an older worker running while a new approval is pending.
                self.revoke_in(db, case_id, "scope_changed")
                db.execute("UPDATE cases SET state='discovery_review' WHERE id=?", (case_id,))
            self.audit(db, case_id, "manifest_proposed", {"kind": kind, "version": version, "hash": manifest_hash})
        return {"id": manifest_id, "case_id": case_id, "kind": kind, "version": version,
                "hash": manifest_hash, "manifest": data, "status": "pending"}

    def activate_from_task(self, case_id: str, manifest_id: str, kind: str) -> dict:
        """The authenticated chat delegate activates a saved plan from the operator's task.

        This is delegated agent authority, not a claim of an independently
        authenticated UI click. The agent must have an explicit operator
        command for this stage; discovery alone grants no checks or PoC.
        """
        if kind not in {"scope", "actions"}:
            raise Denied("Unknown manifest kind")
        with self.transaction() as db:
            c = self.case(db, case_id)
            m = db.execute("SELECT * FROM manifests WHERE id=? AND case_id=? AND kind=?",
                           (manifest_id, case_id, kind)).fetchone()
            if not m or m["status"] != "pending" or digest(json.loads(m["body"])) != m["hash"]:
                raise Denied("No unchanged pending manifest for this task")
            if c["state"] in {"paused", "cancelled", "completed", "discovering"}:
                raise Denied("Case is not accepting task activation")
            if kind == "scope":
                scope = Scope.model_validate_json(m["body"])
                scope.check_window(self.clock())
            else:
                scope_row, scope = self.approved_scope(db, case_id)
                actions = ActionPlan.model_validate_json(m["body"])
                if (c["state"] != "checks_review" or actions.scope_version != scope_row["version"]
                    or actions.expires_at <= self.clock() or "poc" not in scope.authorization.actions
                    or any(a.host not in {t.host for t in [*scope.targets, *scope.network_targets]} for a in actions.actions)):
                    raise Denied("Actions no longer authorized by this scope/stage")
            client = delegate_client.get()
            actor = "delegated:" + client + ":" + self.principal
            source = client + "_operator_delegation"
            db.execute("UPDATE manifests SET status='approved',approved_by=?,approved_at=? WHERE id=?",
                       (actor, self.clock(), manifest_id))
            state = "scope_approved" if kind == "scope" else "actions_approved"
            db.execute("UPDATE cases SET state=? WHERE id=?", (state, case_id))
            self.audit(db, case_id, "activated_from_operator_task",
                       {"manifest_id": manifest_id, "hash": m["hash"], "kind": kind,
                        "source": source, "delegate": actor,
                        "authorization_ref": scope.authorization.reference,
                        "independent_human_receipt": False})
        return {"manifest_id": manifest_id, "status": "approved", "approved_by": actor,
                "authorization_source": source}

    def approved_scope(self, db: sqlite3.Connection, case_id: str) -> tuple[sqlite3.Row, Scope]:
        row = db.execute("SELECT * FROM manifests WHERE case_id=? AND kind='scope' ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
        if not row or row["status"] != "approved":
            raise Denied("No currently approved scope")
        if digest(json.loads(row['body'])) != row['hash']:
            raise Denied('Approved scope integrity failure')
        scope = Scope.model_validate_json(row["body"])
        scope.check_window(self.clock())
        return row, scope

    def approved_action(self, db, case_id: str, manifest_id: str, finding_id: str):
        scope_row, scope = self.approved_scope(db, case_id)
        row = db.execute("SELECT * FROM manifests WHERE case_id=? AND kind='actions' ORDER BY version DESC LIMIT 1",
                         (case_id,)).fetchone()
        if not row or row['id'] != manifest_id or row['status'] != 'approved' or digest(json.loads(row['body'])) != row['hash']:
            raise Denied('No matching current approved action plan')
        plan = ActionPlan.model_validate_json(row['body'])
        if (plan.scope_version != scope_row['version'] or plan.expires_at <= self.clock()
                or 'poc' not in scope.authorization.actions):
            raise Denied('Action plan expired or bound to a different scope')
        action = next((a for a in plan.actions if a.finding_id == finding_id), None)
        if action is None or action.request is None:
            raise Denied('No executable request saved for this finding; propose an exact action request')
        if not any(t.permits(action.request.url, action.request.method) for t in scope.targets):
            raise Denied('Action request outside approved HTTP scope')
        if self.clock() + action.request.seconds > plan.expires_at:
            raise Denied('Action request exceeds action expiration')
        return row, action

    def action_context(self, case_id: str, manifest_id: str, finding_id: str):
        with self.lock:
            case = self.case(self.db, case_id)
            if case['state'] not in {'actions_approved', 'checks_review'}:
                raise Denied('Case is not in the separately activated actions stage')
            row, scope = self.approved_scope(self.db, case_id)
            action_row, action = self.approved_action(self.db, case_id, manifest_id, finding_id)
            return self.export(case_id), dict(row), dict(action_row), action

    def revoke_in(self, db: sqlite3.Connection, case_id: str, reason: str) -> None:
        db.execute("UPDATE cases SET generation=generation+1 WHERE id=?", (case_id,))
        db.execute("UPDATE jobs SET status='revoked' WHERE case_id=? AND status IN ('prepared','running')", (case_id,))
        generation = self.case(db, case_id)["generation"]
        db.execute("INSERT INTO outbox(case_id,generation,event) VALUES(?,?,?)", (case_id, generation, "revoke"))
        self.audit(db, case_id, "revoked", {"generation": generation, "reason": reason})

    def stop(self, case_id: str) -> dict:
        with self.transaction() as db:
            self.case(db, case_id)
            self.revoke_in(db, case_id, "operator_stop")
            db.execute("UPDATE discovery_runs SET status='cancelled',finished=?,reason='Operator stop' "
                       "WHERE case_id=? AND status IN ('pending','running')", (self.clock(), case_id))
            db.execute("UPDATE cases SET state='paused' WHERE id=?", (case_id,))
        return self.get_case(case_id)

    def resume(self, case_id: str) -> dict:
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c["state"] != "paused":
                raise Denied("Only paused cases can resume")
            latest = db.execute("SELECT status FROM manifests WHERE case_id=? AND kind='scope' ORDER BY version DESC LIMIT 1",
                                (case_id,)).fetchone()
            if latest is None or latest["status"] != "approved":
                db.execute("UPDATE cases SET state='discovery_review' WHERE id=?", (case_id,))
                self.audit(db, case_id, "resumed", {"stage": "discovery_review", "automatic_collection": False})
                return self.get_case(case_id)
            row, _ = self.approved_scope(db, case_id)
            # PoC is never resumed automatically after an uncertain outcome.
            db.execute("UPDATE cases SET state='scope_approved' WHERE id=?", (case_id,))
            self.audit(db, case_id, "resumed", {"scope_version": row["version"], "spent": c["spent"]})
        return self.get_case(case_id)

    def prepare_job(self, case_id: str, worker: str, profile: str, seconds: int, *, expected_binding: dict | None = None,
                    action_binding: dict | None = None) -> tuple[dict, str]:
        """Internal dispatcher API; token is not included in model-facing job status."""
        if profile not in {"target_http", "target_network"} or not 1 <= seconds <= 1800:
            raise Denied("Invalid job profile or duration")
        with self.transaction() as db:
            c = self.case(db, case_id)
            row, scope = self.approved_scope(db, case_id)
            binding = {'scope_id': row['id'], 'scope_hash': row['hash'], 'generation': c['generation']}
            if expected_binding is not None and expected_binding != binding:
                raise Denied('Pentest session scope/generation binding is stale')
            if action_binding is not None:
                action_row, action = self.approved_action(db, case_id, action_binding['action_manifest_id'], action_binding['finding_id'])
                exact = {'task_stage': 'poc', 'action_manifest_id': action_row['id'], 'action_hash': action_row['hash'],
                         'finding_id': action.finding_id}
                if action_binding != exact or c['state'] not in {'actions_approved', 'checks_review'} or seconds != action.request.seconds:
                    raise Denied('Action execution binding/stage/duration mismatch')
                # A new activation is required for a repeated demonstration.
                for previous in db.execute('SELECT plan FROM job_runtime r JOIN jobs j ON j.id=r.job_id WHERE j.case_id=?', (case_id,)):
                    prior = json.loads(previous['plan']) if previous['plan'] else {}
                    if all(prior.get(key) == value for key, value in exact.items()):
                        raise Denied('This exact action was already attempted; do not automatically replay it')
            elif c["state"] not in {"scope_approved", "checking", "checks_review"}:
                raise Denied("Case not ready for checks")
            jobs = db.execute("SELECT COUNT(*) FROM jobs WHERE case_id=?", (case_id,)).fetchone()[0]
            if seconds + c["spent"] > scope.budget.seconds or jobs >= scope.budget.jobs:
                raise Denied("Case budget exhausted")
            if db.execute("SELECT 1 FROM jobs WHERE case_id=? AND status IN ('prepared','running')", (case_id,)).fetchone():
                raise Denied("Initial heavy-job concurrency is one")
            job_id, token = uuid.uuid4().hex, secrets.token_urlsafe(32)
            deadline = self.clock() + min(seconds, scope.expires_at - self.clock())
            db.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (job_id, case_id, c["generation"], row["id"], profile, worker, "prepared",
                        hashlib.sha256(token.encode()).hexdigest(), min(self.clock()+30, deadline), deadline, seconds))
            db.execute("INSERT INTO job_runtime(job_id,plan) VALUES(?,?)", (job_id, canonical(action_binding or {})))
            db.execute("UPDATE cases SET spent=spent+?,state=? WHERE id=?", (seconds, 'poc_running' if action_binding else 'checking', case_id))
            self.audit(db, case_id, "job_prepared", {"job_id": job_id, "generation": c["generation"]})
            return {"id": job_id, "generation": c["generation"], "scope_hash": row["hash"], "deadline": deadline}, token

    def validate_capability(self, job_id: str, token: str, worker: str, profile: str) -> dict:
        with self.lock:
            job = self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not job or job["worker"] != worker or job["profile"] != profile:
                raise Denied("Capability belongs to a different worker/profile")
            if not secrets.compare_digest(job["token_hash"], hashlib.sha256(token.encode()).hexdigest()):
                raise Denied("Invalid capability")
            c = self.case(self.db, job["case_id"])
            row, _ = self.approved_scope(self.db, c["id"])
            runtime = self.db.execute('SELECT plan FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            plan = json.loads(runtime['plan']) if runtime and runtime['plan'] else {}
            states = {'checking', 'scope_approved'}
            if plan.get('task_stage') == 'poc':
                action_row, _ = self.approved_action(self.db, c['id'], plan['action_manifest_id'], plan['finding_id'])
                if plan.get('action_hash') != action_row['hash']:
                    raise Denied('Action capability hash mismatch')
                states = {'poc_running'}
            if (job["generation"] != c["generation"] or row["id"] != job["scope_id"]
                or c["state"] not in states
                or digest(json.loads(row["body"])) != row["hash"]
                or job["status"] not in {"prepared", "running"} or self.clock() >= min(job["lease_until"], job["deadline"])):
                raise Denied("Expired/revoked capability")
            return dict(job)

    def native_execution_record(self, case_id: str, job_id: str, token: str, worker: str,
                                profile: str, run_id: str, targets: list[str]) -> dict:
        """Internal admission projection; no capability material reaches the model."""
        from .native_handoff import execution_record
        with self.lock:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = self.db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if (job['case_id'] != case_id or job['status'] != 'prepared' or worker != 'existing-pentestgpt-compose'
                    or run_id != 'chat-' + job_id
                    or runtime['policy_hash'] is not None or json.loads(runtime['plan']).get('task_stage') == 'poc'):
                raise Denied('Native checks handoff requires its own prepared checks reservation')
            case = dict(self.case(self.db, case_id))
            manifest, _ = self.approved_scope(self.db, case_id)
            jobs = self.db.execute('SELECT COUNT(*) FROM jobs WHERE case_id=?', (case_id,)).fetchone()[0]
            record = execution_record(snapshot=case, manifest=dict(manifest), targets=targets,
                job_id=job_id, run_id=run_id, seconds=job['duration'], deadline=job['deadline'],
                now=self.clock(), spent=case['spent'], jobs=jobs)
            if record['profile'] != profile:
                raise Denied('Native selection differs from reserved job profile')
            return record

    def checks_context(self, case_id: str) -> tuple[dict, dict]:
        """Internal immutable snapshot; capabilities never enter model context."""
        with self.lock:
            c = self.case(self.db, case_id)
            row, _ = self.approved_scope(self.db, case_id)
            if c['state'] not in {'scope_approved', 'checking', 'checks_review'}:
                raise Denied('Case not ready for checks context')
            if digest(json.loads(row['body'])) != row['hash']:
                raise Denied('Approved scope integrity failure')
            return self.export(case_id), dict(row)

    def bind_job_policy(self, job_id: str, token: str, worker: str, profile: str,
                        policy_hash: str, plan: dict) -> dict:
        """Seal dispatcher-produced policy before an installer receives it."""
        if len(policy_hash) != 64 or any(c not in '0123456789abcdef' for c in policy_hash):
            raise Denied('Invalid policy hash')
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if job['status'] != 'prepared' or runtime['policy_hash'] is not None:
                raise Denied('Job policy already sealed or job started')
            db.execute('UPDATE job_runtime SET policy_hash=?,plan=? WHERE job_id=?', (policy_hash, canonical(plan), job_id))
            self.audit(db, job['case_id'], 'job_policy_bound', {'job_id': job_id, 'policy_hash': policy_hash})
            return self.job_lease(job_id, token, worker, profile)

    def job_lease(self, job_id: str, token: str, worker: str, profile: str) -> dict:
        with self.lock:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = self.db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            scope = self.db.execute('SELECT hash FROM manifests WHERE id=?', (job['scope_id'],)).fetchone()
            return {'job_id': job_id, 'generation': job['generation'], 'scope_hash': scope['hash'],
                    'sequence': runtime['lease_sequence'], 'valid_until': job['lease_until'], 'revoked': False}

    def acknowledge_job(self, job_id: str, token: str, worker: str, profile: str, acknowledgement: dict) -> None:
        """Internal installer ACK, not a model/MCP argument."""
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            expected = {**self.job_lease(job_id, token, worker, profile), 'policy_hash': runtime['policy_hash']}
            if runtime['policy_hash'] is None or acknowledgement != expected or runtime['acknowledged_sequence'] >= runtime['lease_sequence']:
                raise Denied('Missing, stale or mismatched network policy acknowledgement')
            db.execute('UPDATE job_runtime SET acknowledged_sequence=? WHERE job_id=?', (runtime['lease_sequence'], job_id))
            self.audit(db, job['case_id'], 'job_network_acknowledged', {'job_id': job_id, 'sequence': runtime['lease_sequence']})

    def start_job(self, job_id: str, token: str, worker: str, profile: str) -> None:
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if job['status'] != 'prepared' or not runtime['policy_hash'] or runtime['acknowledged_sequence'] != runtime['lease_sequence']:
                raise Denied('Network policy must be installed and acknowledged before worker start')
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            db.execute('UPDATE job_runtime SET started=? WHERE job_id=?', (self.clock(), job_id))
            self.audit(db, job['case_id'], 'job_started', {'job_id': job_id})

    def start_native_job(self, job_id: str, token: str, worker: str, profile: str, plan: dict) -> None:
        """Host-only admission of the existing PentestGPT CLI, without a network ACK.

        This deliberately records a different execution mode. Its launcher
        allowlist and checks prompt do not enforce HTTP paths or native shell
        effects. No model-facing endpoint accepts this plan or the job token.
        """
        if plan.get('execution_mode') != 'existing_compose_runtime' or plan.get('reduced_scope_enforced') is not False:
            raise Denied('Expected an explicit native runtime admission contract')
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if job['status'] != 'prepared' or runtime['policy_hash'] is not None:
                raise Denied('Native job already bound or started')
            prior = json.loads(runtime['plan']) if runtime['plan'] else {}
            if prior.get('task_stage') == 'poc' and any(plan.get(key) != value for key, value in prior.items()):
                raise Denied('Native plan changed the activated action binding')
            if plan.get('tool') == 'pentestgpt-agent':
                from .native_handoff import NativeExecution
                record = NativeExecution.model_validate(plan.get('native_execution'))
                current = self.native_execution_record(job['case_id'], job_id, token, worker, profile,
                                                       plan['run_id'], plan['execution_targets'])
                data = record.model_dump(mode='json')
                if (record.snapshot_at > current['snapshot_at'] or any(data[key] != value for key, value in current.items()
                        if key not in {'snapshot_at', 'remaining_seconds_at_snapshot'})):
                    raise Denied('Native handoff differs from the actual Controller reservation/accounting')
            db.execute('UPDATE job_runtime SET policy_hash=?,plan=?,started=? WHERE job_id=?',
                       (digest(plan), canonical(plan), self.clock(), job_id))
            db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            self.audit(db, job['case_id'], 'native_runtime_admitted', {'job_id': job_id,
                'execution_mode': 'existing_compose_runtime', 'network_ack': False,
                'phase_enforcement': plan.get('phase_enforcement', 'prompt_only'), 'reduced_scope_enforced': False})

    def renew_native_job(self, job_id: str, token: str, worker: str, profile: str) -> None:
        """Refresh Controller accounting only; this is not a firewall receipt."""
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if job['status'] != 'running' or json.loads(runtime['plan']).get('execution_mode') != 'existing_compose_runtime':
                raise Denied('Not a running native PentestGPT job')
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (min(self.clock()+30, job['deadline']), job_id))

    def renew_job(self, job_id: str, token: str, worker: str, profile: str) -> dict:
        with self.transaction() as db:
            job = self.validate_capability(job_id, token, worker, profile)
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if job['status'] != 'running' or runtime['acknowledged_sequence'] != runtime['lease_sequence']:
                raise Denied('Previous lease not installed or job not running')
            db.execute('UPDATE jobs SET lease_until=? WHERE id=?', (min(self.clock()+30, job['deadline']), job_id))
            db.execute('UPDATE job_runtime SET lease_sequence=lease_sequence+1 WHERE job_id=?', (job_id,))
            return self.job_lease(job_id, token, worker, profile)

    def finish_job(self, case_id: str, job_id: str, *, outcome: str, result: dict, synthetic: bool) -> dict:
        """Trusted dispatcher commits bounded, already sanitized worker evidence."""
        if outcome not in {'completed', 'failed', 'interrupted'} or type(synthetic) is not bool:
            raise Denied('Invalid job result')
        body = canonical(result)
        if len(body.encode('utf-8')) > 65536:
            raise Denied('Worker result too large')
        with self.transaction() as db:
            c = self.case(db, case_id)
            job = db.execute('SELECT * FROM jobs WHERE id=? AND case_id=?', (job_id, case_id)).fetchone()
            runtime = db.execute('SELECT * FROM job_runtime WHERE job_id=?', (job_id,)).fetchone()
            if not job or not runtime:
                raise Denied('Job not found in this case')
            if runtime['finished'] is not None:
                return self.get_job_status(case_id, job_id)
            # Revoked/expired or uncertain outcomes cannot become a success.
            if job['status'] not in {'prepared', 'running'} or job['generation'] != c['generation'] or self.clock() >= min(job['lease_until'], job['deadline']):
                outcome = 'interrupted'
            if outcome == 'completed' and job['status'] != 'running':
                raise Denied('An unstarted worker cannot report completion')
            evidence_id = uuid.uuid4().hex
            db.execute('INSERT INTO evidence(id,case_id,source,at,status,body,hash,synthetic) VALUES(?,?,?,?,?,?,?,?)',
                       (evidence_id, case_id, 'worker:' + job['profile'], self.clock(), outcome, body, digest(result), int(synthetic)))
            db.execute('UPDATE job_runtime SET finished=?,outcome=?,evidence_id=? WHERE job_id=?', (self.clock(), outcome, evidence_id, job_id))
            db.execute('UPDATE jobs SET status=? WHERE id=?', (outcome, job_id))
            if (c['state'] in {'checking', 'poc_running'} and job['generation'] == c['generation']
                and not db.execute("SELECT 1 FROM jobs WHERE case_id=? AND id<>? AND status IN ('prepared','running')", (case_id, job_id)).fetchone()):
                db.execute("UPDATE cases SET state='checks_review' WHERE id=?", (case_id,))
            self.audit(db, case_id, 'job_finished', {'job_id': job_id, 'outcome': outcome, 'evidence_id': evidence_id, 'synthetic': synthetic})
            return self.get_job_status(case_id, job_id)

    def get_job_status(self, case_id: str, job_id: str) -> dict:
        with self.lock:
            jobs = self.get_case(case_id)['jobs']
            job = next((j for j in jobs if j['id'] == job_id), None)
            if job is None:
                raise Denied('Job not found in this case')
            return job

    def acknowledge_revocation(self, case_id: str, generation: int, closed_job_ids: list[str]) -> None:
        """Internal launcher receipt after worker closure; never an MCP tool."""
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c['generation'] != generation:
                raise Denied('Stale revocation acknowledgement')
            required = {r[0] for r in db.execute('SELECT id FROM jobs WHERE case_id=? AND generation<?', (case_id, generation))}
            if not required.issubset(set(closed_job_ids)):
                raise Denied('Revocation receipt omits workers')
            db.execute("UPDATE outbox SET delivered=1 WHERE case_id=? AND generation<=? AND event='revoke'", (case_id, generation))
            self.audit(db, case_id, 'job_access_closed', {'generation': generation, 'closed_job_ids': sorted(set(closed_job_ids))})

    @staticmethod
    def integration_status() -> dict:
        return {"version": "0.3.0", "live_execution": False,
                "public_source_discovery": True, "discovery_revision": 1,
                "target_http_acquisition": False, "live_checks": False,
                "typed_checks_planner": True, "checks_dispatcher_revision": 1,
                "pentest_context_revision": 1, "production_worker_backend": False,
                "pentest_role_facade_revision": 1, "pentest_scripted_adapter_revision": 1,
                "target_dns_revalidation_revision": 1, "real_model_bridge_qualified": False,
                "fixture_dispatch": "synthetic_zero_network_only",
                "task_authorization_revision": 1,
                "mcp_lifecycle_revision": 2,
                "chat_authorization": delegate_client.get() + "_operator_delegation",
                "linux_network_enforcement": "direct_worker_candidate_unqualified",
                "tls_interception": False,
                "reason": "Public-source discovery, typed checks dispatch, bound role facade and laboratory scripted PentestGPT adapter available locally; real-model tool isolation, production worker qualification and network scope remain incomplete"}

    def export(self, case_id: str) -> dict:
        with self.lock:
            result = self.get_case(case_id)
            result["evidence"] = [dict(v) for v in self.db.execute("SELECT * FROM evidence WHERE case_id=?", (case_id,))]
            result["source_receipts"] = [dict(v) for v in self.db.execute(
                "SELECT * FROM source_receipts WHERE case_id=? ORDER BY rowid", (case_id,))]
            result["audit"] = [dict(v) for v in self.db.execute("SELECT * FROM audit WHERE case_id=? ORDER BY sequence", (case_id,))]
            result["limitations"] = self.integration_status()
            return result
