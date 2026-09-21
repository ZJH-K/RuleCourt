"""Versioned RuleStore and the maintenance/public seams for T02.

Rule packages enter as drafts. Only a package with a recorded human review in
the ``verified`` state can be enabled for the public RuleStore. Coverage
obligations are stored separately and are returned only by the maintenance
review view.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .store import _now


class RulePackageValidationError(ValueError):
    """A package cannot be registered as a coherent versioned source."""


class DuplicateRulePackageError(RulePackageValidationError):
    """The same source revision and checksum has already been registered."""


class RuleScopeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    factions: set[str] = Field(default_factory=set)
    actions: set[str] = Field(default_factory=set)
    phases: set[str] = Field(default_factory=set)
    tags: set[str] = Field(default_factory=set)


class ScopeStrategyInput(BaseModel):
    """The explicit, versioned boundary for what this package claims to cover."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    included_factions: set[str] = Field(default_factory=set)
    included_actions: set[str] = Field(default_factory=set)
    notes: list[str] = Field(min_length=1)


class RuleNodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    section: str = Field(min_length=1, max_length=120)
    parent_section: str | None = Field(default=None, max_length=120)
    title: str | None = Field(default=None, max_length=300)
    text: str = Field(min_length=1, max_length=10000)
    scope: RuleScopeInput = Field(default_factory=RuleScopeInput)
    keywords: set[str] = Field(default_factory=set)


class RuleRelationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_rule_id: str = Field(min_length=1, max_length=120)
    target_rule_id: str = Field(min_length=1, max_length=120)
    relation: Literal["overrides", "clarifies", "exception_to", "depends_on"]


