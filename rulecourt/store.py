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
                CREATE TABLE IF NOT EXISTS investigations (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    strategy TEXT NOT NULL, status TEXT NOT NULL,
                    budget TEXT NOT NULL, usage TEXT NOT NULL,
                    last_reason TEXT, last_run_id TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS investigation_runs (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    investigation_id TEXT NOT NULL REFERENCES investigations(id),
                    strategy TEXT NOT NULL, model TEXT NOT NULL, provider TEXT NOT NULL,
                    resumed INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL, reason TEXT NOT NULL,
                    failure_reason TEXT, stop_reason TEXT NOT NULL,
                    iterations INTEGER NOT NULL DEFAULT 0,
                    tool_calls INTEGER NOT NULL DEFAULT 0,
                    tool_failures INTEGER NOT NULL DEFAULT 0,
                    provider_calls INTEGER NOT NULL DEFAULT 0,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    started_at TEXT NOT NULL, finished_at TEXT NOT NULL
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
        verdict = self._add_verdict(
            case_id,
            run_id,
            status,
            reason,
            expected_revision=None,
            evidence=evidence,
            details=details,
        )
        assert verdict is not None
        return verdict

    def add_verdict_if_current(
        self,
        case_id: str,
        run_id: str,
        status: str,
        reason: str,
        *,
        expected_revision: int,
        evidence: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        return self._add_verdict(
            case_id,
            run_id,
            status,
            reason,
            expected_revision=expected_revision,
            evidence=evidence,
            details=details,
        )

    def _add_verdict(
        self,
        case_id: str,
        run_id: str,
        status: str,
        reason: str,
        *,
        expected_revision: int | None,
        evidence: list[str] | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        verdict_id = str(uuid4())
        evidence = evidence or []
        details = details or {}
        with self._connect() as db:
            if expected_revision is not None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()
            revision = row["revision"]
            if expected_revision is not None and revision != expected_revision:
                return None
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

    def record_adjudication(
        self,
        case_id: str,
        run_id: str,
        result: dict[str, Any],
        *,
        expected_revision: int,
        details: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Persist a workflow result only if its state snapshot is still current."""
        decision_payload = result["decision"]
        verification_payload = result["verification"]
        evidence = list(result.get("evidence", []))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None or row["revision"] != expected_revision:
                return None

            decision_id = str(uuid4())
            verification_id = verification_payload.get("id") or str(uuid4())
            verdict_id = str(uuid4())
            recorded_details = {
                **details,
                "decision_ref": decision_id,
                "verification_refs": [verification_id],
            }
            created_at = _now()
            db.execute(
                """INSERT INTO decisions
                (id, case_id, run_id, revision, ruleset_id, status, reason_codes,
                 rule_ids, derived_facts, missing_fields, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    case_id,
                    run_id,
                    expected_revision,
                    result["ruleset_id"],
                    decision_payload["status"],
                    json.dumps(decision_payload.get("reason_codes", []), ensure_ascii=False),
                    json.dumps(decision_payload.get("rule_ids", []), ensure_ascii=False),
                    json.dumps(decision_payload.get("derived_facts", {}), ensure_ascii=False),
                    json.dumps(decision_payload.get("missing_fields", []), ensure_ascii=False),
                    created_at,
                ),
            )
            db.execute(
                """INSERT INTO verifications
                (id, case_id, run_id, revision, decision_id, ruleset_id,
                 ruleset_version_ref, status, rule_ids, evidence, checks, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verification_id,
                    case_id,
                    run_id,
                    expected_revision,
                    decision_id,
                    verification_payload["ruleset_id"],
                    verification_payload.get("ruleset_version_ref"),
                    verification_payload["status"],
                    json.dumps(verification_payload.get("rule_ids", []), ensure_ascii=False),
                    json.dumps(verification_payload.get("evidence", []), ensure_ascii=False),
                    json.dumps(verification_payload.get("checks", {}), ensure_ascii=False),
                    created_at,
                ),
            )
            db.execute(
                """INSERT INTO verdicts
                (id, case_id, run_id, revision, status, reason, created_at, evidence, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    verdict_id,
                    case_id,
                    run_id,
                    expected_revision,
                    result["status"],
                    result["reason"],
                    created_at,
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(recorded_details, ensure_ascii=False),
                ),
            )
            db.execute(
                "INSERT INTO events(case_id, run_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    case_id,
                    run_id,
                    "verdict",
                    json.dumps({"verdict_id": verdict_id, "status": result["status"]}),
                    created_at,
                ),
            )
        decision = {
            **decision_payload,
            "id": decision_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": expected_revision,
        }
        verification = {
            **verification_payload,
            "id": verification_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": expected_revision,
            "decision_id": decision_id,
        }
        verdict = {
            "id": verdict_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": expected_revision,
            "status": result["status"],
            "reason": result["reason"],
            "evidence": evidence,
            "details": recorded_details,
        }
        return {"decision": decision, "verification": verification, "verdict": verdict}

    @staticmethod
    def _decode_investigation(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["budget"] = json.loads(item.pop("budget") or "{}")
        item["usage"] = json.loads(item.pop("usage") or "{}")
        return item

    @staticmethod
    def _decode_investigation_run(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["resumed"] = bool(item["resumed"])
        item["metadata"] = json.loads(item.pop("metadata") or "{}")
        item["usage"] = {
            "iterations": item["iterations"],
            "tool_calls": item["tool_calls"],
            "tool_failures": item["tool_failures"],
            "provider_calls": item["provider_calls"],
            "input_tokens": item["input_tokens"],
            "output_tokens": item["output_tokens"],
            "total_tokens": item["total_tokens"],
            "latency_ms": item["latency_ms"],
        }
        return item

    def create_investigation(
        self, case_id: str, strategy: str, budget: dict[str, Any]
    ) -> dict[str, Any]:
        investigation_id = str(uuid4())
        timestamp = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO investigations
                (id, case_id, strategy, status, budget, usage, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    investigation_id,
                    case_id,
                    strategy,
                    "active",
                    json.dumps(budget, ensure_ascii=False),
                    json.dumps({}, ensure_ascii=False),
                    timestamp,
                    timestamp,
                ),
            )
        result = self.get_investigation(investigation_id)
        assert result is not None
        return result

    def get_investigation(self, investigation_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM investigations WHERE id=?", (investigation_id,)
            ).fetchone()
            return None if row is None else self._decode_investigation(row)

    def latest_investigation(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT * FROM investigations
                WHERE case_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (case_id,),
            ).fetchone()
            return None if row is None else self._decode_investigation(row)

    def investigations(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM investigations
                WHERE case_id=? ORDER BY created_at, rowid""",
                (case_id,),
            )
            return [self._decode_investigation(row) for row in rows]

    def update_investigation(
        self,
        investigation_id: str,
        *,
        status: str,
        usage: dict[str, Any],
        last_reason: str,
        last_run_id: str,
    ) -> dict[str, Any]:
        with self._connect() as db:
            db.execute(
                """UPDATE investigations
                SET status=?, usage=?, last_reason=?, last_run_id=?, updated_at=?
                WHERE id=?""",
                (
                    status,
                    json.dumps(usage, ensure_ascii=False),
                    last_reason,
                    last_run_id,
                    _now(),
                    investigation_id,
                ),
            )
        result = self.get_investigation(investigation_id)
        assert result is not None
        return result

    def add_investigation_run(
        self,
        case_id: str,
        investigation_id: str,
        *,
        record: dict[str, Any],
    ) -> dict[str, Any]:
        run_id = record["run_id"]
        usage = record.get("usage") or {}
        with self._connect() as db:
            db.execute(
                """INSERT INTO investigation_runs
                (id, case_id, investigation_id, strategy, model, provider, resumed,
                 status, reason, failure_reason, stop_reason, iterations, tool_calls,
                 tool_failures, provider_calls, input_tokens, output_tokens, total_tokens,
                 latency_ms, metadata, started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id,
                    case_id,
                    investigation_id,
                    record["strategy"],
                    record["model"],
                    record["provider"],
                    int(bool(record.get("resumed"))),
                    record["status"],
                    record["reason"],
                    record.get("failure_reason"),
                    record["stop_reason"],
                    int(usage.get("iterations", 0)),
                    int(usage.get("tool_calls", 0)),
                    int(usage.get("tool_failures", 0)),
                    int(usage.get("provider_calls", 0)),
                    int(usage.get("input_tokens", 0)),
                    int(usage.get("output_tokens", 0)),
                    int(usage.get("total_tokens", 0)),
                    int(usage.get("latency_ms", 0)),
                    json.dumps(record.get("metadata") or {}, ensure_ascii=False),
                    record["started_at"],
                    record["finished_at"],
                ),
            )
            row = db.execute("SELECT * FROM investigation_runs WHERE id=?", (run_id,)).fetchone()
        assert row is not None
        return self._decode_investigation_run(row)

    def investigation_runs(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT * FROM investigation_runs
                WHERE case_id=? ORDER BY started_at, rowid""",
                (case_id,),
            )
            return [self._decode_investigation_run(row) for row in rows]

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
            investigations = self.investigations(case_id)
            investigation_runs = self.investigation_runs(case_id)
            current_revision = row["revision"]
            current_verdict = next(
                (item for item in reversed(verdicts) if item["revision"] == current_revision),
                None,
            )
            current_details = current_verdict["details"] if current_verdict is not None else {}
            current_decision_id = current_details.get("decision_ref")
            current_verification_ids = set(current_details.get("verification_refs", []))
            for verdict in verdicts:
                verdict["valid_for_current_state"] = (
                    current_verdict is not None and verdict["id"] == current_verdict["id"]
                )
            for decision in decisions:
                decision["valid_for_current_state"] = (
                    current_verdict is not None
                    and decision["revision"] == current_revision
                    and (current_decision_id is None or decision["id"] == current_decision_id)
                )
            for verification in verifications:
                verification["valid_for_current_state"] = (
                    current_verdict is not None
                    and verification["revision"] == current_revision
                    and verification["id"] in current_verification_ids
                )
            current_decision = next(
                (item for item in decisions if item["valid_for_current_state"]),
                None,
            )
            current_verification = next(
                (item for item in verifications if item["valid_for_current_state"]),
                None,
            )

            return {
                "id": row["id"],
                "created_at": row["created_at"],
                "revision": row["revision"],
                "current_verdict": current_verdict,
                "current_decision": current_decision,
                "current_verification": current_verification,
                "confirmed_state": json.loads(row["confirmed_state"]),
                "messages": messages,
                "verdicts": verdicts,
                "decisions": decisions,
                "investigations": investigations,
                "investigation_runs": investigation_runs,
                "latest_investigation": investigations[-1] if investigations else None,
                "verifications": verifications,
            }

    def events(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, run_id, type, payload, created_at FROM events WHERE case_id=? ORDER BY id",
                (case_id,),
            )
            return [{**dict(row), **json.loads(row["payload"])} for row in rows]
