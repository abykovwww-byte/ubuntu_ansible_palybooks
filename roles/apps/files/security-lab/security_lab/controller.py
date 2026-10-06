"""Single-writer, persistent cases and single-use approval challenges."""

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
        if os.name == "nt":
            import msvcrt

            self.owner_file.seek(0)
            if not self.owner_file.read(1):
                self.owner_file.write(b"0")
                self.owner_file.flush()
            self.owner_file.seek(0)
            msvcrt.locking(self.owner_file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(self.owner_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.db = sqlite3.connect(self.root / "control.db", timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, 1}:
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
        CREATE TABLE IF NOT EXISTS challenges (
          hash TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
          manifest_id TEXT NOT NULL REFERENCES manifests(id), owner TEXT NOT NULL,
          session TEXT NOT NULL, generation INTEGER NOT NULL, expires REAL NOT NULL,
          consumed INTEGER NOT NULL DEFAULT 0
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
        PRAGMA user_version=1;
        """)

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
        db.execute("INSERT INTO audit(case_id,at,event,actor,details) VALUES(?,?,?,?,?)",
                   (case_id, self.clock(), event, self.principal, canonical(details)))

    def case(self, db: sqlite3.Connection, case_id: str) -> sqlite3.Row:
        row = db.execute("SELECT * FROM cases WHERE id=? AND owner=?", (case_id, self.principal)).fetchone()
        if row is None:
            raise Denied("Case not found or not owned by this principal")
        return row

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
            row["surface"] = [dict(v) for v in self.db.execute("SELECT * FROM surface WHERE case_id=?", (case_id,))]
            row["manifests"] = [dict(v) for v in self.db.execute(
                "SELECT id,kind,version,hash,status,approved_by,approved_at FROM manifests WHERE case_id=?", (case_id,))]
            row["jobs"] = [dict(v) for v in self.db.execute(
                "SELECT id,profile,status,generation,lease_until,deadline FROM jobs WHERE case_id=?", (case_id,))]
            row["schema_version"] = 1
            return row

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

    def propose(self, case_id: str, kind: str, body: dict) -> dict:
        if kind not in {"scope", "actions"}:
            raise Denied("Unknown manifest kind")
        model = Scope.model_validate(body) if kind == "scope" else ActionPlan.model_validate(body)
        data = model.model_dump(mode="json")
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c["state"] in {"cancelled", "completed", "paused"}:
                raise Denied("Case is not accepting proposals")
            if kind == "scope":
                model.check_window(self.clock())
            else:
                scope_row, scope = self.approved_scope(db, case_id)
                if model.scope_version != scope_row["version"] or "poc" not in scope.authorization.actions:
                    raise Denied("Actions not covered by the approved scope/authorization")
                if model.expires_at > scope.expires_at or model.expires_at <= self.clock():
                    raise Denied("Invalid action expiration")
                if any(a.host not in {t.host for t in scope.targets} for a in model.actions):
                    raise Denied("Action target outside scope")
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

    def challenge(self, case_id: str, manifest_id: str, session: str) -> dict:
        with self.transaction() as db:
            c = self.case(db, case_id)
            m = db.execute("SELECT * FROM manifests WHERE id=? AND case_id=? AND status='pending'", (manifest_id, case_id)).fetchone()
            if not m or c["state"] in {"paused", "cancelled", "completed"}:
                raise Denied("No approvable pending manifest")
            secret = secrets.token_urlsafe(32)
            db.execute("INSERT INTO challenges VALUES(?,?,?,?,?,?,?,?)",
                       (hashlib.sha256(secret.encode()).hexdigest(), case_id, manifest_id, self.principal,
                        session, c["generation"], self.clock() + 300, 0))
            self.audit(db, case_id, "confirmation_requested", {"manifest_id": manifest_id, "hash": m["hash"]})
        return {"challenge": secret, "manifest_id": manifest_id, "hash": m["hash"],
                "kind": m["kind"], "version": m["version"], "manifest": json.loads(m["body"])}

    def record_unconfirmed_response(self, challenge: str, session: str, status: str,
                                    reason: str, diagnostics: dict) -> dict:
        """Consume a failed attempt without inventing a human decision.

        Timeouts/stops/changed manifests still need an audit outcome, so this
        path checks ownership and session but never grants or changes a scope.
        Only the internal transport handler calls this method.
        """
        if status not in {"confirmation_unavailable", "confirmation_not_approved"}:
            raise Denied("Invalid transport outcome")
        with self.transaction() as db:
            h = hashlib.sha256(challenge.encode()).hexdigest()
            r = db.execute("SELECT * FROM challenges WHERE hash=?", (h,)).fetchone()
            if not r or r["consumed"] or r["owner"] != self.principal or r["session"] != session:
                raise Denied("Invalid or already consumed challenge")
            self.case(db, r["case_id"])
            m = db.execute("SELECT * FROM manifests WHERE id=?", (r["manifest_id"],)).fetchone()
            db.execute("UPDATE challenges SET consumed=1 WHERE hash=?", (h,))
            self.audit(db, r["case_id"], status,
                {"manifest_id": m["id"], "hash": m["hash"], "transport": "mcp_elicitation",
                 "reason": reason, "diagnostics": diagnostics, "human_decision_observed": False})
        return {"manifest_id": m["id"], "status": status, "manifest_status": m["status"],
                "approved": False, "approved_by": None, "reason": reason,
                "diagnostics": diagnostics, "human_decision_observed": False}

    def resolve_confirmation(self, challenge: str, session: str, accepted: bool, *,
                             transport: str, diagnostics: dict | None = None) -> dict:
        """Internal protocol callback only. Never expose accepted/transport as MCP arguments."""
        if transport != "mcp_elicitation":
            raise Denied("A trusted elicitation response is required")
        with self.transaction() as db:
            h = hashlib.sha256(challenge.encode()).hexdigest()
            r = db.execute("SELECT * FROM challenges WHERE hash=?", (h,)).fetchone()
            if not r or r["consumed"] or r["expires"] <= self.clock() or r["owner"] != self.principal or r["session"] != session:
                raise Denied("Invalid, expired or already consumed challenge")
            c = self.case(db, r["case_id"])
            m = db.execute("SELECT * FROM manifests WHERE id=?", (r["manifest_id"],)).fetchone()
            if c["generation"] != r["generation"] or m["status"] != "pending" or digest(json.loads(m["body"])) != m["hash"]:
                raise Denied("Manifest changed or revoked")
            if c["state"] in {"paused", "cancelled", "completed"}:
                raise Denied("Case stopped while confirmation was pending")
            if m["kind"] == "scope":
                Scope.model_validate_json(m["body"]).check_window(self.clock())
            else:
                scope_row, scope = self.approved_scope(db, c["id"])
                actions = ActionPlan.model_validate_json(m["body"])
                if actions.scope_version != scope_row["version"] or actions.expires_at <= self.clock() or "poc" not in scope.authorization.actions:
                    raise Denied("Actions no longer authorized")
            db.execute("UPDATE challenges SET consumed=1 WHERE hash=?", (h,))
            status = "approved" if accepted else "rejected"
            db.execute("UPDATE manifests SET status=?,approved_by=?,approved_at=? WHERE id=?",
                       (status, self.principal if accepted else None, self.clock() if accepted else None, m["id"]))
            if accepted:
                state = "scope_approved" if m["kind"] == "scope" else "actions_approved"
                db.execute("UPDATE cases SET state=? WHERE id=?", (state, c["id"]))
            self.audit(db, c["id"], "confirmation_" + status,
                       {"manifest_id": m["id"], "hash": m["hash"], "transport": transport,
                        "diagnostics": diagnostics or {}})
        return {"manifest_id": m["id"], "status": status, "approved_by": self.principal if accepted else None}

    def approved_scope(self, db: sqlite3.Connection, case_id: str) -> tuple[sqlite3.Row, Scope]:
        row = db.execute("SELECT * FROM manifests WHERE case_id=? AND kind='scope' ORDER BY version DESC LIMIT 1", (case_id,)).fetchone()
        if not row or row["status"] != "approved":
            raise Denied("No currently approved scope")
        scope = Scope.model_validate_json(row["body"])
        scope.check_window(self.clock())
        return row, scope

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
            db.execute("UPDATE cases SET state='paused' WHERE id=?", (case_id,))
        return self.get_case(case_id)

    def resume(self, case_id: str) -> dict:
        with self.transaction() as db:
            c = self.case(db, case_id)
            if c["state"] != "paused":
                raise Denied("Only paused cases can resume")
            row, _ = self.approved_scope(db, case_id)
            # PoC is never resumed automatically after an uncertain outcome.
            db.execute("UPDATE cases SET state='scope_approved' WHERE id=?", (case_id,))
            self.audit(db, case_id, "resumed", {"scope_version": row["version"], "spent": c["spent"]})
        return self.get_case(case_id)

    def prepare_job(self, case_id: str, worker: str, profile: str, seconds: int) -> tuple[dict, str]:
        """Internal dispatcher API; token is not included in model-facing job status."""
        if profile not in {"target_http", "target_network"} or not 1 <= seconds <= 1800:
            raise Denied("Invalid job profile or duration")
        with self.transaction() as db:
            c = self.case(db, case_id)
            row, scope = self.approved_scope(db, case_id)
            if c["state"] not in {"scope_approved", "checking"}:
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
            if (job["generation"] != c["generation"] or row["id"] != job["scope_id"]
                or job["status"] not in {"prepared", "running"} or self.clock() >= min(job["lease_until"], job["deadline"])):
                raise Denied("Expired/revoked capability")
            return dict(job)

    def integration_status(self) -> dict:
        return {"version": "0.1.0", "live_execution": False,
                "confirmation_protocol_revision": 2,
                "codex_desktop_confirmation": "requires_actual_client_probe",
                "linux_network_enforcement": "requires_linux_lab",
                "reason": "No live job is dispatched before the Codex and network acceptance gates pass"}

    def export(self, case_id: str) -> dict:
        with self.lock:
            result = self.get_case(case_id)
            result["evidence"] = [dict(v) for v in self.db.execute("SELECT * FROM evidence WHERE case_id=?", (case_id,))]
            result["audit"] = [dict(v) for v in self.db.execute("SELECT * FROM audit WHERE case_id=? ORDER BY sequence", (case_id,))]
            result["limitations"] = self.integration_status()
            return result
