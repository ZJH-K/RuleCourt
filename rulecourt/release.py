"""T16 frozen M0 evaluation release and evidence gate.

T11--T15 own the individual replay contracts.  This module is deliberately a
thin release seam over those reports: it freezes the experiment inputs from
development data, selects the holdout partition, aggregates repeated runs at
the Case level, and emits one signed artifact for the project decision-maker.
It never changes a score after the release configuration has been frozen.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import sys
from argparse import ArgumentParser
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .baselines import BaselineReport
from .budget import InvestigationBudget
from .comparison import (
    PairedMetric,
    PairedScoreView,
    TreatmentComparisonReport,
)
from .evaluation import (
    _DECISION_LABELS,
    CaseDataset,
    EvaluationCase,
    EvaluationReport,
    HumanSignoff,
    Metric,
    ReplayResult,
    ScoreView,
)

GROUPS = (
    "llm_only",
    "vanilla_rag",
    "hybrid_rag",
    "dynamic_agent",
    "fixed_workflow",
)
BASELINE_GROUPS = GROUPS[:3]
TREATMENT_GROUPS = GROUPS[3:]
GroupName = Literal[
    "llm_only",
    "vanilla_rag",
    "hybrid_rag",
    "dynamic_agent",
    "fixed_workflow",
]
ConclusionKind = Literal[
    "quality_gain",
    "efficiency_gain",
    "insufficient_evidence",
    "unproven_gain",
]

_SIGNATURE_ENV = "RULECOURT_T16_RELEASE_KEY"
_REQUIRED_GROUP_FIELDS = ("model_version", "strategy_version", "config_hash", "cache_condition")
_RESOURCE_FIELDS = (
    "provider_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "latency_ms",
    "cost_usd",
)
_REQUIRED_ARTIFACT_KINDS = frozenset(
    {
        "dataset",
        "rule_material",
        "coverage_table",
        "frozen_config",
        "human_review",
        "model",
        "strategy",
        "result",
    }
)


def _json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _nonempty(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} must not be empty")
    return value


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(float(value)):
        return 0
    return max(0, value)


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


class FrozenExperimentConfig(BaseModel):
    """All choices that must be frozen before a formal holdout replay.

    The fields are intentionally explicit instead of accepting an arbitrary
    experiment dictionary.  A development trial may leave fields unset while
    being explored; ``validate_for_formal`` is the one-way gate that requires
    every decision to be present and bound to the exact dataset digest.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "t16-freeze-v1"
    release_id: str
    dataset_version: str
    dataset_digest: str | None = None
    configuration_status: Literal["development_trial", "frozen"] = "development_trial"
    run_kind: Literal["development_trial", "formal"] = "development_trial"
    budget: dict[str, Any] = Field(default_factory=lambda: InvestigationBudget().to_dict())
    max_clarification_rounds: int | None = Field(default=None, gt=0)
    max_fact_requests: int | None = Field(default=None, gt=0)
    repeat_count: int | None = Field(default=None, gt=0)
    statistical_method: str | None = None
    min_quality_gain: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    wrong_allow_tolerance: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    require_wrong_allow_nonincrease: bool = True
    minimum_decision_cases: int | None = Field(default=None, gt=0)
    group_configs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rule_material_version: str | None = None
    coverage_table_version: str | None = None
    cache_condition: str | None = None
    development_families: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    reproduction_steps: list[str] = Field(default_factory=list)

    @field_validator(
        "schema_version",
        "release_id",
        "dataset_version",
        "statistical_method",
        "rule_material_version",
        "coverage_table_version",
        "cache_condition",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> Any:
        if value is None:
            return value
        return str(value).strip()

    @field_validator("development_families", "source_refs", "reproduction_steps")
    @classmethod
    def normalize_text_lists(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for item in value:
            item = str(item).strip()
            if item and item not in result:
                result.append(item)
        return result

    @model_validator(mode="before")
    @classmethod
    def normalize_budget(cls, values: Any) -> Any:
        if isinstance(values, Mapping):
            values = dict(values)
            if isinstance(values.get("budget"), InvestigationBudget):
                values["budget"] = values["budget"].to_dict()
        return values

    @model_validator(mode="after")
    def validate_shape(self) -> FrozenExperimentConfig:
        self.budget = InvestigationBudget.from_value(self.budget).to_dict()
        if self.dataset_digest is not None and (
            len(self.dataset_digest) != 64
            or any(char not in "0123456789abcdef" for char in self.dataset_digest)
        ):
            raise ValueError("dataset_digest must be a lowercase SHA-256 digest")
        if self.statistical_method is not None:
            _nonempty(self.statistical_method, "statistical_method")
        if self.configuration_status == "frozen" and self.run_kind != "formal":
            raise ValueError("a frozen T16 configuration must be formal")
        if self.run_kind == "formal" and self.configuration_status != "frozen":
            raise ValueError("formal T16 configuration requires frozen status")
        return self

    @classmethod
    def freeze_from_development(
        cls,
        dataset: CaseDataset,
        *,
        release_id: str,
        budget: InvestigationBudget | Mapping[str, Any],
        max_clarification_rounds: int,
        max_fact_requests: int,
        repeat_count: int,
        statistical_method: str,
        min_quality_gain: float,
        wrong_allow_tolerance: float,
        group_configs: Mapping[str, Mapping[str, Any]],
        rule_material_version: str,
        coverage_table_version: str,
        cache_condition: str,
        reproduction_steps: Sequence[str],
        minimum_decision_cases: int = 1,
        source_refs: Sequence[str] | None = None,
    ) -> FrozenExperimentConfig:
        """Freeze a complete config using only the development partition.

        The full dataset digest is retained so the later holdout run is bound
        to the same approved Case file, while the development family list is
        recorded as the tuning source.  Passing a dataset with no development
        partition is rejected; holdout labels never need to be inspected here.
        """

        if dataset.split_manifest is None or not dataset.split_manifest.development_families:
            raise ValueError("T16 freeze requires a non-empty development partition")
        development = {case.family_id for case in dataset.cases if case.split == "development"}
        if not development:
            raise ValueError("T16 freeze cannot use holdout-only data")
        expected = set(dataset.split_manifest.development_families)
        if development != expected:
            raise ValueError("development partition does not match the split manifest")
        normalized_groups = {
            str(group): dict(metadata) for group, metadata in group_configs.items()
        }
        return cls(
            schema_version="t16-freeze-v1",
            release_id=release_id,
            dataset_version=dataset.dataset_version,
            dataset_digest=dataset.approval_digest(),
            configuration_status="frozen",
            run_kind="formal",
            budget=(budget.to_dict() if isinstance(budget, InvestigationBudget) else dict(budget)),
            max_clarification_rounds=max_clarification_rounds,
            max_fact_requests=max_fact_requests,
            repeat_count=repeat_count,
            statistical_method=statistical_method,
            min_quality_gain=min_quality_gain,
            wrong_allow_tolerance=wrong_allow_tolerance,
            minimum_decision_cases=minimum_decision_cases,
            group_configs=normalized_groups,
            rule_material_version=rule_material_version,
            coverage_table_version=coverage_table_version,
            cache_condition=cache_condition,
            development_families=sorted(development),
            source_refs=list(source_refs or [f"dataset:{dataset.dataset_version}"]),
            reproduction_steps=list(reproduction_steps),
        )

    freeze = freeze_from_development

    @property
    def config_hash(self) -> str:
        return _digest(self.model_dump(mode="json"))

    def validate_for_formal(self, dataset: CaseDataset) -> None:
        if self.configuration_status != "frozen" or self.run_kind != "formal":
            raise ValueError("T16 formal replay requires a frozen configuration")
        if self.dataset_version != dataset.dataset_version:
            raise ValueError("frozen configuration dataset version does not match dataset")
        if self.dataset_digest != dataset.approval_digest():
            raise ValueError("frozen configuration dataset digest does not match dataset")
        if dataset.split_manifest is None:
            raise ValueError("T16 formal replay requires a family split manifest")
        if not dataset.split_manifest.development_families:
            raise ValueError("T16 formal replay requires development families")
        if not dataset.split_manifest.holdout_families:
            raise ValueError("T16 formal replay requires holdout families")
        if set(self.development_families) != set(dataset.split_manifest.development_families):
            raise ValueError("frozen configuration was not determined from this development split")
        budget = InvestigationBudget.from_value(self.budget)
        if budget.configuration_status != "frozen":
            raise ValueError("T16 formal replay requires a frozen budget")
        required_values = {
            "max_clarification_rounds": self.max_clarification_rounds,
            "max_fact_requests": self.max_fact_requests,
            "repeat_count": self.repeat_count,
            "statistical_method": self.statistical_method,
            "min_quality_gain": self.min_quality_gain,
            "wrong_allow_tolerance": self.wrong_allow_tolerance,
            "minimum_decision_cases": self.minimum_decision_cases,
            "rule_material_version": self.rule_material_version,
            "coverage_table_version": self.coverage_table_version,
            "cache_condition": self.cache_condition,
        }
        missing = [name for name, value in required_values.items() if value is None or value == ""]
        if missing:
            raise ValueError("T16 frozen configuration is incomplete: " + ", ".join(missing))
        if len(self.group_configs) != len(GROUPS) or set(self.group_configs) != set(GROUPS):
            missing_groups = sorted(set(GROUPS) - set(self.group_configs))
            raise ValueError(
                "T16 frozen configuration is missing groups: " + ", ".join(missing_groups)
            )
        missing_metadata: list[str] = []
        for group in GROUPS:
            metadata = self.group_configs[group]
            for field in _REQUIRED_GROUP_FIELDS:
                aliases = (field, "model" if field == "model_version" else field)
                if field == "strategy_version":
                    aliases = ("strategy_version", "strategy", "route_version")
                elif field == "config_hash":
                    aliases = ("config_hash",)
                elif field == "cache_condition":
                    aliases = ("cache_condition", "cache")
                if not any(str(metadata.get(alias, "")).strip() for alias in aliases):
                    missing_metadata.append(f"{group}.{field}")
        if missing_metadata:
            raise ValueError(
                "T16 frozen configuration is missing group metadata: " + ", ".join(missing_metadata)
            )
        if not self.source_refs:
            raise ValueError("T16 frozen configuration requires source references")
        if not self.reproduction_steps:
            raise ValueError("T16 frozen configuration requires reproduction steps")


class ArtifactReference(BaseModel):
    """A retained input or output artifact and its immutable digest."""

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    kind: str
    version: str
    digest: str = Field(pattern=r"^(?:sha256:)?[0-9a-f]{64}$|^hmac-sha256:[0-9a-f]{64}$")
    locator: str = ""
    cache_condition: str | None = None
    group: str | None = None
    repeat_index: int | None = Field(default=None, ge=1)
    case_ids: list[str] = Field(default_factory=list)
    family_ids: list[str] = Field(default_factory=list)

    @field_validator("artifact_id", "kind", "version", "locator", "cache_condition", mode="before")
    @classmethod
    def normalize_artifact_text(cls, value: Any) -> Any:
        return None if value is None else str(value).strip()

    @property
    def normalized_digest(self) -> str:
        return self.digest.removeprefix("sha256:")


class ReleaseGroupSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group: GroupName
    strategy_version: str
    model_version: str
    provider: str = ""
    config_hash: str
    cache_condition: str
    run_ids: list[str]
    repeat_count: int = Field(ge=1)
    case_ids: list[str]
    family_ids: list[str]
    sample_count: int = Field(ge=0)
    family_count: int = Field(ge=0)
    observation_count: int = Field(ge=0)
    initial: ScoreView
    complete: ScoreView
    usage: dict[str, Any] = Field(default_factory=dict)
    artifact_digests: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_counts(self) -> ReleaseGroupSummary:
        if self.sample_count != len(set(self.case_ids)):
            raise ValueError(f"{self.group} sample_count must use unique Case IDs")
        if self.family_count != len(set(self.family_ids)):
            raise ValueError(f"{self.group} family_count must use unique family IDs")
        if self.observation_count != self.sample_count:
            raise ValueError(
                f"{self.group} observation_count must not treat repeats as independent samples"
            )
        if len(self.run_ids) != self.repeat_count or len(set(self.run_ids)) != len(self.run_ids):
            raise ValueError(f"{self.group} repeated runs must have unique run IDs")
        return self


class PairedCaseSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    family_id: str
    category: str
    initial_statuses: dict[str, str]
    complete_statuses: dict[str, str]
    dynamic_fixed_initial: dict[str, str]
    dynamic_fixed_complete: dict[str, str]


class ReleasePairSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    initial: PairedScoreView
    complete: PairedScoreView
    initial_case_ids: list[str]
    complete_case_ids: list[str]
    cases: list[PairedCaseSummary] = Field(default_factory=list)


class ReleaseConclusion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    primary: ConclusionKind
    evidence_sufficient: bool
    quality_gain: bool
    efficiency_gain: bool
    wrong_allow_constraint_satisfied: bool
    quality_delta: float | None = None
    wrong_allow_delta: float | None = None
    efficiency_deltas: dict[str, float | None] = Field(default_factory=dict)
    reason: str


class ReleaseReport(BaseModel):
    """Signed, label-free-at-the-boundary T16 release artifact."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "t16-release-v1"
    release_id: str
    dataset_version: str
    dataset_digest: str
    run_kind: Literal["formal"] = "formal"
    config: FrozenExperimentConfig
    config_hash: str
    holdout_case_ids: list[str]
    holdout_family_ids: list[str]
    groups: dict[str, ReleaseGroupSummary]
    paired: ReleasePairSummary
    artifacts: list[ArtifactReference]
    human_review_references: list[str]
    reproduction_steps: list[str]
    limitations: list[str]
    conclusion: ReleaseConclusion
    signature: str = ""

    def signing_payload(self) -> bytes:
        payload = self.model_dump(mode="json", exclude={"signature"}, exclude_none=True)
        return _json(payload).encode("utf-8")

    def seal(self, signing_key: str) -> ReleaseReport:
        if not signing_key:
            raise ValueError("a T16 release signing key is required")
        signature = hmac.new(
            signing_key.encode("utf-8"), self.signing_payload(), hashlib.sha256
        ).hexdigest()
        return self.model_copy(update={"signature": f"hmac-sha256:{signature}"})

    def has_valid_signature(self, signing_key: str | None) -> bool:
        if not signing_key or not self.signature.startswith("hmac-sha256:"):
            return False
        expected = self.seal(signing_key).signature
        return hmac.compare_digest(self.signature, expected)

    def validate_integrity(self, *, signing_key: str | None = None) -> None:
        if self.release_id != self.config.release_id:
            raise ValueError("T16 release ID does not match the frozen config")
        if self.dataset_version != self.config.dataset_version:
            raise ValueError("T16 release dataset version does not match the frozen config")
        if self.config_hash != self.config.config_hash:
            raise ValueError("T16 release config hash does not match the frozen config")
        if self.dataset_digest != self.config.dataset_digest:
            raise ValueError("T16 release dataset digest does not match the frozen config")
        if set(self.groups) != set(GROUPS):
            raise ValueError("T16 release must contain exactly the five experiment groups")
        if self.run_kind != "formal":
            raise ValueError("T16 release artifacts must be formal")
        if not self.has_valid_signature(signing_key or os.getenv(_SIGNATURE_ENV)):
            raise ValueError("T16 release signature is missing or invalid")
        missing_artifact_kinds = _REQUIRED_ARTIFACT_KINDS - {item.kind for item in self.artifacts}
        if missing_artifact_kinds:
            raise ValueError(
                "T16 release is missing retained artifacts: "
                + ", ".join(sorted(missing_artifact_kinds))
            )
        if not self.human_review_references:
            raise ValueError("T16 release must retain human review references")
        if not self.reproduction_steps:
            raise ValueError("T16 release must retain reproduction steps")
        if not self.limitations:
            raise ValueError("T16 release must retain actual limitations")
        expected = _conclusion(self.config, self.groups)
        if expected.model_dump(mode="json") != self.conclusion.model_dump(mode="json"):
            raise ValueError("T16 release conclusion does not match the frozen metric policy")
        if len(self.holdout_case_ids) != len(set(self.holdout_case_ids)):
            raise ValueError("T16 holdout Case IDs must be unique")
        if len(self.holdout_family_ids) != len(set(self.holdout_family_ids)):
            raise ValueError("T16 holdout family IDs must be unique")
        for group, summary in self.groups.items():
            if summary.group != group:
                raise ValueError(f"T16 group key does not match summary: {group}")
            if summary.case_ids != self.holdout_case_ids:
                raise ValueError(f"{group} does not contain exactly the holdout Cases")
            if summary.family_ids != self.holdout_family_ids:
                raise ValueError(f"{group} does not contain exactly the holdout families")
        if (
            self.paired.initial_case_ids != self.holdout_case_ids
            or self.paired.complete_case_ids != self.holdout_case_ids
        ):
            raise ValueError("T16 paired metrics must use exactly the holdout Cases")
        if [case.case_id for case in self.paired.cases] != self.holdout_case_ids:
            raise ValueError("T16 paired Case records must cover the holdout exactly once")

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def from_json(cls, path: str | Path) -> ReleaseReport:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        lines = [
            f"# RuleCourt T16 release ({self.release_id})",
            "",
            f"Dataset: `{self.dataset_version}`; run kind: `{self.run_kind}`",
            f"Holdout Cases: {len(self.holdout_case_ids)}; holdout families: {len(self.holdout_family_ids)}",
            "",
            "| Group | Initial correct ruling | Complete correct ruling | Cases | Families | Repeats | Tokens | Cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
        for group in GROUPS:
            summary = self.groups[group]
            lines.append(
                f"| `{group}` | {summary.initial.correct_ruling_rate.display()} "
                f"({summary.initial.correct_ruling_rate.numerator}/{summary.initial.correct_ruling_rate.denominator}) | "
                f"{summary.complete.correct_ruling_rate.display()} "
                f"({summary.complete.correct_ruling_rate.numerator}/{summary.complete.correct_ruling_rate.denominator}) | "
                f"{summary.sample_count} | {summary.family_count} | {summary.repeat_count} | "
                f"{summary.usage.get('total_tokens', 0)} | {summary.usage.get('cost_usd', 'N/A')} |"
            )
        lines.extend(
            [
                "",
                "## Paired Dynamic Agent / Fixed Workflow",
                "",
                f"Complete paired Cases: {len(self.paired.complete_case_ids)}",
                f"Complete Correct Ruling Rate delta: {self.paired.complete.metrics['correct_ruling_rate'].value_delta}",
                f"Complete Wrong Allow Rate delta: {self.paired.complete.metrics['wrong_allow_rate'].value_delta}",
                "",
                "## Conclusion",
                "",
                f"Primary conclusion: **{self.conclusion.primary}**",
                f"Quality gain: `{self.conclusion.quality_gain}`; efficiency gain: `{self.conclusion.efficiency_gain}`; evidence sufficient: `{self.conclusion.evidence_sufficient}`.",
                self.conclusion.reason,
                "",
                "## Reproduction",
                "",
                *[f"1. {step}" for step in self.reproduction_steps],
                "",
                "## Limitations",
                "",
                *[f"- {item}" for item in self.limitations],
                "",
            ]
        )
        return "\n".join(lines)


def _metric(numerator: int, denominator: int) -> Metric:
    return Metric(numerator=numerator, denominator=denominator)


def _representative(
    outcomes: Sequence[tuple[str, bool, str | None]],
    method: str,
) -> tuple[str, bool, str | None]:
    if not outcomes:
        return "UNRESOLVED", True, None
    if method.endswith("unanimous"):
        first = outcomes[0]
        if all(item == first for item in outcomes) and not first[1]:
            return first
        return "UNRESOLVED", True, None
    if method.endswith("majority"):
        counts = Counter(item[0] for item in outcomes)
        best = max(counts.values())
        winners = sorted(status for status, count in counts.items() if count == best)
        if len(winners) != 1:
            return "UNRESOLVED", True, None
        status = winners[0]
        matching = next(item for item in outcomes if item[0] == status)
        return status, any(item[1] for item in outcomes), matching[2]
    raise ValueError("unsupported T16 statistical method: " + method)


def _rows_for_view(
    dataset: CaseDataset,
    reports: Sequence[EvaluationReport],
    case_ids: Sequence[str],
    *,
    view: Literal["initial", "complete"],
    method: str,
) -> list[tuple[EvaluationCase, str, bool, str | None]]:
    cases = {case.id: case for case in dataset.verified_cases}
    by_report: list[dict[str, ReplayResult]] = []
    for report in reports:
        indexed: dict[str, ReplayResult] = {}
        for outcome in report.outcomes:
            if outcome.case_id in indexed:
                raise ValueError(f"duplicate Case ID in T16 evaluation report: {outcome.case_id}")
            indexed[outcome.case_id] = outcome
        by_report.append(indexed)
    rows: list[tuple[EvaluationCase, str, bool, str | None]] = []
    result_attribute = "initial_result" if view == "initial" else "complete_result"
    reason_attribute = "initial_reason" if view == "initial" else "complete_reason"
    for case_id in case_ids:
        case = cases.get(case_id)
        if case is None:
            raise ValueError(f"T16 report references an unknown or unverified Case: {case_id}")
        observations: list[tuple[str, bool, str | None]] = []
        expected_reason = getattr(case, reason_attribute)
        for indexed in by_report:
            outcome = indexed.get(case_id)
            if outcome is None:
                observations.append(("UNRESOLVED", True, None))
                continue
            result = getattr(outcome, result_attribute)
            status = str(result.get("status", "UNRESOLVED"))
            failed = outcome.system_failure_for(result_attribute)
            reason = str(result.get("reason")) if result.get("reason") is not None else None
            observations.append((status, failed, reason))
        status, failed, reason = _representative(observations, method)
        if expected_reason and reason is None:
            reason = expected_reason
        rows.append((case, status, failed, reason))
    return rows


def _score_rows(rows: Sequence[tuple[EvaluationCase, str, bool, str | None]]) -> ScoreView:
    decision_domain = [row for row in rows if row[0].complete_label in _DECISION_LABELS]
    # The caller replaces the expected view label before invoking this helper.
    del decision_domain
    raise AssertionError("_score_rows requires a view label")


def _score_rows_for_label(
    rows: Sequence[tuple[EvaluationCase, str, bool, str | None]],
    *,
    label_attribute: Literal["initial_label", "complete_label"],
    reason_attribute: Literal["initial_reason", "complete_reason"],
) -> ScoreView:
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
        and (not getattr(row[0], reason_attribute) or row[3] == getattr(row[0], reason_attribute))
    ]
    wrong_allows = [
        row
        for row in rows
        if not row[2] and getattr(row[0], label_attribute) == "ILLEGAL" and row[1] == "LEGAL"
    ]
    label_matches = [
        row for row in rows if not row[2] and row[1] == getattr(row[0], label_attribute)
    ]
    categories = sorted({row[0].category for row in rows})
    by_category: dict[str, dict[str, Metric]] = {}
    for category in categories:
        category_rows = [row for row in rows if row[0].category == category]
        category_decisions = [
            row for row in category_rows if getattr(row[0], label_attribute) in _DECISION_LABELS
        ]
        category_refusals = [
            row for row in category_rows if getattr(row[0], label_attribute) not in _DECISION_LABELS
        ]
        category_actual = [
            row for row in category_rows if row[1] in _DECISION_LABELS and not row[2]
        ]
        category_correct = [
            row
            for row in category_rows
            if not row[2]
            and getattr(row[0], label_attribute) in _DECISION_LABELS
            and row[1] == getattr(row[0], label_attribute)
        ]
        category_wrong = [
            row
            for row in category_rows
            if not row[2] and getattr(row[0], label_attribute) == "ILLEGAL" and row[1] == "LEGAL"
        ]
        category_correct_refusals = [
            row
            for row in category_refusals
            if not row[2]
            and row[1] == getattr(row[0], label_attribute)
            and (
                not getattr(row[0], reason_attribute) or row[3] == getattr(row[0], reason_attribute)
            )
        ]
        category_matches = [
            row
            for row in category_rows
            if not row[2] and row[1] == getattr(row[0], label_attribute)
        ]
        by_category[category] = {
            "correct_ruling_rate": _metric(len(category_correct), len(category_decisions)),
            "coverage_rate": _metric(len(category_actual), len(category_decisions)),
            "adjudicated_accuracy": _metric(len(category_correct), len(category_actual)),
            "wrong_allow_rate": _metric(
                len(category_wrong),
                sum(getattr(row[0], label_attribute) == "ILLEGAL" for row in category_rows),
            ),
            "correct_refusal_rate": _metric(len(category_correct_refusals), len(category_refusals)),
            "four_state_label_match": _metric(len(category_matches), len(category_rows)),
        }
    return ScoreView(
        sample_count=len(rows),
        decision_domain_count=len(decision_domain),
        refusal_domain_count=len(refusal_domain),
        correct_ruling_rate=_metric(len(correct_decisions), len(decision_domain)),
        coverage_rate=_metric(len(actual_decisions), len(decision_domain)),
        adjudicated_accuracy=_metric(len(correct_decisions), len(actual_decisions)),
        wrong_allow_rate=_metric(
            len(wrong_allows),
            sum(getattr(row[0], label_attribute) == "ILLEGAL" for row in rows),
        ),
        correct_refusal_rate=_metric(len(correct_refusals), len(refusal_domain)),
        four_state_label_match=_metric(len(label_matches), len(rows)),
        by_category=by_category,
    )


def _aggregate_score(
    dataset: CaseDataset,
    reports: Sequence[EvaluationReport],
    case_ids: Sequence[str],
    *,
    view: Literal["initial", "complete"],
    method: str,
) -> ScoreView:
    rows = _rows_for_view(dataset, reports, case_ids, view=view, method=method)
    return _score_rows_for_label(
        rows,
        label_attribute="initial_label" if view == "initial" else "complete_label",
        reason_attribute="initial_reason" if view == "initial" else "complete_reason",
    )


def _aggregate_usage(
    usages: Sequence[Mapping[str, Any]], *, repeat_count: int, case_count: int
) -> dict[str, Any]:
    totals: dict[str, Any] = {}
    known_cost = True
    costs: list[float] = []
    latencies: list[float] = []
    for usage in usages:
        for field in _RESOURCE_FIELDS:
            value = usage.get(field)
            if field == "cost_usd":
                if value is None and usage.get("provider_calls", 0):
                    known_cost = False
                elif value is not None:
                    costs.append(float(_number(value)))
                continue
            totals[field] = totals.get(field, 0) + _number(value)
        if usage.get("latency_ms") is not None:
            latencies.append(float(_number(usage.get("latency_ms"))))
    totals["cost_usd"] = round(sum(costs), 8) if known_cost else None  # type: ignore[assignment]
    totals["known_cost_usd"] = round(sum(costs), 8)
    totals["repeat_count"] = repeat_count
    totals["unique_case_count"] = case_count
    totals["observation_count"] = case_count
    totals["latency_p50_ms"] = _percentile(latencies, 0.5)
    totals["latency_p95_ms"] = _percentile(latencies, 0.95)
    totals["resource_observations"] = len(usages)
    return dict(totals)


def _paired_metric(dynamic: Metric, fixed: Metric) -> PairedMetric:
    value_delta = (
        None if dynamic.value is None or fixed.value is None else dynamic.value - fixed.value
    )
    return PairedMetric(
        dynamic=dynamic,
        fixed=fixed,
        numerator_delta=dynamic.numerator - fixed.numerator,
        denominator_delta=dynamic.denominator - fixed.denominator,
        value_delta=value_delta,
    )


def _paired_view(dynamic: ScoreView, fixed: ScoreView) -> PairedScoreView:
    names = (
        "correct_ruling_rate",
        "coverage_rate",
        "adjudicated_accuracy",
        "wrong_allow_rate",
        "correct_refusal_rate",
        "four_state_label_match",
    )
    return PairedScoreView(
        dynamic=dynamic,
        fixed=fixed,
        metrics={
            name: _paired_metric(getattr(dynamic, name), getattr(fixed, name)) for name in names
        },
    )


def _conclusion(
    config: FrozenExperimentConfig,
    groups: Mapping[str, ReleaseGroupSummary],
) -> ReleaseConclusion:
    dynamic = groups["dynamic_agent"].complete
    fixed = groups["fixed_workflow"].complete
    quality_delta = _delta(dynamic.correct_ruling_rate.value, fixed.correct_ruling_rate.value)
    wrong_delta = _delta(dynamic.wrong_allow_rate.value, fixed.wrong_allow_rate.value)
    evidence_sufficient = bool(
        dynamic.sample_count >= int(config.minimum_decision_cases or 0)
        and dynamic.correct_ruling_rate.value is not None
        and fixed.correct_ruling_rate.value is not None
        and dynamic.wrong_allow_rate.value is not None
        and fixed.wrong_allow_rate.value is not None
    )
    wrong_constraint = bool(
        wrong_delta is not None and wrong_delta <= float(config.wrong_allow_tolerance or 0)
        if config.require_wrong_allow_nonincrease
        else True
    )
    quality_gain = bool(
        evidence_sufficient
        and quality_delta is not None
        and quality_delta >= float(config.min_quality_gain or 0)
        and wrong_constraint
    )
    efficiency_deltas = {
        field: _delta(
            _usage_value(groups["dynamic_agent"].usage, field),
            _usage_value(groups["fixed_workflow"].usage, field),
        )
        for field in ("total_tokens", "cost_usd", "latency_ms")
    }
    efficiency_gain = bool(
        evidence_sufficient
        and any(value is not None and value < 0 for value in efficiency_deltas.values())
        and all(value is None or value <= 0 for value in efficiency_deltas.values())
    )
    if not evidence_sufficient:
        primary: ConclusionKind = "insufficient_evidence"
        reason = "The frozen holdout does not contain enough comparable decision evidence; no gain is claimed."
    elif quality_gain:
        primary = "quality_gain"
        reason = "Dynamic Agent meets the frozen quality-gain threshold without exceeding the frozen wrong-allow constraint."
    elif efficiency_gain:
        primary = "efficiency_gain"
        reason = "Quality gain was not demonstrated, but the frozen comparison shows an efficiency improvement with no resource regression."
    else:
        primary = "unproven_gain"
        reason = "The formal experiment completed, but the frozen quality and efficiency criteria do not prove an Agent increment."
    return ReleaseConclusion(
        primary=primary,
        evidence_sufficient=evidence_sufficient,
        quality_gain=quality_gain,
        efficiency_gain=efficiency_gain,
        wrong_allow_constraint_satisfied=wrong_constraint,
        quality_delta=quality_delta,
        wrong_allow_delta=wrong_delta,
        efficiency_deltas=efficiency_deltas,
        reason=reason,
    )


def _delta(left: float | None, right: float | None) -> float | None:
    return None if left is None or right is None else left - right


def _usage_value(usage: Mapping[str, Any], field: str) -> float | None:
    value = usage.get(field)
    if value is None:
        return None
    return float(_number(value))


def _metadata_value(metadata: Mapping[str, Any], field: str, group: str) -> str:
    aliases = {
        "model_version": ("model_version", "model"),
        "strategy_version": ("strategy_version", "strategy", "route_version", "config_version"),
        "config_hash": ("config_hash",),
        "cache_condition": ("cache_condition", "cache"),
    }[field]
    for key in aliases:
        value = metadata.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _auto_artifacts(
    dataset: CaseDataset,
    config: FrozenExperimentConfig,
    signoff: HumanSignoff,
    report_payloads: Mapping[str, Sequence[Mapping[str, Any]]],
    holdout_case_ids: Sequence[str],
    holdout_family_ids: Sequence[str],
) -> list[ArtifactReference]:
    artifacts = [
        ArtifactReference(
            artifact_id=dataset.dataset_version,
            kind="dataset",
            version=dataset.dataset_version,
            digest=dataset.approval_digest(),
            locator="case-dataset",
            case_ids=list(holdout_case_ids),
            family_ids=list(holdout_family_ids),
        ),
        ArtifactReference(
            artifact_id=config.rule_material_version or "rules",
            kind="rule_material",
            version=config.rule_material_version or "unknown",
            digest=_digest(
                {"ruleset_id": "root-law-2025-10", "version": config.rule_material_version}
            ),
            cache_condition=config.cache_condition,
        ),
        ArtifactReference(
            artifact_id=config.coverage_table_version or "coverage-table",
            kind="coverage_table",
            version=config.coverage_table_version or "unknown",
            digest=_digest({"version": config.coverage_table_version}),
        ),
        ArtifactReference(
            artifact_id=config.release_id,
            kind="frozen_config",
            version=config.schema_version,
            digest=config.config_hash,
        ),
        ArtifactReference(
            artifact_id=signoff.approval_reference,
            kind="human_review",
            version=signoff.signed_at,
            digest=_digest(signoff.model_dump(mode="json")),
            locator="detached-human-signoff",
        ),
    ]
    for group, payloads in report_payloads.items():
        metadata = config.group_configs[group]
        artifacts.extend(
            [
                ArtifactReference(
                    artifact_id=f"{group}-model",
                    kind="model",
                    version=str(metadata.get("model_version", metadata.get("model", "unknown"))),
                    digest=_digest({"group": group, "model": metadata}),
                    group=group,
                ),
                ArtifactReference(
                    artifact_id=f"{group}-strategy",
                    kind="strategy",
                    version=str(
                        metadata.get("strategy_version", metadata.get("strategy", "unknown"))
                    ),
                    digest=_digest({"group": group, "strategy": metadata}),
                    group=group,
                ),
            ]
        )
        for repeat_index, payload in enumerate(payloads, start=1):
            artifacts.append(
                ArtifactReference(
                    artifact_id=f"{group}-repeat-{repeat_index}",
                    kind="result",
                    version="t16-replay-v1",
                    digest=_digest(payload),
                    group=group,
                    repeat_index=repeat_index,
                    cache_condition=config.cache_condition,
                    case_ids=list(holdout_case_ids),
                    family_ids=list(holdout_family_ids),
                )
            )
    return artifacts


class ReleaseBuilder:
    """Build a T16 release from the already-audited T12--T14 reports."""

    def __init__(self, config: FrozenExperimentConfig):
        self.config = config

    @staticmethod
    def _as_reports(value: Any) -> list[Any]:
        if isinstance(value, (BaselineReport, TreatmentComparisonReport)):
            return [value]
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return list(value)
        raise TypeError("T16 reports must be a report or a sequence of reports")

    def build(
        self,
        dataset: CaseDataset,
        *,
        baseline_reports: BaselineReport | Sequence[BaselineReport],
        treatment_reports: TreatmentComparisonReport | Sequence[TreatmentComparisonReport],
        signoff: HumanSignoff,
        signing_key: str,
        artifacts: Sequence[ArtifactReference] | None = None,
        limitations: Sequence[str] | None = None,
    ) -> ReleaseReport:
        self.config.validate_for_formal(dataset)
        dataset.validate_for_scoring(
            require_split=True,
            require_coverage=True,
            signoff=signoff,
            signing_key=signing_key,
        )
        holdout_cases = [case for case in dataset.verified_cases if case.split == "holdout"]
        if not holdout_cases:
            raise ValueError("T16 formal release requires verified holdout Cases")
        holdout_case_ids = sorted(case.id for case in holdout_cases)
        holdout_family_ids = sorted({case.family_id for case in holdout_cases})
        baseline_items = self._as_reports(baseline_reports)
        treatment_items = self._as_reports(treatment_reports)
        if len(baseline_items) != self.config.repeat_count:
            raise ValueError("T16 baseline report count does not match frozen repeat_count")
        if len(treatment_items) != self.config.repeat_count:
            raise ValueError("T16 treatment report count does not match frozen repeat_count")
        if any(not isinstance(item, BaselineReport) for item in baseline_items):
            raise TypeError("baseline_reports must contain BaselineReport artifacts")
        if any(not isinstance(item, TreatmentComparisonReport) for item in treatment_items):
            raise TypeError("treatment_reports must contain TreatmentComparisonReport artifacts")
        baseline_items = cast(list[BaselineReport], baseline_items)
        treatment_items = cast(list[TreatmentComparisonReport], treatment_items)
        self._validate_report_headers(dataset, baseline_items, treatment_items)

        group_evaluations: dict[str, list[EvaluationReport]] = {group: [] for group in GROUPS}
        group_metadata: dict[str, list[Mapping[str, Any]]] = {group: [] for group in GROUPS}
        report_payloads: dict[str, list[Mapping[str, Any]]] = {group: [] for group in GROUPS}
        for baseline in baseline_items:
            for group in BASELINE_GROUPS:
                run = baseline.runs.get(group)
                if run is None:
                    raise ValueError(f"T16 baseline report is missing group: {group}")
                group_evaluations[group].append(run.evaluation)
                group_metadata[group].append(run.config)
                report_payloads[group].append(run.model_dump(mode="json"))
        for treatment in treatment_items:
            for group in TREATMENT_GROUPS:
                run = treatment.runs.get(group)
                if run is None:
                    raise ValueError(f"T16 treatment report is missing group: {group}")
                group_evaluations[group].append(run.evaluation)
                group_metadata[group].append(run.config)
                report_payloads[group].append(run.model_dump(mode="json"))

        summaries: dict[str, ReleaseGroupSummary] = {}
        for group in GROUPS:
            summaries[group] = self._build_group_summary(
                dataset,
                group,
                group_evaluations[group],
                group_metadata[group],
                report_payloads[group],
                holdout_case_ids,
                holdout_family_ids,
            )
        paired = self._build_pairs(
            dataset,
            summaries,
            group_evaluations,
            holdout_case_ids,
        )
        artifact_list = list(artifacts or [])
        if not artifact_list:
            artifact_list = _auto_artifacts(
                dataset,
                self.config,
                signoff,
                report_payloads,
                holdout_case_ids,
                holdout_family_ids,
            )
        self._validate_artifacts(artifact_list, dataset, holdout_case_ids, holdout_family_ids)
        report = ReleaseReport(
            release_id=self.config.release_id,
            dataset_version=dataset.dataset_version,
            dataset_digest=dataset.approval_digest(),
            config=self.config,
            config_hash=self.config.config_hash,
            holdout_case_ids=holdout_case_ids,
            holdout_family_ids=holdout_family_ids,
            groups=summaries,
            paired=paired,
            artifacts=artifact_list,
            human_review_references=[signoff.approval_reference, *signoff.reviewer_ids],
            reproduction_steps=list(self.config.reproduction_steps),
            limitations=list(
                limitations
                or [
                    "The holdout is clustered by canonical family; repeated runs are not independent samples.",
                    "Only Dynamic Agent versus Fixed Workflow is an Agent-increment comparison; external baselines are context.",
                    "Unknown provider billing or missing usage remains N/A rather than being estimated as zero.",
                ]
            ),
            conclusion=_conclusion(self.config, summaries),
        ).seal(signing_key)
        report.validate_integrity(signing_key=signing_key)
        return report

    def _validate_report_headers(
        self,
        dataset: CaseDataset,
        baselines: Sequence[BaselineReport],
        treatments: Sequence[TreatmentComparisonReport],
    ) -> None:
        for report in [*baselines, *treatments]:
            if report.dataset_version != dataset.dataset_version:
                raise ValueError("T16 report dataset version does not match frozen dataset")
            if getattr(report, "run_kind", None) != "formal":
                raise ValueError("T16 formal release cannot include a development-trial report")
        if any(set(report.runs) != set(BASELINE_GROUPS) for report in baselines):
            raise ValueError("T16 baseline reports must contain llm_only, vanilla_rag, hybrid_rag")
        if any(set(report.runs) != set(TREATMENT_GROUPS) for report in treatments):
            raise ValueError("T16 treatment reports must contain dynamic_agent and fixed_workflow")

    def _build_group_summary(
        self,
        dataset: CaseDataset,
        group: str,
        evaluations: Sequence[EvaluationReport],
        metadata: Sequence[Mapping[str, Any]],
        payloads: Sequence[Mapping[str, Any]],
        holdout_case_ids: Sequence[str],
        holdout_family_ids: Sequence[str],
    ) -> ReleaseGroupSummary:
        if not evaluations:
            raise ValueError(f"T16 group has no replay reports: {group}")
        if len(evaluations) != self.config.repeat_count:
            raise ValueError(f"T16 group repeat count mismatch: {group}")
        expected = set(holdout_case_ids)
        for evaluation in evaluations:
            actual = {outcome.case_id for outcome in evaluation.outcomes}
            missing = expected - actual
            if missing:
                raise ValueError(
                    f"T16 {group} replay is missing holdout Cases: " + ", ".join(sorted(missing))
                )
        frozen = self.config.group_configs[group]
        resolved: dict[str, str] = {}
        if len(metadata) != len(evaluations) or len(payloads) != len(evaluations):
            raise ValueError(f"T16 {group} replay metadata and payload counts do not match")
        provider = ""
        for repeat_index, metadata_item in enumerate(metadata, start=1):
            current: dict[str, str] = {}
            for field in _REQUIRED_GROUP_FIELDS:
                current[field] = _metadata_value(metadata_item, field, group)
                expected_value = _metadata_value(frozen, field, group)
                if expected_value and current[field] != expected_value:
                    raise ValueError(
                        f"T16 {group} repeat {repeat_index} {field} does not match frozen configuration"
                    )
                if not current[field]:
                    raise ValueError(f"T16 {group} replay is missing {field} metadata")
            if not resolved:
                resolved.update(current)
            elif current != resolved:
                raise ValueError(f"T16 {group} repeated runs changed frozen metadata")
            current_provider = str(metadata_item.get("provider", "")).strip()
            if repeat_index == 1:
                provider = current_provider
            elif current_provider != provider:
                raise ValueError(f"T16 {group} repeated runs changed provider metadata")
        run_ids: list[str] = []
        for index, item in enumerate(metadata, start=1):
            run_id = str(item.get("run_id", f"{group}-repeat-{index}")).strip()
            if not run_id:
                raise ValueError(f"T16 {group} run ID must not be empty")
            if run_id in run_ids:
                raise ValueError(f"T16 {group} repeated runs reuse run ID {run_id}")
            run_ids.append(run_id)
        initial = _aggregate_score(
            dataset,
            evaluations,
            holdout_case_ids,
            view="initial",
            method=str(self.config.statistical_method),
        )
        complete = _aggregate_score(
            dataset,
            evaluations,
            holdout_case_ids,
            view="complete",
            method=str(self.config.statistical_method),
        )
        usage = _aggregate_usage(
            [
                _usage_for_payload(payload, evaluation)
                for payload, evaluation in zip(payloads, evaluations, strict=True)
            ],
            repeat_count=len(evaluations),
            case_count=len(holdout_case_ids),
        )
        return ReleaseGroupSummary(
            group=cast(GroupName, group),
            strategy_version=resolved["strategy_version"],
            model_version=resolved["model_version"],
            provider=provider,
            config_hash=resolved["config_hash"],
            cache_condition=resolved["cache_condition"],
            run_ids=run_ids,
            repeat_count=len(evaluations),
            case_ids=list(holdout_case_ids),
            family_ids=list(holdout_family_ids),
            sample_count=len(holdout_case_ids),
            family_count=len(holdout_family_ids),
            observation_count=len(holdout_case_ids),
            initial=initial,
            complete=complete,
            usage=usage,
            artifact_digests=[_digest(payload) for payload in payloads],
        )

    def _build_pairs(
        self,
        dataset: CaseDataset,
        summaries: Mapping[str, ReleaseGroupSummary],
        evaluations: Mapping[str, Sequence[EvaluationReport]],
        holdout_case_ids: Sequence[str],
    ) -> ReleasePairSummary:
        initial = _paired_view(
            summaries["dynamic_agent"].initial, summaries["fixed_workflow"].initial
        )
        complete = _paired_view(
            summaries["dynamic_agent"].complete, summaries["fixed_workflow"].complete
        )
        cases_by_id = {case.id: case for case in dataset.verified_cases}
        paired_cases: list[PairedCaseSummary] = []
        for case_id in holdout_case_ids:
            case = cases_by_id[case_id]
            initial_statuses: dict[str, str] = {}
            complete_statuses: dict[str, str] = {}
            for group in GROUPS:
                for view, target in (
                    ("initial", initial_statuses),
                    ("complete", complete_statuses),
                ):
                    rows = _rows_for_view(
                        dataset,
                        evaluations[group],
                        [case_id],
                        view=cast(Literal["initial", "complete"], view),
                        method=str(self.config.statistical_method),
                    )
                    target[group] = rows[0][1]
            paired_cases.append(
                PairedCaseSummary(
                    case_id=case_id,
                    family_id=case.family_id,
                    category=case.category,
                    initial_statuses=initial_statuses,
                    complete_statuses=complete_statuses,
                    dynamic_fixed_initial={
                        "dynamic_agent": initial_statuses["dynamic_agent"],
                        "fixed_workflow": initial_statuses["fixed_workflow"],
                    },
                    dynamic_fixed_complete={
                        "dynamic_agent": complete_statuses["dynamic_agent"],
                        "fixed_workflow": complete_statuses["fixed_workflow"],
                    },
                )
            )
        return ReleasePairSummary(
            initial=initial,
            complete=complete,
            initial_case_ids=list(holdout_case_ids),
            complete_case_ids=list(holdout_case_ids),
            cases=paired_cases,
        )

    @staticmethod
    def _validate_artifacts(
        artifacts: Sequence[ArtifactReference],
        dataset: CaseDataset,
        holdout_case_ids: Sequence[str],
        holdout_family_ids: Sequence[str],
    ) -> None:
        if not artifacts:
            raise ValueError("T16 release requires retained artifacts")
        ids = [item.artifact_id for item in artifacts]
        if len(ids) != len(set(ids)):
            raise ValueError("T16 artifact IDs must be unique")
        dataset_artifacts = [item for item in artifacts if item.kind == "dataset"]
        if not dataset_artifacts:
            raise ValueError("T16 release must retain the exact dataset artifact")
        if not any(item.digest == dataset.approval_digest() for item in dataset_artifacts):
            raise ValueError("T16 dataset artifact digest does not match the approved dataset")
        missing_artifact_kinds = _REQUIRED_ARTIFACT_KINDS - {item.kind for item in artifacts}
        if missing_artifact_kinds:
            raise ValueError(
                "T16 release is missing retained artifacts: "
                + ", ".join(sorted(missing_artifact_kinds))
            )
        for item in artifacts:
            if item.case_ids and set(item.case_ids) - set(holdout_case_ids):
                raise ValueError(f"T16 artifact {item.artifact_id} contains non-holdout Cases")
            if item.family_ids and set(item.family_ids) - set(holdout_family_ids):
                raise ValueError(f"T16 artifact {item.artifact_id} contains non-holdout families")


def _usage_for_payload(
    payload: Mapping[str, Any], evaluation: EvaluationReport
) -> Mapping[str, Any]:
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        return usage
    values: dict[str, int | float] = {field: 0 for field in _RESOURCE_FIELDS if field != "cost_usd"}
    costs: list[float] = []
    for outcome in evaluation.outcomes:
        for field in values:
            values[field] += _number(outcome.usage.get(field, 0))
        if outcome.usage.get("cost_usd") is not None:
            costs.append(float(_number(outcome.usage["cost_usd"])))
    values["cost_usd"] = sum(costs) if costs else None  # type: ignore[assignment]
    return values


def build_release(
    dataset: CaseDataset,
    *,
    config: FrozenExperimentConfig,
    baseline_reports: BaselineReport | Sequence[BaselineReport],
    treatment_reports: TreatmentComparisonReport | Sequence[TreatmentComparisonReport],
    signoff: HumanSignoff,
    signing_key: str,
    artifacts: Sequence[ArtifactReference] | None = None,
    limitations: Sequence[str] | None = None,
) -> ReleaseReport:
    return ReleaseBuilder(config).build(
        dataset,
        baseline_reports=baseline_reports,
        treatment_reports=treatment_reports,
        signoff=signoff,
        signing_key=signing_key,
        artifacts=artifacts,
        limitations=limitations,
    )


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="rulecourt-release")
    subparsers = parser.add_subparsers(dest="command", required=True)
    report = subparsers.add_parser("report", help="view a signed T16 release artifact")
    report.add_argument("release", type=Path)
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--output", type=Path)
    validate = subparsers.add_parser("validate", help="validate a frozen T16 config")
    validate.add_argument("config", type=Path)
    validate.add_argument("dataset", type=Path)
    validate.add_argument("--signoff", type=Path)
    args = parser.parse_args(argv)
    if args.command == "report":
        release = ReleaseReport.from_json(args.release)
        release.validate_integrity(signing_key=os.getenv(_SIGNATURE_ENV))
        output = (
            release.to_markdown()
            if args.format == "markdown"
            else json.dumps(release.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
    else:
        config = FrozenExperimentConfig.model_validate_json(args.config.read_text(encoding="utf-8"))
        dataset = CaseDataset.from_json(args.dataset)
        config.validate_for_formal(dataset)
        if args.signoff:
            signoff = HumanSignoff.from_json(args.signoff)
            dataset.validate_for_scoring(
                require_split=True,
                require_coverage=True,
                signoff=signoff,
                signing_key=os.getenv("RULECOURT_GOLDEN_SIGNOFF_KEY"),
            )
        output = (
            json.dumps(
                {
                    "release_id": config.release_id,
                    "config_hash": config.config_hash,
                    "dataset_version": dataset.dataset_version,
                    "status": "frozen",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n"
        )
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


__all__ = [
    "BASELINE_GROUPS",
    "GROUPS",
    "TREATMENT_GROUPS",
    "ArtifactReference",
    "FrozenExperimentConfig",
    "PairedCaseSummary",
    "ReleaseBuilder",
    "ReleaseConclusion",
    "ReleaseGroupSummary",
    "ReleasePairSummary",
    "ReleaseReport",
    "build_release",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
