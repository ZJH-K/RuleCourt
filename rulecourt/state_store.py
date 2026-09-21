"""Persistent acceptance boundary for proposed Case state.

This store shares the Case database with :mod:`rulecourt.store`, but keeps
state evidence and declaration history separate from the older T01 tables.
The separation makes it possible for the natural-language extractor to remain
untrusted while the store owns revisions, lifecycle transitions and the
confirmed snapshot.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from .state import (
    ProposedStatePatch,
    validate_completeness_assertion,
    validate_fact_change,
    validate_scope_assumption,
)
from .store import _now


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique(values: list[Any]) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def _set_path(target: dict[str, Any], path: str, value: Any) -> None:
    current = target
    parts = path.split(".")
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _get_path(target: dict[str, Any], path: str) -> Any:
    current: Any = target
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _fact_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "field_path": row["field_path"],
        "value": json.loads(row["value"]),
        "operation": row["operation"],
        "status": row["status"],
        "created_revision": row["created_revision"],
        "evidence_refs": json.loads(row["evidence_refs"]),
        "supersedes_id": row["supersedes_id"],
        "created_at": row["created_at"],
    }


def _scope_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "scope_id": row["scope_id"],
        "predicate_id": row["predicate_id"],
        "asserted_value": None if row["asserted_value"] is None else bool(row["asserted_value"]),
        "ruleset_version": row["ruleset_version"],
        "scope_policy_version": row["scope_policy_version"],
        "depends_on_fact_refs": json.loads(row["depends_on_fact_refs"]),
        "evidence_refs": json.loads(row["evidence_refs"]),
        "created_revision": row["created_revision"],
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "created_at": row["created_at"],
    }


def _completeness_output(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "case_id": row["case_id"],
        "collection_target": row["collection_target"],
        "covered_scope": json.loads(row["covered_scope"]),
        "completeness": row["completeness"],
        "member_snapshot_ref": row["member_snapshot_ref"],
        "member_snapshot": json.loads(row["member_snapshot"]),
        "evidence_refs": json.loads(row["evidence_refs"]),
        "created_revision": row["created_revision"],
        "status": row["status"],
        "supersedes_id": row["supersedes_id"],
        "created_at": row["created_at"],
    }


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS state_observations (
                    case_id TEXT PRIMARY KEY REFERENCES cases(id) ON DELETE CASCADE,
                    unknown_fields TEXT NOT NULL DEFAULT '[]',
                    issues TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS state_facts (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    field_path TEXT NOT NULL,
                    value TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'superseded', 'retracted', 'conflicted')),
                    created_revision INTEGER NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS scope_assumptions (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    scope_id TEXT NOT NULL,
                    predicate_id TEXT NOT NULL,
                    asserted_value INTEGER,
                    ruleset_version TEXT NOT NULL,
                    scope_policy_version TEXT NOT NULL,
                    depends_on_fact_refs TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    created_revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'invalidated', 'retracted', 'superseded')),
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS completeness_assertions (
                    id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    collection_target TEXT NOT NULL,
                    covered_scope TEXT NOT NULL,
                    completeness TEXT NOT NULL CHECK(completeness IN ('partial', 'complete')),
                    member_snapshot_ref TEXT NOT NULL,
                    member_snapshot TEXT NOT NULL,
                    evidence_refs TEXT NOT NULL,
                    created_revision INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('proposed', 'confirmed', 'invalidated', 'retracted', 'superseded')),
                    supersedes_id TEXT,
                    created_at TEXT NOT NULL
                );
                """
            )
            db.execute(
                "INSERT OR IGNORE INTO state_observations(case_id, updated_at) "
                "SELECT id, ? FROM cases",
                (_now(),),
            )

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

    @staticmethod
    def _case_exists(db: sqlite3.Connection, case_id: str) -> bool:
        return db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None

    @staticmethod
    def _evidence_issues(
        db: sqlite3.Connection, case_id: str, patch: ProposedStatePatch
    ) -> list[dict[str, str]]:
        evidence = []
        for change in patch.changes:
            evidence.extend(change.evidence)
        for assumption in patch.scope_assumptions:
            evidence.extend(assumption.evidence_refs)
        for assertion in patch.completeness_assertions:
            evidence.extend(assertion.evidence_refs)
        issues: list[dict[str, str]] = []
        for item in evidence:
            message = db.execute(
                "SELECT 1 FROM messages WHERE id=? AND case_id=?",
                (item.source_message_id, case_id),
            ).fetchone()
            if message is None:
                issues.append(
                    {
                        "code": "EVIDENCE_NOT_IN_CASE",
                        "message": "Every source reference must point to a message in this Case.",
                        "field_path": item.field_path,
                    }
                )
        return issues

    @staticmethod
    def _active_fact(db: sqlite3.Connection, case_id: str, field_path: str) -> sqlite3.Row | None:
        return db.execute(
            """SELECT * FROM state_facts
            WHERE case_id=? AND field_path=? AND status='active'
            ORDER BY created_revision DESC, created_at DESC LIMIT 1""",
            (case_id, field_path),
        ).fetchone()

    @staticmethod
    def _merge_evidence(old: str, additions: list[Any]) -> str:
        current = json.loads(old)
        return _json(_unique(current + [item.model_dump() for item in additions]))

    @staticmethod
    def _fact_value(row: sqlite3.Row | None) -> Any:
        return None if row is None else json.loads(row["value"])

    def _apply_fact(
        self,
        db: sqlite3.Connection,
        case_id: str,
        change: Any,
        revision: int,
    ) -> tuple[bool, bool, list[dict[str, str]]]:
        old = self._active_fact(db, case_id, change.field_path)
        old_value = self._fact_value(old)
        if change.operation == "assert" and old is not None and old_value == change.value:
            db.execute(
                "UPDATE state_facts SET evidence_refs=? WHERE id=?",
                (self._merge_evidence(old["evidence_refs"], change.evidence), old["id"]),
            )
            return False, False, []

        if (
            change.field_path.endswith(".adjacent_to")
            and change.operation == "assert"
            and old is not None
        ):
            merged = _unique(list(old_value or []) + list(change.value or []))
            if merged == list(old_value or []):
                db.execute(
                    "UPDATE state_facts SET evidence_refs=? WHERE id=?",
                    (self._merge_evidence(old["evidence_refs"], change.evidence), old["id"]),
                )
                return False, False, []
            value = merged
        else:
            value = change.value

        if change.operation == "retract":
            if old is None:
                return False, False, []
            db.execute("UPDATE state_facts SET status='retracted' WHERE id=?", (old["id"],))
            self._invalidate_completeness_for_fact(db, case_id, change.field_path)
            return True, False, []

        if old is not None:
            if change.operation == "assert":
                db.execute("UPDATE state_facts SET status='conflicted' WHERE id=?", (old["id"],))
                status = "conflicted"
                conflict = {
                    "code": "FACT_CONFLICT",
                    "message": "A new assertion conflicts with the accepted fact; submit an explicit correction.",
                    "field_path": change.field_path,
                }
            else:
                db.execute("UPDATE state_facts SET status='superseded' WHERE id=?", (old["id"],))
                status = "active"
                conflict = None
        else:
            status = "active"
            conflict = None
        fact_id = str(uuid4())
        db.execute(
            """INSERT INTO state_facts
            (id, case_id, field_path, value, operation, status, created_revision,
             evidence_refs, supersedes_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fact_id,
                case_id,
                change.field_path,
                _json(value),
                change.operation,
                status,
                revision,
                _json([item.model_dump() for item in change.evidence]),
                old["id"] if old is not None else None,
                _now(),
            ),
        )
        return True, conflict is not None, [conflict] if conflict else []

    @staticmethod
    def _invalidate_completeness_for_fact(
        db: sqlite3.Connection, case_id: str, field_path: str
    ) -> None:
        parts = field_path.split(".")
        if len(parts) < 5 or parts[0] != "clearings" or parts[2] != "presence":
            return
        target = ".".join(parts[:3])
        db.execute(
            """UPDATE completeness_assertions SET status='invalidated'
            WHERE case_id=? AND collection_target=? AND status IN ('proposed', 'confirmed')""",
            (case_id, target),
        )

    @staticmethod
    def _build_state(db: sqlite3.Connection, case_id: str) -> dict[str, Any]:
        state: dict[str, Any] = {}
        rows = db.execute(
            """SELECT * FROM state_facts WHERE case_id=? AND status='active'
            ORDER BY created_revision, created_at, id""",
            (case_id,),
        )
        for row in rows:
            path = row["field_path"]
            value = json.loads(row["value"])
            if path.endswith(".adjacent_to"):
                existing = _get_path(state, path)
                value = _unique(list(existing or []) + list(value or []))
            _set_path(state, path, value)

        assertions = db.execute(
            """SELECT * FROM completeness_assertions
            WHERE case_id=? AND status='confirmed' AND completeness='complete'
            ORDER BY created_revision, created_at, id""",
            (case_id,),
        )
        for row in assertions:
            target = row["collection_target"]
            snapshot = json.loads(row["member_snapshot"])
            members = snapshot.get("members", [])
            scope = json.loads(row["covered_scope"])
            if target.endswith(".adjacent_to"):
                _set_path(
                    state, target, _unique([item for item in members if isinstance(item, str)])
                )
                continue
            if not target.endswith(".presence"):
                continue
            clearing = scope["clearing_id"]
            faction = scope.get("faction")
            piece = scope.get("piece_type")
            if faction is None and piece is None:
                presence = {}
                for member in members:
                    if not isinstance(member, dict):
                        continue
                    member_faction = member.get("faction")
                    member_piece = member.get("piece_type")
                    if member_faction in {"eyrie", "marquise"} and member_piece in {
                        "warriors",
                        "buildings",
                    }:
                        presence.setdefault(member_faction, {})[member_piece] = member.get(
                            "count", 0
                        )
                _set_path(state, f"clearings.{clearing}.presence", presence)
            elif piece is not None and faction is not None and not members:
                _set_path(state, f"clearings.{clearing}.presence.{faction}.{piece}", 0)
        return state

    def apply_patch(self, case_id: str, patch: ProposedStatePatch) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                return {
                    "accepted": False,
                    "state_revision": None,
                    "sufficient": False,
                    "missing_fields": [],
                    "issues": [{"code": "CASE_NOT_FOUND", "message": "Case not found."}],
                }
            current_revision = row["revision"]
            if patch.expected_state_revision != current_revision:
                return {
                    "accepted": False,
                    "state_revision": current_revision,
                    "sufficient": False,
                    "missing_fields": [],
                    "issues": [
                        {
                            "code": "STATE_REVISION_CONFLICT",
                            "message": "The proposal was based on an older Case revision.",
                        }
                    ],
                }

            issues: list[dict[str, str]] = []
            for change in patch.changes:
                problem = validate_fact_change(change)
                if problem:
                    issues.append(problem)
            for assumption in patch.scope_assumptions:
                problem = validate_scope_assumption(assumption)
                if problem:
                    issues.append(problem)
            for assertion in patch.completeness_assertions:
                problem = validate_completeness_assertion(assertion)
                if problem:
                    issues.append(problem)
            issues.extend(self._evidence_issues(db, case_id, patch))
            if issues:
                return {
                    "accepted": False,
                    "state_revision": current_revision,
                    "sufficient": False,
                    "missing_fields": patch.unknown_fields,
                    "issues": issues,
                }

            has_change = False
            has_conflict = False
            fact_issues: list[dict[str, str]] = []
            for change in patch.changes:
                changed, conflict, new_issues = self._apply_fact(
                    db, case_id, change, current_revision + 1
                )
                has_change = has_change or changed
                has_conflict = has_conflict or conflict
                fact_issues.extend(new_issues)

            for assumption in patch.scope_assumptions:
                assumption_id = str(uuid4())
                status = (
                    "retracted"
                    if assumption.operation == "retract"
                    else "proposed"
                    if assumption.asserted_value is None
                    else "confirmed"
                )
                if assumption.supersedes_id:
                    db.execute(
                        "UPDATE scope_assumptions SET status='superseded' WHERE id=? AND case_id=?",
                        (assumption.supersedes_id, case_id),
                    )
                db.execute(
                    """INSERT INTO scope_assumptions
                    (id, case_id, scope_id, predicate_id, asserted_value, ruleset_version,
                     scope_policy_version, depends_on_fact_refs, evidence_refs, created_revision,
                     status, supersedes_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        assumption_id,
                        case_id,
                        assumption.scope_id,
                        assumption.predicate_id,
                        None
                        if assumption.asserted_value is None
                        else int(assumption.asserted_value),
                        assumption.ruleset_version,
                        assumption.scope_policy_version,
                        _json(assumption.depends_on_fact_refs),
                        _json([item.model_dump() for item in assumption.evidence_refs]),
                        current_revision + 1,
                        status,
                        assumption.supersedes_id,
                        _now(),
                    ),
                )
                has_change = True

            for assertion in patch.completeness_assertions:
                assertion_id = str(uuid4())
                status = "retracted" if assertion.operation == "retract" else "confirmed"
                if assertion.supersedes_id:
                    db.execute(
                        "UPDATE completeness_assertions SET status='superseded' WHERE id=? AND case_id=?",
                        (assertion.supersedes_id, case_id),
                    )
                db.execute(
                    """INSERT INTO completeness_assertions
                    (id, case_id, collection_target, covered_scope, completeness,
                     member_snapshot_ref, member_snapshot, evidence_refs, created_revision,
                     status, supersedes_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        assertion_id,
                        case_id,
                        assertion.collection_target,
                        _json(assertion.covered_scope),
                        assertion.completeness,
                        assertion.member_snapshot_ref or f"snapshot:{uuid4()}",
                        _json(assertion.member_snapshot),
                        _json([item.model_dump() for item in assertion.evidence_refs]),
                        current_revision + 1,
                        status,
                        assertion.supersedes_id,
                        _now(),
                    ),
                )
                has_change = True

            new_revision = current_revision + 1 if has_change else current_revision
            if has_change:
                db.execute(
                    "UPDATE cases SET revision=?, confirmed_state=? WHERE id=?",
                    (
                        new_revision,
                        _json(self._build_state(db, case_id)),
                        case_id,
                    ),
                )
            else:
                new_revision = current_revision

            old_observation = db.execute(
                "SELECT unknown_fields FROM state_observations WHERE case_id=?", (case_id,)
            ).fetchone()
            unknown_fields = (
                [] if old_observation is None else json.loads(old_observation["unknown_fields"])
            )
            unknown_fields = _unique(unknown_fields + patch.unknown_fields)
            for change in patch.changes:
                unknown_fields = [item for item in unknown_fields if item != change.field_path]
            combined_issues = fact_issues
            db.execute(
                """INSERT INTO state_observations(case_id, unknown_fields, issues, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET unknown_fields=excluded.unknown_fields,
                issues=excluded.issues, updated_at=excluded.updated_at""",
                (case_id, _json(unknown_fields), _json(combined_issues), _now()),
            )
            return {
                "accepted": not has_conflict,
                "state_revision": new_revision,
                "sufficient": not unknown_fields,
                "missing_fields": unknown_fields,
                "issues": combined_issues,
            }

    def view(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            case = db.execute(
                "SELECT id, revision, confirmed_state FROM cases WHERE id=?", (case_id,)
            ).fetchone()
            if case is None:
                return None
            observation = db.execute(
                "SELECT unknown_fields, issues FROM state_observations WHERE case_id=?", (case_id,)
            ).fetchone()
            facts = [
                _fact_output(row)
                for row in db.execute(
                    "SELECT * FROM state_facts WHERE case_id=? ORDER BY created_revision, created_at, id",
                    (case_id,),
                )
            ]
            assumptions = [
                _scope_output(row)
                for row in db.execute(
                    "SELECT * FROM scope_assumptions WHERE case_id=? ORDER BY created_revision, created_at, id",
                    (case_id,),
                )
            ]
            assertions = [
                _completeness_output(row)
                for row in db.execute(
                    """SELECT * FROM completeness_assertions
                    WHERE case_id=? ORDER BY created_revision, created_at, id""",
                    (case_id,),
                )
            ]
            return {
                "confirmed_state": json.loads(case["confirmed_state"]),
                "state_revision": case["revision"],
                "unknown_fields": []
                if observation is None
                else json.loads(observation["unknown_fields"]),
                "state_issues": [] if observation is None else json.loads(observation["issues"]),
                "state_facts": facts,
                "scope_assumptions": assumptions,
                "completeness_assertions": assertions,
                "active_scope_assumptions": [
                    item for item in assumptions if item["status"] in {"proposed", "confirmed"}
                ],
                "active_completeness_assertions": [
                    item for item in assertions if item["status"] in {"proposed", "confirmed"}
                ],
            }
