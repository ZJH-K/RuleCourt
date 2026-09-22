"""Offline evaluation primitives for replaying labelled RuleCourt Cases.

The evaluation path is deliberately separate from the public adjudication path.
It owns the candidate-case labels and the fact responder, while a runner only
receives the public initial message and facts explicitly requested by the
investigation.
"""

from __future__ import annotations

import json
import random
import sys
from argparse import ArgumentParser
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class FactRequest(BaseModel):
    """A deterministic request for one or more explicitly named facts."""

    model_config = ConfigDict(extra="forbid")

    fields: list[str] = Field(default_factory=list)
    text: str | None = None

    @field_validator("fields")
    @classmethod
    def normalize_fields(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for field in value:
            item = field.strip()
            if item and item not in normalized:
                normalized.append(item)
        return normalized

    @model_validator(mode="after")
    def require_request_target(self) -> FactRequest:
        if not self.fields and not (self.text and self.text.strip()):
            raise ValueError("a fact request needs fields or text")
        return self


class FactAnswer(BaseModel):
    """The only facts the responder is allowed to reveal for one request."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["answered", "unknown", "ambiguous", "budget_exhausted"]
    requested_fields: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    unknown_fields: list[str] = Field(default_factory=list)
    text: str = ""
    sources: list[str] = Field(default_factory=list)
    completeness: dict[str, bool] = Field(default_factory=dict)
    completeness_scopes: dict[str, str | None] = Field(default_factory=dict)
    round: int = Field(ge=1)


class FactFixture(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: Any
    text: str = ""
    source: list[str] = Field(default_factory=list)
    complete: bool = False
    complete_scope: str | None = None

    @field_validator("source", mode="before")
    @classmethod
    def normalize_source(cls, value: str | list[str] | None) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return list(value)


class FactResponder:
    """Return pre-labelled facts without generating or inferring new facts.

    The responder is intentionally small at its seam: callers provide a
    ``FactRequest`` and receive a stable ``FactAnswer``.  Every request counts,
    including repeated and unknown requests, so a replay cannot reset the
    clarification budget by changing its run identifier.
    """

    def __init__(self, facts: Mapping[str, Any], *, max_requests: int | None = None):
        if max_requests is not None and (
            isinstance(max_requests, bool) or not isinstance(max_requests, int) or max_requests <= 0
        ):
            raise ValueError("max_requests must be a positive integer")
        self._facts = {field: self._coerce_spec(value) for field, value in facts.items()}
        self._max_requests = max_requests
        self._request_count = 0

    @staticmethod
    def _coerce_spec(value: Any) -> FactFixture:
        if isinstance(value, FactFixture):
            return value
        if isinstance(value, Mapping) and "value" in value:
            source = value.get("source", [])
            if isinstance(source, str):
                source = [source]
            return FactFixture(
                value=value["value"],
                text=str(value.get("text", "")),
                source=list(source),
                complete=bool(value.get("complete", False)),
            )
        return FactFixture(value=value)

    @property
    def request_count(self) -> int:
        return self._request_count

    def answer(self, request: FactRequest) -> FactAnswer:
        self._request_count += 1
        fields = list(request.fields)
        if not fields:
            text = request.text or ""
            fields = [field for field in self._facts if field in text]
            if not fields:
                return FactAnswer(
                    status="ambiguous",
                    round=self._request_count,
                )
        if self._max_requests is not None and self._request_count > self._max_requests:
            return FactAnswer(
                status="budget_exhausted",
                requested_fields=fields,
                round=self._request_count,
            )

        known: dict[str, Any] = {}
        unknown: list[str] = []
        texts: list[str] = []
        sources: list[str] = []
        completeness: dict[str, bool] = {}
        completeness_scopes: dict[str, str | None] = {}
        for field in fields:
            spec = self._facts.get(field)
            if spec is None:
                unknown.append(field)
                continue
            known[field] = spec.value
            completeness[field] = spec.complete
            completeness_scopes[field] = spec.complete_scope
            if spec.text:
                texts.append(spec.text)
            for source in spec.source:
                if source not in sources:
                    sources.append(source)

        status: Literal["answered", "unknown"] = "answered" if known else "unknown"
        return FactAnswer(
            status=status,
            requested_fields=fields,
            facts=known,
            unknown_fields=unknown,
            text="".join(texts),
            sources=sources,
            completeness=completeness,
            completeness_scopes=completeness_scopes,
            round=self._request_count,
        )


VerdictLabel = Literal[
    "LEGAL",
    "ILLEGAL",
    "INSUFFICIENT_INFORMATION",
    "UNRESOLVED",
]
ReviewStatus = Literal["draft", "verified", "disputed"]


REQUIRED_REVIEW_CHECKS = (
    "labels",
    "scope",
    "fact_availability",
    "evidence",
    "acceptable_questions",
    "independent_source",
)


class CaseRevision(BaseModel):
    """A retained correction or ambiguity record for a Case source."""

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    kind: str = "correction"
    recorded_at: str | None = None
    reason: str
    changes: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind", "reason")
    @classmethod
    def nonempty_revision_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Case revision text must not be empty")
        return value


class CaseReview(BaseModel):
    """Independent human review record for a candidate evaluation Case."""

    model_config = ConfigDict(extra="forbid")

    status: ReviewStatus = "draft"
    reviewer_id: str | None = None
    rule_version: str | None = None
    reviewed_at: str | None = None
    basis: str = ""
    evidence: list[str] = Field(default_factory=list)
    report: str = ""
    checks: dict[str, bool] = Field(default_factory=dict)
    dispute_reasons: list[str] = Field(default_factory=list)

    @field_validator("checks")
    @classmethod
    def normalize_checks(cls, value: dict[str, bool]) -> dict[str, bool]:
        checks: dict[str, bool] = {}
        for name, checked in value.items():
            normalized = str(name).strip()
            if not normalized:
                continue
            if not isinstance(checked, bool):
                raise TypeError(f"review check {normalized!r} must be boolean")
            checks[normalized] = checked
        return checks

    @field_validator("dispute_reasons")
    @classmethod
    def normalize_dispute_reasons(cls, value: list[str]) -> list[str]:
        reasons: list[str] = []
        for reason in value:
            normalized = reason.strip()
            if normalized and normalized not in reasons:
                reasons.append(normalized)
        return reasons

    @property
    def missing_checks(self) -> list[str]:
        return [name for name in REQUIRED_REVIEW_CHECKS if self.checks.get(name) is not True]

    @property
    def checklist_complete(self) -> bool:
        return not self.missing_checks


class EvaluationCase(BaseModel):
    """A labelled Case whose hidden fields are owned by the evaluator."""

    model_config = ConfigDict(extra="forbid")

    id: str
    family_id: str
    category: str
    scope: str
    ruleset_id: str = "root-law-2025-10"
    initial_input: str
    initial_label: VerdictLabel
    complete_label: VerdictLabel
    label_source: str = "candidate:unverified"
    initial_reason: str | None = None
    complete_reason: str | None = None
    initial_unknown_fields: list[str] = Field(default_factory=list)
    scope_assumptions: list[str] = Field(default_factory=list)
    completeness_assertions: list[dict[str, Any]] = Field(default_factory=list)
    clarification_facts: dict[str, FactFixture] = Field(default_factory=dict)
    fact_sources: dict[str, str] = Field(default_factory=dict)
    evidence: list[str] = Field(default_factory=list)
    acceptable_questions: list[str] = Field(default_factory=list)
    review: CaseReview = Field(default_factory=CaseReview)
    history: list[CaseRevision] = Field(default_factory=list)
    split: Literal["development", "holdout"] | None = None

    @field_validator(
        "id", "family_id", "category", "scope", "ruleset_id", "initial_input", "label_source"
    )
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("case identity and initial_input must not be empty")
        return value

    @field_validator("acceptable_questions")
    @classmethod
    def unique_question_targets(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for field in value:
            field = field.strip()
            if field and field not in result:
                result.append(field)
        return result

    @field_validator("history")
    @classmethod
    def validate_history(cls, value: list[CaseRevision]) -> list[CaseRevision]:
        revisions = [item.revision for item in value]
        if len(revisions) != len(set(revisions)) or revisions != sorted(revisions):
            raise ValueError("Case revision history must be ordered and unique")
        return value

    @model_validator(mode="after")
    def validate_sources(self) -> EvaluationCase:
        missing_sources = set(self.clarification_facts) - set(self.fact_sources)
        if missing_sources:
            raise ValueError(
                "every clarification fact needs an independent source: "
                + ", ".join(sorted(missing_sources))
            )
        if self.review.status == "verified":
            missing_review = [
                name
                for name, value in (
                    ("reviewer_id", self.review.reviewer_id),
                    ("basis", self.review.basis),
                    ("report", self.review.report),
                )
                if not value or not str(value).strip()
            ]
            if missing_review or not self.review.evidence:
                required = ", ".join(
                    [*missing_review, "evidence"] if not self.review.evidence else missing_review
                )
                raise ValueError(f"verified cases require review {required}")
            if self.label_source == "candidate:unverified":
                raise ValueError("verified cases require an independent label_source")
            if not self.evidence:
                raise ValueError("verified cases require evidence")
        elif self.review.status == "disputed" and (
            not self.review.basis.strip() or not self.review.report.strip()
        ):
            raise ValueError("disputed cases require review basis and report")
        return self

    def scoring_validation_errors(self) -> list[str]:
        """Return formal-scoring errors without changing the review status."""

        if self.review.status == "draft":
            return []
        errors: list[str] = []
        if not self.review.reviewer_id or not self.review.reviewer_id.strip():
            errors.append("reviewer_id")
        if not self.review.rule_version or not self.review.rule_version.strip():
            errors.append("rule_version")
        if not self.review.reviewed_at or not self.review.reviewed_at.strip():
            errors.append("reviewed_at")
        if self.review.status == "verified":
            if self.label_source == "candidate:unverified":
                errors.append("independent_label_source")
            if not self.evidence:
                errors.append("case_evidence")
            errors.extend(f"checks.{name}" for name in self.review.missing_checks)
            if self.review.dispute_reasons:
                errors.append("verified_case_has_dispute_reasons")
        elif not self.review.dispute_reasons:
            errors.append("dispute_reasons")
        return errors

    def fact_responder(self, *, max_requests: int | None = None) -> FactResponder:
        return FactResponder(self.clarification_facts, max_requests=max_requests)

    def public_payload(self) -> dict[str, Any]:
        """Return only fields that may enter the system under evaluation."""

        return {
            "id": self.id,
            "family_id": self.family_id,
            "category": self.category,
            "scope": self.scope,
            "initial_input": self.initial_input,
        }

    def strategy_payload(self) -> dict[str, Any]:
        """Return only the input that an evaluated strategy may observe."""

        return {"id": self.id, "initial_input": self.initial_input}


class FamilySplitManifest(BaseModel):
    """Reproducible family-level partition metadata."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["seeded_family_shuffle"] = "seeded_family_shuffle"
    seed: int = 0
    holdout_fraction: float = Field(gt=0, lt=1)
    development_families: list[str] = Field(default_factory=list)
    holdout_families: list[str] = Field(default_factory=list)

    @field_validator("development_families", "holdout_families")
    @classmethod
    def normalize_families(cls, value: list[str]) -> list[str]:
        families: list[str] = []
        for family in value:
            normalized = family.strip()
            if normalized and normalized not in families:
                families.append(normalized)
        return sorted(families)

    @model_validator(mode="after")
    def validate_disjoint_families(self) -> FamilySplitManifest:
        if set(self.development_families) & set(self.holdout_families):
            raise ValueError("development and holdout families must be disjoint")
        return self


class ScoringManifest(BaseModel):
    """Versioned, label-free list of Cases approved for formal scoring."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    case_ids: list[str]
    family_ids: list[str]
    ruleset_versions: list[str]
    reviewer_ids: list[str]
    split: FamilySplitManifest
    excluded_counts: dict[str, int]
    disputed_case_ids: list[str] = Field(default_factory=list)
    disputed_reviews: dict[str, dict[str, Any]] = Field(default_factory=dict)


class CaseDataset(BaseModel):
    """Versioned candidate/Golden Case collection and family splitter."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    cases: list[EvaluationCase] = Field(default_factory=list)
    split_manifest: FamilySplitManifest | None = None

    @field_validator("dataset_version")
    @classmethod
    def nonempty_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("dataset_version must not be empty")
        return value

    @model_validator(mode="after")
    def validate_structure(self) -> CaseDataset:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("case ids must be unique")
        self._validate_family_split()
        return self

    def _validate_family_split(self) -> None:
        by_family: dict[str, set[str]] = {}
        for case in self.cases:
            if case.split is not None:
                by_family.setdefault(case.family_id, set()).add(case.split)
        crossing = sorted(family for family, partitions in by_family.items() if len(partitions) > 1)
        if crossing:
            raise ValueError(
                "a family cannot cross development and holdout: " + ", ".join(crossing)
            )
        if self.split_manifest is None:
            return
        declared = set(self.split_manifest.development_families) | set(
            self.split_manifest.holdout_families
        )
        actual = {case.family_id for case in self.cases}
        if declared != actual:
            raise ValueError("family split manifest must cover every dataset family")
        holdout = set(self.split_manifest.holdout_families)
        for case in self.cases:
            expected = "holdout" if case.family_id in holdout else "development"
            if case.split != expected:
                raise ValueError(f"case {case.id} does not match its family split")

    @classmethod
    def from_json(cls, path: str | Path) -> CaseDataset:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"dataset_version": "candidate", "cases": payload}
        return cls.model_validate(payload)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @property
    def verified_cases(self) -> list[EvaluationCase]:
        return [case for case in self.cases if case.review.status == "verified"]

    @property
    def excluded_counts(self) -> dict[str, int]:
        return {
            "draft": sum(case.review.status == "draft" for case in self.cases),
            "disputed": sum(case.review.status == "disputed" for case in self.cases),
        }

    def validate_for_scoring(self, *, require_split: bool = False) -> list[EvaluationCase]:
        """Validate the human gate without promoting any Case review status."""

        if require_split and self.split_manifest is None:
            raise ValueError("formal scoring requires a family split manifest")
        self._validate_family_split()
        errors: dict[str, list[str]] = {}
        for case in self.cases:
            case_errors = case.scoring_validation_errors()
            if case_errors:
                errors[case.id] = case_errors
        if errors:
            details = "; ".join(
                f"{case_id}: {', '.join(items)}" for case_id, items in errors.items()
            )
            raise ValueError("dataset is not ready for scoring: " + details)
        return list(self.verified_cases)

    def scoring_manifest(self) -> ScoringManifest:
        """Return a versioned, label-free formal-scoring Case list."""

        cases = self.validate_for_scoring(require_split=True)
        if self.split_manifest is None:
            raise ValueError("formal scoring requires a family split manifest")
        return ScoringManifest(
            dataset_version=self.dataset_version,
            case_ids=[case.id for case in cases],
            family_ids=sorted({case.family_id for case in cases}),
            ruleset_versions=sorted(
                {case.review.rule_version or case.ruleset_id for case in cases}
            ),
            reviewer_ids=sorted(
                {case.review.reviewer_id for case in cases if case.review.reviewer_id}
            ),
            split=self.split_manifest,
            excluded_counts=self.excluded_counts,
            disputed_case_ids=sorted(
                case.id for case in self.cases if case.review.status == "disputed"
            ),
            disputed_reviews={
                case.id: case.review.model_dump(mode="json")
                for case in self.cases
                if case.review.status == "disputed"
            },
        )

    def save_scoring_manifest(self, path: str | Path) -> None:
        manifest = self.scoring_manifest()
        Path(path).write_text(
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def split_by_family(
        self, *, holdout_fraction: float = 0.2, seed: int = 0
    ) -> dict[str, list[EvaluationCase]]:
        if not 0 < holdout_fraction < 1:
            raise ValueError("holdout_fraction must be greater than 0 and less than 1")
        families = sorted({case.family_id for case in self.cases})
        shuffled = list(families)
        random.Random(seed).shuffle(shuffled)
        if len(shuffled) <= 1:
            holdout_families: set[str] = set()
        else:
            count = min(len(shuffled) - 1, max(1, round(len(shuffled) * holdout_fraction)))
            holdout_families = set(shuffled[:count])
        result = {
            "development": [case for case in self.cases if case.family_id not in holdout_families],
            "holdout": [case for case in self.cases if case.family_id in holdout_families],
        }
        for partition, cases in result.items():
            for case in cases:
                case.split = partition  # type: ignore[assignment]
        self.split_manifest = FamilySplitManifest(
            seed=seed,
            holdout_fraction=holdout_fraction,
            development_families=[family for family in families if family not in holdout_families],
            holdout_families=[family for family in families if family in holdout_families],
        )
        self._validate_family_split()
        return result


class ReplayResult(BaseModel):
    """Public and complete observations from one Case replay."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    family_id: str
    category: str
    initial_result: dict[str, Any]
    complete_result: dict[str, Any]
    fact_requests: list[FactRequest] = Field(default_factory=list)
    fact_answers: list[FactAnswer] = Field(default_factory=list)
    clarification_rounds: int = Field(default=0, ge=0)
    usage: dict[str, Any] = Field(default_factory=dict)
    run_count: int = Field(default=0, ge=0)
    initial_failure_reason: str | None = None
    complete_failure_reason: str | None = None
    failure_reason: str | None = None
    stop_reason: str | None = None

    @property
    def system_failure(self) -> bool:
        return self.system_failure_for("initial_result") or self.system_failure_for(
            "complete_result"
        )

    def system_failure_for(
        self, result_attribute: Literal["initial_result", "complete_result"]
    ) -> bool:
        result = getattr(self, result_attribute)
        specific = (
            self.initial_failure_reason
            if result_attribute == "initial_result"
            else self.complete_failure_reason
        )
        if specific:
            return True
        if (
            self.initial_failure_reason is None
            and self.complete_failure_reason is None
            and self.failure_reason
        ):
            return True
        return (
            result.get("failure_reason") in _SYSTEM_FAILURE_REASONS
            or result.get("reason") in _SYSTEM_FAILURE_REASONS
        )


class CaseAdapter(Protocol):
    """The small seam a replay runner needs from a system under evaluation."""

    def create_case(self) -> str: ...

    def submit_message(self, case_id: str, text: str) -> Mapping[str, Any]: ...


class FastAPICaseAdapter:
    """Adapt a synchronous FastAPI TestClient-like client to ``CaseAdapter``."""

    def __init__(self, client: Any):
        self.client = client

    def create_case(self) -> str:
        response = self.client.post("/api/cases", json={})
        self._ensure_success(response)
        return str(response.json()["id"])

    def submit_message(self, case_id: str, text: str) -> Mapping[str, Any]:
        response = self.client.post(f"/api/cases/{case_id}/messages", json={"text": text})
        self._ensure_success(response)
        return response.json()

    @staticmethod
    def _ensure_success(response: Any) -> None:
        if not 200 <= int(response.status_code) < 300:
            detail = getattr(response, "text", "")
            raise RuntimeError(f"case adapter request failed ({response.status_code}): {detail}")


class EvaluationRunner:
    """Replay labelled Cases through an injected system adapter.

    The runner gives the system only ``initial_input`` and facts returned for
    its explicit clarification targets. Gold labels, evidence, review records,
    and unasked facts never cross this seam.
    """

    def __init__(
        self,
        adapter: CaseAdapter,
        *,
        max_clarification_rounds: int = 8,
        max_fact_requests: int = 8,
    ):
        if max_clarification_rounds <= 0 or max_fact_requests <= 0:
            raise ValueError("clarification limits must be positive")
        self.adapter = adapter
        self.max_clarification_rounds = max_clarification_rounds
        self.max_fact_requests = max_fact_requests

    @staticmethod
    def _questions(response: Mapping[str, Any]) -> list[dict[str, str]]:
        questions: list[dict[str, str]] = []
        for raw in response.get("clarification_questions", []) or []:
            if not isinstance(raw, Mapping):
                continue
            field = str(raw.get("field", "")).strip()
            question = str(raw.get("question", "")).strip()
            if field or question:
                questions.append({"field": field, "question": question})
        return questions

    @staticmethod
    def _usage(responses: list[Mapping[str, Any]]) -> dict[str, Any]:
        for response in reversed(responses):
            investigation = response.get("investigation")
            if isinstance(investigation, Mapping) and isinstance(
                investigation.get("usage"), Mapping
            ):
                return dict(investigation["usage"])
        totals: dict[str, int | float] = {}
        for response in responses:
            usage = response.get("usage")
            if not isinstance(usage, Mapping):
                continue
            for key, value in usage.items():
                if isinstance(value, (int, float)):
                    totals[key] = totals.get(key, 0) + value
        return totals

    @staticmethod
    def _run_count(responses: list[Mapping[str, Any]]) -> int:
        for response in reversed(responses):
            investigation = response.get("investigation")
            if isinstance(investigation, Mapping) and investigation.get("run_count") is not None:
                return int(investigation["run_count"])
        return len(responses)

    def run_case(self, case: EvaluationCase) -> ReplayResult:
        case_id = self.adapter.create_case()
        initial = dict(self.adapter.submit_message(case_id, case.initial_input))
        current = initial
        responses: list[Mapping[str, Any]] = [initial]
        fact_requests: list[FactRequest] = []
        fact_answers: list[FactAnswer] = []
        allowed_facts = set(case.acceptable_questions)
        responder = FactResponder(
            {
                field: fixture
                for field, fixture in case.clarification_facts.items()
                if field in allowed_facts
            },
            max_requests=self.max_fact_requests,
        )
        rounds = 0
        runner_failure_reason: str | None = None
        runner_stop_reason: str | None = None
        while current.get("status") == "INSUFFICIENT_INFORMATION":
            if rounds >= self.max_clarification_rounds:
                runner_failure_reason = "CLARIFICATION_LIMIT_EXCEEDED"
                runner_stop_reason = "clarification_limit"
                break
            questions = self._questions(current)
            if not questions:
                runner_stop_reason = "no_clarification_questions"
                break
            fields = list(dict.fromkeys(item["field"] for item in questions if item["field"]))
            request = FactRequest(
                fields=fields,
                text=None if fields else " ".join(item["question"] for item in questions),
            )
            fact_requests.append(request)
            answer = responder.answer(request)
            fact_answers.append(answer)
            rounds += 1
            if answer.status != "answered" or not answer.text:
                if answer.status == "budget_exhausted":
                    runner_failure_reason = "FACT_REQUEST_BUDGET_EXHAUSTED"
                    runner_stop_reason = "fact_budget_exhausted"
                elif answer.status == "ambiguous":
                    runner_failure_reason = "AMBIGUOUS_FACT_REQUEST"
                    runner_stop_reason = "ambiguous_fact_request"
                else:
                    runner_stop_reason = "fact_unavailable"
                break
            current = dict(self.adapter.submit_message(case_id, answer.text))
            responses.append(current)

        failure_reason = runner_failure_reason or next(
            (
                str(response.get("failure_reason"))
                for response in reversed(responses)
                if response.get("failure_reason")
            ),
            None,
        )
        stop_reason = runner_stop_reason or next(
            (
                str(response.get("stop_reason"))
                for response in reversed(responses)
                if response.get("stop_reason")
            ),
            None,
        )
        initial_failure_reason = initial.get("failure_reason") or (
            str(initial.get("reason")) if initial.get("reason") in _SYSTEM_FAILURE_REASONS else None
        )
        complete_failure_reason = current.get("failure_reason") or (
            str(current.get("reason")) if current.get("reason") in _SYSTEM_FAILURE_REASONS else None
        )
        return ReplayResult(
            case_id=case.id,
            family_id=case.family_id,
            category=case.category,
            initial_result=initial,
            complete_result=current,
            fact_requests=fact_requests,
            fact_answers=fact_answers,
            clarification_rounds=rounds,
            usage=self._usage(responses),
            run_count=self._run_count(responses),
            initial_failure_reason=initial_failure_reason,
            complete_failure_reason=complete_failure_reason,
            failure_reason=failure_reason,
            stop_reason=stop_reason,
        )

    def run_dataset(
        self, dataset: CaseDataset, *, verified_only: bool = False
    ) -> list[ReplayResult]:
        cases = (
            dataset.validate_for_scoring(require_split=False) if verified_only else dataset.cases
        )
        return [self.run_case(case) for case in cases]


class Metric(BaseModel):
    """A ratio with an explicit numerator and denominator for N/A reporting."""

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = None

    @model_validator(mode="after")
    def calculate_value(self) -> Metric:
        if self.denominator == 0:
            self.value = None
        else:
            self.value = self.numerator / self.denominator
        return self

    def display(self) -> str:
        return "N/A" if self.value is None else f"{self.value:.3f}"


class ScoreView(BaseModel):
    sample_count: int = Field(ge=0)
    decision_domain_count: int = Field(ge=0)
    refusal_domain_count: int = Field(ge=0)
    correct_ruling_rate: Metric
    coverage_rate: Metric
    adjudicated_accuracy: Metric
    wrong_allow_rate: Metric
    correct_refusal_rate: Metric
    four_state_label_match: Metric
    by_category: dict[str, dict[str, Metric]] = Field(default_factory=dict)


class EvaluationReport(BaseModel):
    """Separate first-result and complete-investigation score report."""

    model_config = ConfigDict(extra="forbid")

    dataset_version: str
    scored_case_count: int = Field(ge=0)
    excluded_counts: dict[str, int]
    excluded_case_ids: dict[str, list[str]] = Field(default_factory=dict)
    excluded_reviews: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    outcomes: list[ReplayResult] = Field(default_factory=list)
    initial: ScoreView
    complete: ScoreView

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def from_json(cls, path: str | Path) -> EvaluationReport:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        lines = [
            f"# RuleCourt evaluation report ({self.dataset_version})",
            "",
            f"Scored cases: {self.scored_case_count}",
            f"Excluded draft cases: {self.excluded_counts.get('draft', 0)}",
            f"Excluded disputed cases: {self.excluded_counts.get('disputed', 0)}",
        ]
        for status, reviews in sorted(self.excluded_reviews.items()):
            for review in reviews:
                report = str(review.get("report", "")).strip()
                if report:
                    lines.append(f"- Excluded `{status}`: {report}")
        for name, view in (
            ("Initial result", self.initial),
            ("Complete investigation", self.complete),
        ):
            lines.extend(
                [
                    "",
                    f"## {name}",
                    "",
                    "| Metric | Result | n |",
                    "| --- | ---: | ---: |",
                ]
            )
            metrics = (
                ("Correct Ruling Rate", view.correct_ruling_rate),
                ("Coverage", view.coverage_rate),
                ("Adjudicated accuracy", view.adjudicated_accuracy),
                ("Wrong allow rate", view.wrong_allow_rate),
                ("Correct refusal rate", view.correct_refusal_rate),
                ("Four-state label match", view.four_state_label_match),
            )
            for label, metric in metrics:
                lines.append(
                    f"| {label} | {metric.display()} | {metric.numerator}/{metric.denominator} |"
                )
            if view.by_category:
                lines.extend(["", "### By category", ""])
                for category, category_metrics in sorted(view.by_category.items()):
                    lines.append(
                        f"- `{category}`: "
                        + "; ".join(
                            f"{label}={metric.display()} ({metric.numerator}/{metric.denominator})"
                            for label, metric in sorted(category_metrics.items())
                        )
                    )
        return "\n".join(lines) + "\n"


_DECISION_LABELS = {"LEGAL", "ILLEGAL"}
_SYSTEM_FAILURE_REASONS = {
    "INVESTIGATION_FAILED",
    "INVESTIGATION_TIMEOUT",
    "BUDGET_EXHAUSTED",
    "INVESTIGATION_INCOMPLETE",
}


def _metric(numerator: int, denominator: int) -> Metric:
    return Metric(numerator=numerator, denominator=denominator)


def _score_view(
    cases: list[EvaluationCase],
    outcomes: Mapping[str, ReplayResult],
    *,
    label_attribute: Literal["initial_label", "complete_label"],
    result_attribute: Literal["initial_result", "complete_result"],
) -> ScoreView:
    reason_attribute: Literal["initial_reason", "complete_reason"] = (
        "initial_reason" if label_attribute == "initial_label" else "complete_reason"
    )
    rows: list[tuple[EvaluationCase, str, bool]] = []
    for case in cases:
        outcome = outcomes.get(case.id)
        if outcome is None:
            actual = "UNRESOLVED"
            failed = True
        else:
            result = getattr(outcome, result_attribute)
            actual = str(result.get("status", "UNRESOLVED"))
            failed = outcome.system_failure_for(result_attribute)
        rows.append((case, actual, failed))

    decision_domain = [row for row in rows if getattr(row[0], label_attribute) in _DECISION_LABELS]
    refusal_domain = [
        row for row in rows if getattr(row[0], label_attribute) not in _DECISION_LABELS
    ]
    actual_decisions = [row for row in rows if row[1] in _DECISION_LABELS and not row[2]]
    correct_decisions = [
        row
        for row in rows
        if not row[2]
        and getattr(row[0], label_attribute) in _DECISION_LABELS
        and row[1] == getattr(row[0], label_attribute)
    ]
    correct_refusals = [
        row
        for row in refusal_domain
        if not row[2]
        and row[1] == getattr(row[0], label_attribute)
        and (
            not getattr(row[0], reason_attribute)
            or (
                outcomes.get(row[0].id) is not None
                and getattr(outcomes[row[0].id], result_attribute).get("reason")
                == getattr(row[0], reason_attribute)
            )
        )
    ]
    correct_rulings = [
        row for row in correct_decisions if row[0] in [item[0] for item in decision_domain]
    ]
    wrong_allows = [
        row
        for row in rows
        if not row[2] and getattr(row[0], label_attribute) == "ILLEGAL" and row[1] == "LEGAL"
    ]
    label_matches = [
        row for row in rows if not row[2] and row[1] == getattr(row[0], label_attribute)
    ]

    categories = sorted({case.category for case, _, _ in rows})
    by_category: dict[str, dict[str, Metric]] = {}
    for category in categories:
        category_rows = [row for row in rows if row[0].category == category]
        category_d = [
            row for row in category_rows if getattr(row[0], label_attribute) in _DECISION_LABELS
        ]
        category_a = [
            row for row in category_rows if getattr(row[0], label_attribute) not in _DECISION_LABELS
        ]
        category_actual = [
            row for row in category_rows if row[1] in _DECISION_LABELS and not row[2]
        ]
        category_correct = [
            row
            for row in category_rows
            if not row[2]
            and row[1] == getattr(row[0], label_attribute)
            and getattr(row[0], label_attribute) in _DECISION_LABELS
        ]
        category_refusal = [
            row
            for row in category_a
            if not row[2]
            and row[1] == getattr(row[0], label_attribute)
            and (
                not getattr(row[0], reason_attribute)
                or (
                    outcomes.get(row[0].id) is not None
                    and getattr(outcomes[row[0].id], result_attribute).get("reason")
                    == getattr(row[0], reason_attribute)
                )
            )
        ]
        category_illegal = [
            row for row in category_rows if getattr(row[0], label_attribute) == "ILLEGAL"
        ]
        category_wrong = [row for row in category_illegal if not row[2] and row[1] == "LEGAL"]
        category_match = [
            row
            for row in category_rows
            if not row[2] and row[1] == getattr(row[0], label_attribute)
        ]
        by_category[category] = {
            "correct_ruling_rate": _metric(len(category_correct), len(category_d)),
            "coverage_rate": _metric(len(category_actual), len(category_d)),
            "adjudicated_accuracy": _metric(len(category_correct), len(category_actual)),
            "wrong_allow_rate": _metric(len(category_wrong), len(category_illegal)),
            "correct_refusal_rate": _metric(len(category_refusal), len(category_a)),
            "four_state_label_match": _metric(len(category_match), len(category_rows)),
        }

    return ScoreView(
        sample_count=len(rows),
        decision_domain_count=len(decision_domain),
        refusal_domain_count=len(refusal_domain),
        correct_ruling_rate=_metric(len(correct_rulings), len(decision_domain)),
        coverage_rate=_metric(len(actual_decisions), len(decision_domain)),
        adjudicated_accuracy=_metric(len(correct_decisions), len(actual_decisions)),
        wrong_allow_rate=_metric(
            len(wrong_allows),
            sum(getattr(case, label_attribute) == "ILLEGAL" for case, _, _ in rows),
        ),
        correct_refusal_rate=_metric(len(correct_refusals), len(refusal_domain)),
        four_state_label_match=_metric(len(label_matches), len(rows)),
        by_category=by_category,
    )


def score_results(dataset: CaseDataset, results: list[ReplayResult]) -> EvaluationReport:
    """Score only verified Cases, retaining excluded counts for auditability."""

    verified = dataset.validate_for_scoring(require_split=False)
    outcomes = {
        result.case_id: result
        for result in results
        if result.case_id in {case.id for case in verified}
    }
    excluded_case_ids = {
        status: [case.id for case in dataset.cases if case.review.status == status]
        for status in ("draft", "disputed")
    }
    return EvaluationReport(
        dataset_version=dataset.dataset_version,
        scored_case_count=len(verified),
        excluded_counts=dataset.excluded_counts,
        excluded_case_ids=excluded_case_ids,
        excluded_reviews={
            status: [
                case.review.model_dump(mode="json")
                for case in dataset.cases
                if case.review.status == status
            ]
            for status in ("draft", "disputed")
        },
        outcomes=list(results),
        initial=_score_view(
            verified,
            outcomes,
            label_attribute="initial_label",
            result_attribute="initial_result",
        ),
        complete=_score_view(
            verified,
            outcomes,
            label_attribute="complete_label",
            result_attribute="complete_result",
        ),
    )


def main(argv: list[str] | None = None) -> int:
    """View or validate an exported evaluation artifact from the command line."""

    parser = ArgumentParser(prog="rulecourt-eval")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate", help="validate a candidate Case dataset")
    validate.add_argument("dataset", type=Path)
    validate.add_argument("--formal", action="store_true")

    report = subparsers.add_parser("report", help="view an exported evaluation report")
    report.add_argument("report", type=Path)
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--output", type=Path)

    manifest = subparsers.add_parser(
        "manifest",
        aliases=("release",),
        help="export the formal scoring manifest",
    )
    manifest.add_argument("dataset", type=Path)
    manifest.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "validate":
        dataset = CaseDataset.from_json(args.dataset)
        if args.formal:
            dataset.validate_for_scoring(require_split=True)
        payload = {
            "dataset_version": dataset.dataset_version,
            "case_count": len(dataset.cases),
            "verified_count": len(dataset.verified_cases),
            "excluded_counts": dataset.excluded_counts,
            "split_manifest": (
                dataset.split_manifest.model_dump(mode="json")
                if dataset.split_manifest is not None
                else None
            ),
        }
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    elif args.command in {"manifest", "release"}:
        dataset = CaseDataset.from_json(args.dataset)
        output = (
            json.dumps(
                dataset.scoring_manifest().model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    else:
        evaluation_report = EvaluationReport.from_json(args.report)
        output = (
            evaluation_report.to_markdown()
            if args.format == "markdown"
            else json.dumps(evaluation_report.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
    output_path = getattr(args, "output", None)
    if output_path:
        output_path.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
