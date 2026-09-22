"""Persistent Case records. Model output never writes trusted state or verdict fields."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CaseStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, confirmed_state TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id), run_id TEXT,
                    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verdicts (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    evidence TEXT NOT NULL DEFAULT '[]',
                    details TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS decisions (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    ruleset_id TEXT NOT NULL, status TEXT NOT NULL,
                    reason_codes TEXT NOT NULL, rule_ids TEXT NOT NULL,
                    derived_facts TEXT NOT NULL, missing_fields TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verifications (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    decision_id TEXT, ruleset_id TEXT NOT NULL,
                    ruleset_version_ref TEXT, status TEXT NOT NULL,
                    rule_ids TEXT NOT NULL, evidence TEXT NOT NULL,
                    checks TEXT NOT NULL, created_at TEXT NOT NULL
                );
            """)
            verdict_columns = {row["name"] for row in db.execute("PRAGMA table_info(verdicts)")}
            if "evidence" not in verdict_columns:
                db.execute("ALTER TABLE verdicts ADD COLUMN evidence TEXT NOT NULL DEFAULT '[]'")
            if "details" not in verdict_columns:
                db.execute("ALTER TABLE verdicts ADD COLUMN details TEXT NOT NULL DEFAULT '{}'")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self) -> dict[str, Any]:
        case_id = str(uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO cases(id, created_at) VALUES (?, ?)", (case_id, _now()))
        case = self.get(case_id)
        assert case is not None
        return case

    def exists(self, case_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None

    def add_message(self, case_id: str, text: str) -> str:
        message_id = str(uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages(id, case_id, text, created_at) VALUES (?, ?, ?, ?)",
                (message_id, case_id, text, _now()),
            )
        return message_id

    def add_event(self, case_id: str, run_id: str, event_type: str, **payload):
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(case_id, run_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (case_id, run_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
            )

    def add_decision(
        self, case_id: str, run_id: str, decision: dict[str, Any], ruleset_id: str
    ) -> dict[str, Any]:
        decision_id = str(uuid4())
        with self._connect() as db:
            revision = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            db.execute(
                """INSERT INTO decisions
                (id, case_id, run_id, revision, ruleset_id, status, reason_codes,
                 rule_ids, derived_facts, missing_fields, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    case_id,
                    run_id,
                    revision,
                    ruleset_id,
                    decision["status"],
                    json.dumps(decision.get("reason_codes", []), ensure_ascii=False),
                    json.dumps(decision.get("rule_ids", []), ensure_ascii=False),
                    json.dumps(decision.get("derived_facts", {}), ensure_ascii=False),
                    json.dumps(decision.get("missing_fields", []), ensure_ascii=False),
                    _now(),
                ),
            )
        return {
            **decision,
            "id": decision_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": revision,
        }

    def add_verification(
        self,
        case_id: str,
        run_id: str,
        verification: dict[str, Any],
        *,
        decision_id: str | None,
    ) -> dict[str, Any]:
        verification_id = verification.get("id") or str(uuid4())
        with self._connect() as db:
            revision = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            db.execute(
                """INSERT INTO verifications
                (id, case_id, run_id, revision, decision_id, ruleset_id,
                 ruleset_version_ref, status, rule_ids, evidence, checks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verification_id,
                    case_id,
                    run_id,
                    revision,
                    decision_id,
                    verification["ruleset_id"],
                    verification.get("ruleset_version_ref"),
                    verification["status"],
                    json.dumps(verification.get("rule_ids", []), ensure_ascii=False),
                    json.dumps(verification.get("evidence", []), ensure_ascii=False),
                    json.dumps(verification.get("checks", {}), ensure_ascii=False),
                    _now(),
                ),
            )
        return {
            **verification,
            "id": verification_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": revision,
            "decision_id": decision_id,
        }

    def add_verdict(
        self,
        case_id: str,
        run_id: str,
        status: str,
        reason: str,
        *,
        evidence: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        verdict_id = str(uuid4())
        evidence = evidence or []
        details = details or {}
        with self._connect() as db:
            revision = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            db.execute(
                """INSERT INTO verdicts
                (id, case_id, run_id, revision, status, reason, created_at, evidence, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verdict_id,
                    case_id,
                    run_id,
                    revision,
                    status,
                    reason,
                    _now(),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO events(case_id, run_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    case_id,
                    run_id,
                    "verdict",
                    json.dumps({"verdict_id": verdict_id, "status": status}),
                    _now(),
                ),
            )
        return {
            "id": verdict_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": revision,
            "status": status,
            "reason": reason,
            "evidence": evidence,
            "details": details,
        }

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                return None
            messages = [
                dict(item)
                for item in db.execute(
                    "SELECT id, text, created_at FROM messages WHERE case_id=? ORDER BY created_at, rowid",
                    (case_id,),
                )
            ]
            verdicts = []
            for item in db.execute(
                "SELECT * FROM verdicts WHERE case_id=? ORDER BY created_at, rowid",
                (case_id,),
            ):
                verdict = dict(item)
                verdict["evidence"] = json.loads(verdict["evidence"] or "[]")
                verdict["details"] = json.loads(verdict["details"] or "{}")
                verdicts.append(verdict)
            decisions = []
            for item in db.execute(
                "SELECT * FROM decisions WHERE case_id=? ORDER BY created_at, rowid",
                (case_id,),
            ):
                decision = dict(item)
                decision["reason_codes"] = json.loads(decision["reason_codes"])
                decision["rule_ids"] = json.loads(decision["rule_ids"])
                decision["derived_facts"] = json.loads(decision["derived_facts"])
                decision["missing_fields"] = json.loads(decision["missing_fields"])
                decisions.append(decision)
            verifications = []
            for item in db.execute(
                "SELECT * FROM verifications WHERE case_id=? ORDER BY created_at, rowid",
                (case_id,),
            ):
                verification = dict(item)
                verification["rule_ids"] = json.loads(verification["rule_ids"])
                verification["evidence"] = json.loads(verification["evidence"])
                verification["checks"] = json.loads(verification["checks"])
                verifications.append(verification)
            return {
                "id": row["id"],
                "created_at": row["created_at"],
                "revision": row["revision"],
                "confirmed_state": json.loads(row["confirmed_state"]),
                "messages": messages,
                "verdicts": verdicts,
                "decisions": decisions,
                "verifications": verifications,
            }

    def events(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, run_id, type, payload, created_at FROM events WHERE case_id=? ORDER BY id",
                (case_id,),
            )
            return [{**dict(row), **json.loads(row["payload"])} for row in rows]