class CoverageObligationInput(BaseModel):
    """The only fields permitted in the private verification-coverage table."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=120)
    rule_ids: list[str] = Field(min_length=1)
    applies_when: list[str] = Field(min_length=1)
    acceptable_evidence: list[str] = Field(min_length=1)
    satisfied_when: list[str] = Field(min_length=1)


class RulePackageInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    game_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=300)
    source_type: Literal["law", "faq", "errata", "card", "user_upload"]
    revision: str = Field(min_length=1, max_length=120)
    published_at: date | None = None
    authority: Literal["official", "user", "community"]
    locator: str = Field(min_length=1, max_length=2000)
    scope_strategy: ScopeStrategyInput
    source_content: str = Field(min_length=1, max_length=2_000_000)
    checksum: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    rules: list[RuleNodeInput] = Field(min_length=1)
    relations: list[RuleRelationInput] = Field(default_factory=list)
    coverage_obligations: list[CoverageObligationInput] = Field(default_factory=list)


class RuleReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["verified", "disputed"]
    reviewer_id: str = Field(min_length=1, max_length=200)
    basis: str = Field(min_length=1, max_length=10000)
    evidence: list[str] = Field(min_length=1)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scope_dict(scope: RuleScopeInput) -> dict[str, list[str]]:
    return {
        "factions": sorted(scope.factions),
        "actions": sorted(scope.actions),
        "phases": sorted(scope.phases),
        "tags": sorted(scope.tags),
    }


class RuleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS rule_packages (
                    id TEXT PRIMARY KEY,
                    game_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    revision TEXT NOT NULL,
                    published_at TEXT,
                    authority TEXT NOT NULL,
                    locator TEXT NOT NULL,
                    scope_strategy TEXT NOT NULL,
                    source_content TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('draft', 'verified', 'disputed')),
                    enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN (0, 1)),
                    reviewer_id TEXT,
                    review_basis TEXT,
                    reviewed_at TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(game_id, revision, checksum)
                );
                CREATE TABLE IF NOT EXISTS rule_nodes (
                    id TEXT NOT NULL,
                    package_id TEXT NOT NULL REFERENCES rule_packages(id) ON DELETE CASCADE,
                    section TEXT NOT NULL,
                    parent_section TEXT,
                    title TEXT,
                    text TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    PRIMARY KEY(package_id, id)
                );
                CREATE TABLE IF NOT EXISTS rule_relations (
                    package_id TEXT NOT NULL REFERENCES rule_packages(id) ON DELETE CASCADE,
                    source_rule_id TEXT NOT NULL,
                    target_rule_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    PRIMARY KEY(package_id, source_rule_id, target_rule_id, relation),
                    FOREIGN KEY(package_id, source_rule_id) REFERENCES rule_nodes(package_id, id) ON DELETE CASCADE,
                    FOREIGN KEY(package_id, target_rule_id) REFERENCES rule_nodes(package_id, id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS coverage_obligations (
                    id TEXT NOT NULL,
                    package_id TEXT NOT NULL REFERENCES rule_packages(id) ON DELETE CASCADE,
                    rule_ids TEXT NOT NULL,
                    applies_when TEXT NOT NULL,
                    acceptable_evidence TEXT NOT NULL,
                    satisfied_when TEXT NOT NULL,
                    PRIMARY KEY(package_id, id)
                );
                CREATE TABLE IF NOT EXISTS rule_reviews (
                    id TEXT PRIMARY KEY,
                    package_id TEXT NOT NULL REFERENCES rule_packages(id) ON DELETE CASCADE,
                    status TEXT NOT NULL CHECK(status IN ('verified', 'disputed')),
                    reviewer_id TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )

            package_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(rule_packages)")
            }
            if "scope_strategy" not in package_columns:
                db.execute(
                    "ALTER TABLE rule_packages ADD COLUMN scope_strategy TEXT NOT NULL DEFAULT '{}'"
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
    def _package_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "game_id": row["game_id"],
            "title": row["title"],
            "source_type": row["source_type"],
            "revision": row["revision"],
            "published_at": row["published_at"],
            "authority": row["authority"],
            "locator": row["locator"],
            "scope_strategy": json.loads(row["scope_strategy"]),
            "checksum": row["checksum"],
            "status": row["status"],
            "enabled": bool(row["enabled"]),
            "reviewer_id": row["reviewer_id"],
            "review_basis": row["review_basis"],
            "reviewed_at": row["reviewed_at"],
            "created_at": row["created_at"],
        }

    def import_package(self, package: RulePackageInput) -> dict[str, Any]:
        expected_checksum = hashlib.sha256(package.source_content.encode("utf-8")).hexdigest()
        if package.checksum.lower() != expected_checksum:
            raise RulePackageValidationError("checksum does not match source_content")

        rule_ids = [rule.id for rule in package.rules]
        if len(rule_ids) != len(set(rule_ids)):
            raise RulePackageValidationError("rule IDs must be unique within a package")
        known_rule_ids = set(rule_ids)
        for relation in package.relations:
            if (
                relation.source_rule_id not in known_rule_ids
                or relation.target_rule_id not in known_rule_ids
            ):
                raise RulePackageValidationError("relations must reference rules in this package")
            if relation.source_rule_id == relation.target_rule_id:
                raise RulePackageValidationError("a rule relation cannot target itself")
        obligation_ids = [item.id for item in package.coverage_obligations]
        if len(obligation_ids) != len(set(obligation_ids)):
            raise RulePackageValidationError("coverage obligation IDs must be unique")
        for obligation in package.coverage_obligations:
            if not set(obligation.rule_ids).issubset(known_rule_ids):
                raise RulePackageValidationError(
                    "coverage obligations must reference package rules"
                )

        package_id = str(uuid4())
        try:
            with self._connect() as db:
                duplicate = db.execute(
                    "SELECT 1 FROM rule_packages WHERE game_id=? AND revision=? AND checksum=?",
                    (package.game_id, package.revision, expected_checksum),
                ).fetchone()
                if duplicate is not None:
                    raise DuplicateRulePackageError(
                        "this game revision and checksum are already registered"
                    )
                db.execute(
                    """INSERT INTO rule_packages
                    (id, game_id, title, source_type, revision, published_at, authority,
                     locator, scope_strategy, source_content, checksum, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
                    (
                        package_id,
                        package.game_id,
                        package.title,
                        package.source_type,
                        package.revision,
                        package.published_at.isoformat() if package.published_at else None,
                        package.authority,
                        package.locator,
                        _json(
                            {
                                "name": package.scope_strategy.name,
                                "included_factions": sorted(
                                    package.scope_strategy.included_factions
                                ),
                                "included_actions": sorted(package.scope_strategy.included_actions),
                                "notes": package.scope_strategy.notes,
                            }
                        ),
                        package.source_content,
                        expected_checksum,
                        _now(),
                    ),
                )
                for rule in package.rules:
                    db.execute(
                        """INSERT INTO rule_nodes
                        (id, package_id, section, parent_section, title, text, scope, keywords)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            rule.id,
                            package_id,
                            rule.section,
                            rule.parent_section,
                            rule.title,
                            rule.text,
                            _json(_scope_dict(rule.scope)),
                            _json(sorted(rule.keywords)),
                        ),
                    )
                for relation in package.relations:
                    db.execute(
                        """INSERT INTO rule_relations
                        (package_id, source_rule_id, target_rule_id, relation)
                        VALUES (?, ?, ?, ?)""",
                        (
                            package_id,
                            relation.source_rule_id,
                            relation.target_rule_id,
                            relation.relation,
                        ),
                    )
                for obligation in package.coverage_obligations:
                    db.execute(
                        """INSERT INTO coverage_obligations
                        (id, package_id, rule_ids, applies_when, acceptable_evidence, satisfied_when)
                        VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            obligation.id,
                            package_id,
                            _json(obligation.rule_ids),
                            _json(obligation.applies_when),
                            _json(obligation.acceptable_evidence),
                            _json(obligation.satisfied_when),
                        ),
                    )
        except sqlite3.IntegrityError as exc:
            raise RulePackageValidationError(str(exc)) from exc
        result = self.get_package(package_id)
        assert result is not None
        return result

    def list_packages(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM rule_packages ORDER BY created_at, id").fetchall()
            return [self._package_summary(row) for row in rows]

    def _get_package_row(self, package_id: str) -> sqlite3.Row | None:
        with self._connect() as db:
            return db.execute("SELECT * FROM rule_packages WHERE id=?", (package_id,)).fetchone()

    def get_package(self, package_id: str) -> dict[str, Any] | None:
        row = self._get_package_row(package_id)
        if row is None:
            return None
        with self._connect() as db:
            rules = []
            for item in db.execute(
                "SELECT * FROM rule_nodes WHERE package_id=? ORDER BY section, id", (package_id,)
            ):
                rules.append(
                    {
                        "id": item["id"],
                        "section": item["section"],
                        "parent_section": item["parent_section"],
                        "title": item["title"],
                        "text": item["text"],
                        "scope": json.loads(item["scope"]),
                        "keywords": json.loads(item["keywords"]),
                    }
                )
            relations = [
                dict(item)
                for item in db.execute(
                    """SELECT source_rule_id, target_rule_id, relation
                    FROM rule_relations WHERE package_id=?
                    ORDER BY source_rule_id, target_rule_id, relation""",
                    (package_id,),
                )
            ]
            coverage = []
            for item in db.execute(
                "SELECT * FROM coverage_obligations WHERE package_id=? ORDER BY id", (package_id,)
            ):
                coverage.append(
                    {
                        "id": item["id"],
                        "rule_ids": json.loads(item["rule_ids"]),
                        "applies_when": json.loads(item["applies_when"]),
                        "acceptable_evidence": json.loads(item["acceptable_evidence"]),
                        "satisfied_when": json.loads(item["satisfied_when"]),
                    }
                )
            reviews = []
            for item in db.execute(
                "SELECT * FROM rule_reviews WHERE package_id=? ORDER BY created_at, id",
                (package_id,),
            ):
                reviews.append(
                    {
                        "id": item["id"],
                        "status": item["status"],
                        "reviewer_id": item["reviewer_id"],
                        "basis": item["basis"],
                        "evidence": json.loads(item["evidence"]),
                        "created_at": item["created_at"],
                    }
                )
        return {
            **self._package_summary(row),
            "source_content": row["source_content"],
            "rules": rules,
            "relations": relations,
            "coverage_obligations": coverage,
            "reviews": reviews,
        }

    def review(self, package_id: str, review: RuleReviewInput) -> dict[str, Any]:
        row = self._get_package_row(package_id)
        if row is None:
            raise KeyError(package_id)
        review_id = str(uuid4())
        reviewed_at = _now()
        with self._connect() as db:
            db.execute(
                """INSERT INTO rule_reviews
                (id, package_id, status, reviewer_id, basis, evidence, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    review_id,
                    package_id,
                    review.status,
                    review.reviewer_id,
                    review.basis,
                    _json(review.evidence),
                    reviewed_at,
                ),
            )
            db.execute(
                """UPDATE rule_packages SET status=?, enabled=0, reviewer_id=?,
                review_basis=?, reviewed_at=? WHERE id=?""",
                (review.status, review.reviewer_id, review.basis, reviewed_at, package_id),
            )
        result = self.get_package(package_id)
        assert result is not None
        return result

    def enable(self, package_id: str) -> dict[str, Any]:
        row = self._get_package_row(package_id)
        if row is None:
            raise KeyError(package_id)
        if row["status"] != "verified" or row["reviewed_at"] is None:
            raise RulePackageValidationError("only a reviewed verified package can be enabled")
        with self._connect() as db:
            db.execute("UPDATE rule_packages SET enabled=1 WHERE id=?", (package_id,))
        result = self.get_package(package_id)
        assert result is not None
        return result

    @staticmethod
    def _public_source(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["package_id"],
            "game_id": row["game_id"],
            "title": row["package_title"],
            "source_type": row["source_type"],
            "revision": row["revision"],
            "published_at": row["published_at"],
            "authority": row["authority"],
            "locator": row["locator"],
            "scope_strategy": json.loads(row["scope_strategy"]),
            "checksum": row["checksum"],
        }

    @staticmethod
    def _public_rule(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "section": row["section"],
            "parent_section": row["parent_section"],
            "title": row["title"],
            "text": row["text"],
            "scope": json.loads(row["scope"]),
            "keywords": json.loads(row["keywords"]),
            "source": RuleStore._public_source(row),
        }

    def search_public(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        query = query.strip()
        pattern = f"%{query}%"
        with self._connect() as db:
            rows = db.execute(
                """SELECT n.*, p.id AS package_id, p.game_id, p.title AS package_title,
                p.source_type, p.revision, p.published_at, p.authority, p.locator,
                p.scope_strategy, p.checksum
                FROM rule_nodes n JOIN rule_packages p ON p.id=n.package_id
                WHERE p.status='verified' AND p.enabled=1
                AND (?='' OR n.id LIKE ? OR n.section LIKE ? OR n.title LIKE ?
                     OR n.text LIKE ? OR n.keywords LIKE ?)
                ORDER BY n.section, n.id LIMIT ?""",
                (query, pattern, pattern, pattern, pattern, pattern, limit),
            ).fetchall()
            return [self._public_rule(row) for row in rows]

    def get_public_rule(self, rule_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """SELECT n.*, p.id AS package_id, p.game_id, p.title AS package_title,
                p.source_type, p.revision, p.published_at, p.authority, p.locator,
                p.scope_strategy, p.checksum
                FROM rule_nodes n JOIN rule_packages p ON p.id=n.package_id
                WHERE n.id=? AND p.status='verified' AND p.enabled=1""",
                (rule_id,),
            ).fetchone()
            if row is None:
                return None
            result = self._public_rule(row)
            result["relations"] = [
                dict(item)
                for item in db.execute(
                    """SELECT source_rule_id, target_rule_id, relation
                    FROM rule_relations WHERE package_id=?
                    AND (source_rule_id=? OR target_rule_id=?)
                    ORDER BY source_rule_id, target_rule_id, relation""",
                    (row["package_id"], rule_id, rule_id),
                )
            ]
            return result
