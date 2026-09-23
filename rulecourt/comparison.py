"""T14 paired comparison of Dynamic Agent and Fixed Workflow."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sys
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .budget import InvestigationBudget
from .evaluation import (
    CaseAdapter,
    CaseDataset,
    EvaluationCase,
    EvaluationReport,
    EvaluationRunner,
    HumanSignoff,
    Metric,
    ReplayResult,
    ScoreView,
    score_results,
)

StrategyName = Literal["dynamic_agent", "fixed_workflow"]
DifferenceClass = Literal[
    "none", "extraction", "investigation", "budget", "finalization_gate", "adapter_failure"
]
_RESOURCE_FIELDS = (
    "iterations",
    "tool_calls",
    "tool_failures",
    "provider_calls",
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "latency_ms",
    "cost_usd",
)
_ADAPTER_FAILURES = {"ADAPTER_ERROR", "FIXED_WORKFLOW_UNAVAILABLE"}
_BUDGET_FAILURES = {
    "BUDGET_EXHAUSTED",
    "FACT_REQUEST_BUDGET_EXHAUSTED",
    "INVESTIGATION_TIMEOUT",
    "TIME_LIMIT_EXCEEDED",
}
_GATE_FAILURES = {"VERIFICATION_NOT_SATISFIED", "EVIDENCE_UNAVAILABLE", "UNSUPPORTED_ACTION"}


class TreatmentAdapter(CaseAdapter, Protocol):
    """Adapter seam required by a T14 paired comparison."""

    def configure_budget(self, budget: InvestigationBudget) -> None: ...

    def comparison_metadata(self) -> Mapping[str, Any]: ...

    def get_case(self, case_id: str) -> Mapping[str, Any]: ...

    def get_events(self, case_id: str) -> Sequence[Mapping[str, Any]]: ...


class _ArtifactAdapter:
    """Validated no-op adapter used only to build reports from exported artifacts."""

    def __init__(self, strategy: StrategyName, config: TreatmentConfig):
        self.strategy = strategy
        self.config = config
        self.metadata = {
            **config.shared_contract,
            "strategy": strategy,
            "strategy_version": (
                config.dynamic_strategy_version
                if strategy == "dynamic_agent"
                else config.fixed_workflow_version
            ),
            "fixed_route_source": config.fixed_route_source,
            "fixed_template_version": config.fixed_template_version,
            "fixed_workflow_llm_routing": config.fixed_workflow_llm_routing,
        }

    def configure_budget(self, budget: InvestigationBudget) -> None:
        self.metadata.update(
            {
                "budget": budget.to_dict(),
                "budget_enforced": True,
            }
        )

    def comparison_metadata(self) -> Mapping[str, Any]:
        return self.metadata

    def create_case(self) -> str:
        raise RuntimeError("artifact adapter cannot execute a live case")

    def submit_message(self, case_id: str, text: str) -> Mapping[str, Any]:
        raise RuntimeError("artifact adapter cannot execute a live case")

    def get_case(self, case_id: str) -> Mapping[str, Any]:
        raise RuntimeError("artifact adapter cannot inspect a live case")

    def get_events(self, case_id: str) -> Sequence[Mapping[str, Any]]:
        raise RuntimeError("artifact adapter cannot inspect a live case")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _hash(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _number(value: Any) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return 0
    return max(0, value)


def _provider_model_identity(metadata: Mapping[str, Any]) -> str:
    provider = str(metadata.get("provider") or "").strip()
    model = str(metadata.get("model") or "").strip()
    return f"{provider}/{model}" if provider and model else ""


class TreatmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "t14-treatment-config-v1"
    ruleset_id: str = "root-law-2025-10"
    ruleset_version: str = "2025-10"
    state_extractor_version: str = "m0-state-extractor-v1"
    domain_adapter_version: str = "root-adapter-v1"
    controller_version: str = "controller-v1"
    visible_projection_version: str = "public-projection-v1"
    dynamic_strategy_version: str = "dynamic-agent-v1"
    fixed_workflow_version: str = "fixed-workflow-v1"
    fixed_template_version: str = "m0-fixed-template-v1"
    fixed_route_source: str = "hand_authored"
    fixed_workflow_llm_routing: bool = False
    planning_cost_included: bool = True
    run_kind: Literal["development_trial", "formal"] = "development_trial"
    budget: dict[str, Any] = Field(default_factory=lambda: InvestigationBudget().to_dict())
    max_clarification_rounds: int = Field(default=8, gt=0)
    max_fact_requests: int = Field(default=8, gt=0)
    model: str | None = None
    provider: str | None = None
    input_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator(
        "schema_version",
        "ruleset_id",
        "ruleset_version",
        "state_extractor_version",
        "domain_adapter_version",
        "controller_version",
        "visible_projection_version",
        "dynamic_strategy_version",
        "fixed_workflow_version",
        "fixed_template_version",
    )
    @classmethod
    def nonempty_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("T14 configuration versions must not be empty")
        return value

    @model_validator(mode="before")
    @classmethod
    def normalize_budget(cls, values: Any) -> Any:
        if isinstance(values, Mapping):
            values = dict(values)
            if isinstance(values.get("budget"), InvestigationBudget):
                values["budget"] = values["budget"].to_dict()
        return values

    @model_validator(mode="after")
    def validate_configuration(self) -> TreatmentConfig:
        route = self.fixed_route_source.strip().casefold().replace("-", "_")
        if route in {
            "coverage_table",
            "generated_from_coverage_table",
            "generated",
            "coverage_obligations",
        } or ("coverage" in route and "table" in route):
            raise ValueError("fixed route cannot be generated from a coverage table")
        if self.fixed_workflow_llm_routing:
            raise ValueError("fixed workflow must not use LLM routing")
        if not self.planning_cost_included:
            raise ValueError("dynamic planning cost must be included")
        budget = InvestigationBudget.from_mapping(self.budget)
        if self.run_kind == "formal" and budget.configuration_status != "frozen":
            raise ValueError("formal T14 comparison requires a frozen budget")
        object.__setattr__(self, "budget", budget.to_dict())
        return self

    @property
    def config_hash(self) -> str:
        return _hash(self.model_dump(mode="json"))

    @property
    def shared_contract(self) -> dict[str, Any]:
        return {
            "ruleset_id": self.ruleset_id,
            "ruleset_version": self.ruleset_version,
            "state_extractor_version": self.state_extractor_version,
            "domain_adapter_version": self.domain_adapter_version,
            "controller_version": self.controller_version,
            "visible_projection_version": self.visible_projection_version,
            "budget": self.budget,
            "max_clarification_rounds": self.max_clarification_rounds,
            "max_fact_requests": self.max_fact_requests,
            "model": self.model,
            "provider": self.provider,
        }


class AuditFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    location: str = ""
    detail: str = ""


class StrategyAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: StrategyName
    observed_strategy: str | None = None
    observed_route: str | None = None
    observation_available: bool = False
    valid: bool = True
    context_hash: str = ""
    context: list[Any] = Field(default_factory=list)
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    log_projection: list[dict[str, Any]] = Field(default_factory=list)
    tool_call_count: int = Field(default=0, ge=0)
    findings: list[AuditFinding] = Field(default_factory=list)


class PairAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    valid: bool = True
    shared_input_hash: str
    dynamic: StrategyAudit
    fixed: StrategyAudit
    findings: list[AuditFinding] = Field(default_factory=list)


class PairedReplayResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    family_id: str
    category: str
    dynamic: ReplayResult
    fixed: ReplayResult
    audit: PairAudit
    valid_for_comparison: bool = True
    difference_class: DifferenceClass = "none"
    difference_reason: str | None = None


class PairedMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dynamic: Metric
    fixed: Metric
    numerator_delta: int
    denominator_delta: int
    value_delta: float | None


class PairedScoreView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dynamic: ScoreView
    fixed: ScoreView
    metrics: dict[str, PairedMetric]


class ResourceDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    valid_for_comparison: bool = True
    delta: dict[str, int | float | None] = Field(default_factory=dict)


class StrategyRunReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: StrategyName
    config: dict[str, Any]
    evaluation: EvaluationReport
    usage: dict[str, Any] = Field(default_factory=dict)
    audit: dict[str, Any] = Field(default_factory=dict)
    results: list[dict[str, Any]] = Field(default_factory=list)
    observations: dict[str, dict[str, Any]] = Field(default_factory=dict)


class DeterminismMismatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    view: Literal["initial", "complete"]
    providers: list[str]
    expected: Any = None
    actual: dict[str, Any] = Field(default_factory=dict)


class DeterminismReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    providers: list[str] = Field(default_factory=list)
    compared_case_ids: list[str] = Field(default_factory=list)
    strategy: StrategyName | None = None
    reason: str | None = None
    provider_identities: dict[str, str] = Field(default_factory=dict)
    mismatches: list[DeterminismMismatch] = Field(default_factory=list)
    ignored_fields: list[str] = Field(
        default_factory=lambda: [
            "explanation",
            "investigation",
            "usage",
            "tool order",
            "run_id",
            "timestamps",
        ]
    )


class TreatmentComparisonReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "t14-treatment-comparison-v1"
    dataset_version: str
    ruleset_id: str
    ruleset_version: str
    run_kind: Literal["development_trial", "formal"]
    config: dict[str, Any]
    runs: dict[str, StrategyRunReport]
    pairs: list[PairedReplayResult] = Field(default_factory=list)
    paired_case_ids: list[str] = Field(default_factory=list)
    excluded_case_ids: dict[str, list[str]] = Field(default_factory=dict)
    excluded_counts: dict[str, int] = Field(default_factory=dict)
    initial: PairedScoreView
    complete: PairedScoreView
    resource_deltas: list[ResourceDelta] = Field(default_factory=list)
    difference_counts: dict[str, int] = Field(default_factory=dict)
    determinism: list[DeterminismReport] = Field(default_factory=list)
    attribution_note: str = (
        "Only Dynamic Agent versus Fixed Workflow is an Agent-increment comparison; "
        "external baseline differences must not be attributed to planning."
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    @classmethod
    def from_json(cls, path: str | Path) -> TreatmentComparisonReport:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        lines = [
            f"# RuleCourt T14 treatment comparison ({self.dataset_version})",
            "",
            f"Ruleset: {self.ruleset_id} / {self.ruleset_version}",
            f"Run kind: {self.run_kind}",
            f"Valid paired cases: {len(self.paired_case_ids)}",
            f"Excluded invalid-audit cases: {len(self.excluded_case_ids.get('invalid_audit', []))}",
            "",
            "| View | Metric | Dynamic Agent | Fixed Workflow | Delta |",
            "| --- | --- | ---: | ---: | ---: |",
        ]
        for view_name, view in (("Initial", self.initial), ("Complete", self.complete)):
            for name, metric in view.metrics.items():
                lines.append(
                    f"| {view_name} | {name} | {metric.dynamic.display()} | "
                    f"{metric.fixed.display()} | {_display_delta(metric.value_delta)} |"
                )
        lines.extend(
            [
                "",
                "| Strategy | Total tokens | Cost | P50 latency (ms) | P95 latency (ms) |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for strategy in ("dynamic_agent", "fixed_workflow"):
            usage = self.runs[strategy].usage
            lines.append(
                f"| {strategy} | {usage.get('total_tokens', 0)} | "
                f"{_display_cost(usage.get('cost_usd'))} | "
                f"{_display_optional(usage.get('latency_p50_ms'))} | "
                f"{_display_optional(usage.get('latency_p95_ms'))} |"
            )
        lines.extend(["", self.attribution_note, ""])
        return "\n".join(lines)


class VisibilityAuditor:
    _coverage_fragments = ("coverageobligation", "coveragetable")
    _diagnostic_fragments = (
        "diagnosticroute",
        "recommendedroute",
        "recommendedquery",
        "nexttool",
        "nextaction",
        "plannerhint",
        "unmetobligation",
        "missingruleid",
        "verificationroute",
    )

    @classmethod
    def audit(
        cls,
        strategy: StrategyName,
        *,
        initial_input: str,
        initial_result: Mapping[str, Any],
        complete_result: Mapping[str, Any],
        context: Sequence[Any] | None = None,
        tool_results: Sequence[Mapping[str, Any]] | None = None,
        log_projection: Sequence[Mapping[str, Any]] | None = None,
        observed_strategy: str | None = None,
        observed_route: str | None = None,
        usage: Mapping[str, Any] | None = None,
        budget: InvestigationBudget | None = None,
    ) -> StrategyAudit:
        context_items = list(context) if context is not None else []
        tools = [
            dict(item)
            for item in (tool_results or [])
            if isinstance(item, Mapping)
        ]
        logs = [
            dict(item)
            for item in (log_projection or [])
            if isinstance(item, Mapping)
        ]
        findings: list[AuditFinding] = []
        for name, value in (
            ("context", context),
            ("tool_results", tool_results),
            ("log_projection", log_projection),
        ):
            if value is None:
                findings.append(
                    AuditFinding(
                        code="missing_audit_projection",
                        location=name,
                        detail="the replay did not provide the public audit projection",
                    )
                )
        if not context_items:
            findings.append(
                AuditFinding(
                    code="empty_context_projection",
                    location="context",
                    detail="the replay did not expose the initial public context",
                )
            )
        if observed_strategy is None:
            findings.append(
                AuditFinding(
                    code="missing_strategy_observation",
                    location="log_projection",
                    detail="the replay did not expose the authoritative executed strategy",
                )
            )
        elif observed_strategy != strategy:
            findings.append(
                AuditFinding(
                    code="strategy_mismatch",
                    location="log_projection",
                    detail=f"observed strategy {observed_strategy!r} does not match {strategy!r}",
                )
            )
        expected_route = strategy
        if observed_route is None:
            findings.append(
                AuditFinding(
                    code="missing_route_observation",
                    location="log_projection",
                    detail="the replay did not expose the executed route",
                )
            )
        elif observed_route != expected_route:
            findings.append(
                AuditFinding(
                    code="route_mismatch",
                    location="log_projection",
                    detail=f"observed route {observed_route!r} does not match {expected_route!r}",
                )
            )
        if budget is not None and usage is not None:
            limits = {
                "iterations": budget.max_iterations,
                "tool_calls": budget.max_tool_calls,
                "total_tokens": budget.max_total_tokens,
                "latency_ms": budget.max_duration_seconds * 1000,
            }
            for field, limit in limits.items():
                if _number(usage.get(field, 0)) > limit:
                    findings.append(
                        AuditFinding(
                            code="budget_overrun",
                            location=f"usage.{field}",
                            detail=f"reported usage exceeds the configured {field} budget",
                        )
                    )
        for location, value in (
            ("context", context_items),
            ("initial_result", dict(initial_result)),
            ("complete_result", dict(complete_result)),
            ("tool_results", tools),
            ("log_projection", logs),
        ):
            cls._scan(value, location, findings)
        if context_items and _message_text(context_items[0]) != initial_input:
            findings.append(
                AuditFinding(
                    code="input_mismatch",
                    location="context[0]",
                    detail="the first observed context item differs from the evaluator input",
                )
            )
        findings = _unique_findings(findings)
        return StrategyAudit(
            strategy=strategy,
            observed_strategy=observed_strategy,
            observed_route=observed_route,
            observation_available=(
                context is not None
                and tool_results is not None
                and log_projection is not None
                and bool(context_items)
                and observed_strategy is not None
                and observed_route is not None
            ),
            valid=not findings,
            context_hash=_hash(context_items),
            context=context_items,
            tool_results=tools,
            log_projection=logs,
            tool_call_count=len(tools),
            findings=findings,
        )

    @classmethod
    def _scan(cls, value: Any, location: str, findings: list[AuditFinding]) -> None:
        if isinstance(value, Mapping):
            for raw_key, item in value.items():
                key = re.sub(r"[^a-z0-9]+", "", str(raw_key).casefold())
                if any(part in key for part in cls._coverage_fragments):
                    findings.append(
                        AuditFinding(
                            code="coverage_table_leak",
                            location=f"{location}.{raw_key}",
                            detail="private coverage obligations reached a visible projection",
                        )
                    )
                if any(part in key for part in cls._diagnostic_fragments):
                    findings.append(
                        AuditFinding(
                            code="diagnostic_route_leak",
                            location=f"{location}.{raw_key}",
                            detail="private diagnostic route reached a visible projection",
                        )
                    )
                cls._scan(item, f"{location}.{raw_key}", findings)
            return
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            for index, item in enumerate(value):
                cls._scan(item, f"{location}[{index}]", findings)
            return
        if isinstance(value, str):
            text = re.sub(r"\s+", " ", value.casefold())
            if re.search(r"coverage\s+(?:table|obligation|obligations)", text):
                findings.append(
                    AuditFinding(
                        code="coverage_table_leak",
                        location=location,
                        detail="private coverage wording reached a visible projection",
                    )
                )
            if re.search(r"(?:recommended|next|diagnostic)\s+(?:tool|action|query|route)", text):
                findings.append(
                    AuditFinding(
                        code="diagnostic_route_leak",
                        location=location,
                        detail="private route wording reached a visible projection",
                    )
                )


class TreatmentComparisonRunner:
    def __init__(
        self,
        adapters: Mapping[str, TreatmentAdapter],
        *,
        config: TreatmentConfig | Mapping[str, Any] | None = None,
        auditor: type[VisibilityAuditor] = VisibilityAuditor,
        allow_model_provider_override: bool = False,
    ):
        self.config = config if isinstance(config, TreatmentConfig) else TreatmentConfig.model_validate(dict(config or {}))
        self.adapters = self._normalize_adapters(adapters)
        self.auditor = auditor
        self.allow_model_provider_override = allow_model_provider_override
        self.budget = InvestigationBudget.from_mapping(self.config.budget)
        self._configure_adapters()
        self._validate_adapters()

    @classmethod
    def from_artifacts(
        cls,
        config: TreatmentConfig | Mapping[str, Any] | None = None,
        *,
        auditor: type[VisibilityAuditor] = VisibilityAuditor,
    ) -> TreatmentComparisonRunner:
        treatment = (
            config
            if isinstance(config, TreatmentConfig)
            else TreatmentConfig.model_validate(dict(config or {}))
        )
        return cls(
            {
                "dynamic_agent": _ArtifactAdapter("dynamic_agent", treatment),
                "fixed_workflow": _ArtifactAdapter("fixed_workflow", treatment),
            },
            config=treatment,
            auditor=auditor,
        )

    @staticmethod
    def _normalize_adapters(adapters: Mapping[str, TreatmentAdapter]) -> dict[StrategyName, TreatmentAdapter]:
        aliases = {"dynamic": "dynamic_agent", "agent": "dynamic_agent", "fixed": "fixed_workflow", "workflow": "fixed_workflow"}
        normalized = {aliases.get(name, name): adapter for name, adapter in adapters.items()}
        if set(normalized) != {"dynamic_agent", "fixed_workflow"}:
            raise ValueError("T14 comparison requires dynamic_agent and fixed_workflow adapters")
        return {
            "dynamic_agent": normalized["dynamic_agent"],
            "fixed_workflow": normalized["fixed_workflow"],
        }

    @staticmethod
    def _metadata(adapter: TreatmentAdapter) -> dict[str, Any]:
        method = getattr(adapter, "comparison_metadata", None)
        value = method() if callable(method) else getattr(adapter, "metadata", {})
        return dict(value) if isinstance(value, Mapping) else {}

    def _configure_adapters(self) -> None:
        for strategy, adapter in self.adapters.items():
            configure = getattr(adapter, "configure_budget", None)
            if not callable(configure):
                raise TypeError(
                    f"{strategy} adapter must expose configure_budget for a shared T14 budget"
                )
            try:
                configure(self.budget)
            except Exception as exc:
                raise ValueError(f"{strategy} adapter rejected the shared T14 budget") from exc

    def _validate_adapters(self) -> None:
        expected_contract = self.config.shared_contract
        for strategy, adapter in self.adapters.items():
            metadata = self._metadata(adapter)
            required = set(expected_contract) | {"strategy", "budget_enforced"}
            if strategy == "fixed_workflow":
                required |= {
                    "fixed_route_source",
                    "fixed_template_version",
                    "fixed_workflow_llm_routing",
                }
            missing = sorted(name for name in required if name not in metadata)
            if missing:
                raise ValueError(
                    f"{strategy} adapter metadata is missing the shared T14 contract: "
                    + ", ".join(missing)
                )
            if metadata["strategy"] != strategy:
                raise ValueError(f"adapter strategy metadata does not match {strategy}")
            if metadata["budget_enforced"] is not True:
                raise ValueError(f"{strategy} adapter must enforce the shared T14 budget")
            for name, expected in expected_contract.items():
                if (
                    self.allow_model_provider_override
                    and name in {"model", "provider"}
                ):
                    continue
                if _json(metadata[name]) != _json(expected):
                    raise ValueError(f"{strategy} adapter does not share {name}")
            if self.allow_model_provider_override:
                for name in ("model", "provider"):
                    if (
                        _json(metadata[name])
                        != _json(self._metadata(self.adapters["dynamic_agent"])[name])
                    ):
                        raise ValueError(
                            f"determinism adapters must share {name} within a provider"
                        )
            expected_version = (
                self.config.dynamic_strategy_version
                if strategy == "dynamic_agent"
                else self.config.fixed_workflow_version
            )
            if metadata.get("strategy_version") != expected_version:
                raise ValueError(f"{strategy} adapter strategy version differs")
            if strategy == "fixed_workflow":
                if metadata["fixed_route_source"] != self.config.fixed_route_source:
                    raise ValueError("fixed workflow route source differs")
                if metadata["fixed_template_version"] != self.config.fixed_template_version:
                    raise ValueError("fixed workflow template version differs")
                if metadata["fixed_workflow_llm_routing"] is not False:
                    raise ValueError("fixed workflow adapter must not use LLM routing")

    def run_case(self, case: EvaluationCase) -> PairedReplayResult:
        if case.ruleset_id != self.config.ruleset_id:
            raise ValueError("case and treatment ruleset versions differ")
        results: dict[StrategyName, ReplayResult] = {}
        audits: dict[StrategyName, StrategyAudit] = {}
        for strategy in ("dynamic_agent", "fixed_workflow"):
            adapter = self.adapters[strategy]
            try:
                result = EvaluationRunner(
                    adapter,
                    max_clarification_rounds=self.config.max_clarification_rounds,
                    max_fact_requests=self.config.max_fact_requests,
                ).run_case(case)
            except Exception as exc:  # noqa: BLE001 - preserve both arm outcomes
                result = _adapter_failure(case, type(exc).__name__, str(exc))
            results[strategy] = result
            snapshot = _snapshot(adapter, result)
            audits[strategy] = self.auditor.audit(
                strategy,
                initial_input=case.initial_input,
                initial_result=result.initial_result,
                complete_result=result.complete_result,
                context=snapshot["context"],
                tool_results=snapshot["tools"],
                log_projection=snapshot["logs"],
                observed_strategy=snapshot["strategy"],
                observed_route=snapshot["route"],
                usage=result.usage,
                budget=self.budget,
            )
        pair_audit = _pair_audit(case.initial_input, audits["dynamic_agent"], audits["fixed_workflow"])
        difference, reason = _classify_difference(results["dynamic_agent"], results["fixed_workflow"])
        return PairedReplayResult(
            case_id=case.id,
            family_id=case.family_id,
            category=case.category,
            dynamic=results["dynamic_agent"],
            fixed=results["fixed_workflow"],
            audit=pair_audit,
            valid_for_comparison=pair_audit.valid,
            difference_class=difference,
            difference_reason=reason,
        )

    def run_dataset(
        self,
        dataset: CaseDataset,
        *,
        verified_only: bool = False,
        formal: bool = False,
        signoff: Any | None = None,
        signing_key: str | None = None,
        determinism_adapters: Mapping[str, Mapping[str, TreatmentAdapter]] | None = None,
    ) -> TreatmentComparisonReport:
        if formal and self.config.run_kind != "formal":
            raise ValueError("formal=True requires a formal T14 configuration")
        formal_run = formal or self.config.run_kind == "formal"
        if self.config.run_kind == "development_trial" and any(
            case.split == "holdout" for case in dataset.cases
        ):
            raise ValueError("T14 development trials do not permit holdout cases")
        if formal_run:
            cases = dataset.validate_for_scoring(
                require_split=True,
                require_coverage=True,
                signoff=signoff,
                signing_key=signing_key,
            )
            if determinism_adapters is None or len(determinism_adapters) < 2:
                raise ValueError(
                    "formal T14 comparison requires at least two model/provider adapter sets"
                )
        else:
            dataset.validate_for_scoring(require_split=False)
            cases = dataset.verified_cases if verified_only else dataset.cases
        pairs = [self.run_case(case) for case in cases]
        determinism = self._run_determinism(cases, determinism_adapters)
        return self._report(
            dataset,
            cases,
            pairs,
            formal=formal_run,
            signoff=signoff,
            signing_key=signing_key,
            determinism=determinism,
        )

    def _run_determinism(
        self,
        cases: Sequence[EvaluationCase],
        provider_adapters: Mapping[str, Mapping[str, TreatmentAdapter]] | None,
    ) -> list[DeterminismReport]:
        if not provider_adapters:
            return [
                DeterminismReport(
                    strategy=strategy,
                    passed=False,
                    reason="not_run",
                )
                for strategy in ("dynamic_agent", "fixed_workflow")
            ]
        if len(provider_adapters) < 2:
            return [
                DeterminismReport(
                    strategy=strategy,
                    passed=False,
                    providers=[str(name) for name in provider_adapters],
                    reason="at_least_two_provider_models_required",
                )
                for strategy in ("dynamic_agent", "fixed_workflow")
            ]
        results: dict[StrategyName, dict[str, list[ReplayResult]]] = {
            "dynamic_agent": {},
            "fixed_workflow": {},
        }
        provider_identities: dict[str, str] = {}
        for provider_name, adapters in provider_adapters.items():
            provider_runner = TreatmentComparisonRunner(
                adapters,
                config=self.config,
                auditor=self.auditor,
                allow_model_provider_override=True,
            )
            identity = _provider_model_identity(
                provider_runner._metadata(provider_runner.adapters["dynamic_agent"])
            )
            if identity:
                provider_identities[str(provider_name)] = identity
            pair_results = [provider_runner.run_case(case) for case in cases]
            results["dynamic_agent"][str(provider_name)] = [
                pair.dynamic for pair in pair_results
            ]
            results["fixed_workflow"][str(provider_name)] = [
                pair.fixed for pair in pair_results
            ]
        identities = (
            provider_identities
            if len(provider_identities) == len(provider_adapters)
            else None
        )
        return [
            check_determinism(
                results[strategy],
                strategy=strategy,
                provider_identities=identities,
            )
            for strategy in ("dynamic_agent", "fixed_workflow")
        ]

    def build_report(
        self,
        dataset: CaseDataset,
        cases: Sequence[EvaluationCase],
        pairs: Sequence[PairedReplayResult],
        *,
        formal: bool = False,
        signoff: HumanSignoff | None = None,
        signing_key: str | None = None,
        determinism: Sequence[DeterminismReport] | None = None,
    ) -> TreatmentComparisonReport:
        """Build a report from already replayed, audited pairs."""

        return self._report(
            dataset,
            cases,
            pairs,
            formal=formal,
            signoff=signoff,
            signing_key=signing_key,
            determinism=determinism,
        )

    def _report(
        self,
        dataset: CaseDataset,
        cases: Sequence[EvaluationCase],
        pairs: Sequence[PairedReplayResult],
        *,
        formal: bool = False,
        signoff: HumanSignoff | None = None,
        signing_key: str | None = None,
        determinism: Sequence[DeterminismReport] | None = None,
    ) -> TreatmentComparisonReport:
        valid = [pair for pair in pairs if pair.valid_for_comparison]
        if formal and len(valid) != len(pairs):
            raise ValueError("formal T14 comparison cannot score invalid audit pairs")
        if formal and (
            not determinism
            or any(
                not item.passed
                or len(item.providers) < 2
                or len(item.provider_identities) != len(item.providers)
                or len(set(item.provider_identities.values())) < 2
                for item in determinism
            )
        ):
            raise ValueError(
                "formal T14 comparison requires passing two-provider determinism evidence"
            )
        valid_ids = {pair.case_id for pair in valid}
        score_dataset = (
            dataset
            if formal
            else CaseDataset(
                dataset_version=dataset.dataset_version,
                cases=[
                    case.model_copy(deep=True)
                    for case in cases
                    if case.id in valid_ids
                ],
            )
        )
        dynamic_results = [pair.dynamic for pair in valid]
        fixed_results = [pair.fixed for pair in valid]
        dynamic_usage = _usage(dynamic_results, self.config, "dynamic_agent")
        fixed_usage = _usage(fixed_results, self.config, "fixed_workflow")
        if formal and not dynamic_usage["planning_usage_available"]:
            raise ValueError(
                "formal T14 comparison requires explicit Dynamic Agent planning usage"
            )
        dynamic_eval = score_results(
            score_dataset,
            dynamic_results,
            formal=formal,
            signoff=signoff,
            signing_key=signing_key,
        )
        fixed_eval = score_results(
            score_dataset,
            fixed_results,
            formal=formal,
            signoff=signoff,
            signing_key=signing_key,
        )
        configs = {
            "dynamic_agent": {
                **self.config.model_dump(mode="json"),
                "strategy": "dynamic_agent",
                "strategy_version": self.config.dynamic_strategy_version,
                "budget_enforced": True,
                "config_hash": self.config.config_hash,
            },
            "fixed_workflow": {
                **self.config.model_dump(mode="json"),
                "strategy": "fixed_workflow",
                "strategy_version": self.config.fixed_workflow_version,
                "template_version": self.config.fixed_template_version,
                "budget_enforced": True,
                "config_hash": self.config.config_hash,
            },
        }
        runs = {
            name: StrategyRunReport(
                strategy=cast(StrategyName, name),
                config=configs[name],
                evaluation=evaluation,
                usage=dynamic_usage if name == "dynamic_agent" else fixed_usage,
                audit=_audit(
                    [
                        pair.audit.dynamic if name == "dynamic_agent" else pair.audit.fixed
                        for pair in pairs
                    ]
                ),
                results=[
                    (pair.dynamic if name == "dynamic_agent" else pair.fixed).model_dump(
                        mode="json"
                    )
                    for pair in pairs
                ],
                observations={
                    pair.case_id: (
                        pair.audit.dynamic if name == "dynamic_agent" else pair.audit.fixed
                    ).model_dump(
                        mode="json",
                        include={
                            "context",
                            "tool_results",
                            "log_projection",
                            "observed_strategy",
                            "observed_route",
                        },
                    )
                    for pair in pairs
                },
            )
            for name, evaluation in (("dynamic_agent", dynamic_eval), ("fixed_workflow", fixed_eval))
        }
        excluded = {
            "invalid_audit": sorted(pair.case_id for pair in pairs if not pair.valid_for_comparison),
            "draft": sorted(case.id for case in cases if case.review.status == "draft"),
            "disputed": sorted(case.id for case in cases if case.review.status == "disputed"),
        }
        excluded = {key: value for key, value in excluded.items() if value}
        counts: dict[str, int] = {}
        for pair in pairs:
            counts[pair.difference_class] = counts.get(pair.difference_class, 0) + 1
        return TreatmentComparisonReport(
            dataset_version=dataset.dataset_version,
            ruleset_id=self.config.ruleset_id,
            ruleset_version=self.config.ruleset_version,
            run_kind=self.config.run_kind,
            config={**self.config.model_dump(mode="json"), "config_hash": self.config.config_hash},
            runs=runs,
            pairs=list(pairs),
            paired_case_ids=sorted(valid_ids),
            excluded_case_ids=excluded,
            excluded_counts={key: len(value) for key, value in excluded.items()},
            initial=_paired_view(dynamic_eval.initial, fixed_eval.initial),
            complete=_paired_view(dynamic_eval.complete, fixed_eval.complete),
            resource_deltas=[_delta(pair) for pair in pairs],
            difference_counts=counts,
            determinism=list(determinism or []),
        )

def _snapshot(adapter: TreatmentAdapter, result: ReplayResult) -> dict[str, Any]:
    runtime_id = (
        getattr(adapter, "last_case_id", None)
        or getattr(adapter, "case_id", None)
        or result.case_id
    )
    case_view: Mapping[str, Any] | None = None
    events: list[Mapping[str, Any]] | None = None
    case_getter = getattr(adapter, "get_case", None)
    if callable(case_getter):
        try:
            value = case_getter(str(runtime_id))
            if isinstance(value, Mapping):
                case_view = value
        except Exception:  # noqa: BLE001 - retain missing audit evidence
            case_view = None
    event_getter = getattr(adapter, "get_events", None)
    if callable(event_getter):
        try:
            value = event_getter(str(runtime_id))
            if isinstance(value, Sequence) and not isinstance(
                value, (str, bytes, bytearray)
            ):
                events = [item for item in value if isinstance(item, Mapping)]
        except Exception:  # noqa: BLE001 - retain missing audit evidence
            events = None

    context: list[Any] | None = None
    if case_view is not None:
        value = case_view.get("messages")
        if isinstance(value, Sequence) and not isinstance(
            value, (str, bytes, bytearray)
        ):
            context = list(value)
    tools: list[dict[str, Any]] | None = None
    logs: list[dict[str, Any]] | None = None
    observed_strategy: str | None = None
    if events is not None:
        logs = [dict(item) for item in events]
        tools = [item for item in logs if item.get("type") == "tool_call"]
        for event in reversed(logs):
            if event.get("type") == "investigation_started" and event.get("strategy"):
                observed_strategy = str(event["strategy"])
                break
    return {
        "context": context,
        "tools": tools,
        "logs": logs,
        "strategy": observed_strategy,
        "route": (
            "fixed_workflow"
            if any(item.get("type") == "fixed_workflow_started" for item in logs or [])
            else observed_strategy
        ),
    }


def _message_text(value: Any) -> str:
    return str(value.get("content", value.get("text", ""))) if isinstance(value, Mapping) else str(value)


def _unique_findings(findings: Sequence[AuditFinding]) -> list[AuditFinding]:
    result: list[AuditFinding] = []
    seen: set[tuple[str, str, str]] = set()
    for item in findings:
        key = (item.code, item.location, item.detail)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _pair_audit(initial_input: str, dynamic: StrategyAudit, fixed: StrategyAudit) -> PairAudit:
    findings = [*dynamic.findings, *fixed.findings]
    if dynamic.context and fixed.context and _message_text(dynamic.context[0]) != _message_text(fixed.context[0]):
        findings.append(AuditFinding(code="shared_input_mismatch", location="pair.context", detail="strategies observed different initial context"))
    findings = _unique_findings(findings)
    return PairAudit(
        valid=dynamic.valid and fixed.valid and not findings,
        shared_input_hash=_hash(initial_input),
        dynamic=dynamic,
        fixed=fixed,
        findings=findings,
    )


def _adapter_failure(case: EvaluationCase, error_type: str, detail: str) -> ReplayResult:
    response = {
        "case_id": case.id,
        "status": "UNRESOLVED",
        "reason": "ADAPTER_ERROR",
        "failure_reason": "ADAPTER_ERROR",
        "details": {"error_type": error_type, "detail": detail},
    }
    return ReplayResult(
        case_id=case.id,
        family_id=case.family_id,
        category=case.category,
        initial_result=response,
        complete_result=response,
        initial_failure_reason="ADAPTER_ERROR",
        complete_failure_reason="ADAPTER_ERROR",
        failure_reason="ADAPTER_ERROR",
        stop_reason="adapter_error",
    )


def _projection(result: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("status", "reason", "failure_reason", "missing_fields", "evidence", "decision", "verification", "checks", "state_update")
    return {key: result.get(key) for key in keys if key in result}


def _classify_difference(dynamic: ReplayResult, fixed: ReplayResult) -> tuple[DifferenceClass, str | None]:
    same = all(_projection(getattr(dynamic, view)) == _projection(getattr(fixed, view)) for view in ("initial_result", "complete_result"))
    if same:
        if (
            dynamic.failure_reason in _ADAPTER_FAILURES
            or fixed.failure_reason in _ADAPTER_FAILURES
        ):
            return "adapter_failure", "one strategy adapter reported an unavailable route"
        return "none", None
    responses = [dynamic.initial_result, dynamic.complete_result, fixed.initial_result, fixed.complete_result]
    reasons = {str(item.get("failure_reason")) for item in responses if item.get("failure_reason")}
    reasons.update(str(item.get("reason")) for item in responses if item.get("reason"))
    if reasons & _ADAPTER_FAILURES:
        return "adapter_failure", "one strategy adapter reported an unavailable or failed route"
    if reasons & _BUDGET_FAILURES:
        return "budget", "strategies ended with different budget outcomes"
    if reasons & _GATE_FAILURES:
        return "finalization_gate", "strategies differed at deterministic finalization"
    dynamic_state = [dynamic.initial_result.get("state_update"), dynamic.complete_result.get("state_update")]
    fixed_state = [fixed.initial_result.get("state_update"), fixed.complete_result.get("state_update")]
    if dynamic_state != fixed_state:
        return "extraction", "strategies accepted different state updates"
    return "investigation", "strategies reached different outcome-bearing results"


def _paired_metric(dynamic: Metric, fixed: Metric) -> PairedMetric:
    delta = None if dynamic.value is None or fixed.value is None else dynamic.value - fixed.value
    return PairedMetric(
        dynamic=dynamic,
        fixed=fixed,
        numerator_delta=dynamic.numerator - fixed.numerator,
        denominator_delta=dynamic.denominator - fixed.denominator,
        value_delta=delta,
    )


def _paired_view(dynamic: ScoreView, fixed: ScoreView) -> PairedScoreView:
    names = ("correct_ruling_rate", "coverage_rate", "adjudicated_accuracy", "wrong_allow_rate", "correct_refusal_rate", "four_state_label_match")
    return PairedScoreView(
        dynamic=dynamic,
        fixed=fixed,
        metrics={name: _paired_metric(getattr(dynamic, name), getattr(fixed, name)) for name in names},
    )


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * percentile
    low, high = math.floor(position), math.ceil(position)
    return values[low] if low == high else values[low] + (values[high] - values[low]) * (position - low)


def _usage(results: Sequence[ReplayResult], config: TreatmentConfig, strategy: StrategyName) -> dict[str, Any]:
    totals: dict[str, int | float] = {field: 0 for field in _RESOURCE_FIELDS if field != "cost_usd"}
    cost = 0.0
    known = True
    latencies: list[float] = []
    for result in results:
        usage = result.usage
        for field in totals:
            totals[field] += _number(usage.get(field, 0))
        latencies.append(float(_number(usage.get("latency_ms", 0))))
        reported = usage.get("cost_usd")
        if reported is not None:
            cost += float(_number(reported))
        elif usage.get("provider_calls", 0):
            if config.input_cost_per_1k_tokens is None or config.output_cost_per_1k_tokens is None:
                known = False
            else:
                cost += (
                    _number(usage.get("input_tokens", 0)) * config.input_cost_per_1k_tokens
                    + _number(usage.get("output_tokens", 0)) * config.output_cost_per_1k_tokens
                ) / 1000
    planning_available = strategy != "dynamic_agent" or bool(results) and all(
        "planning_tokens" in result.usage and "planning_calls" in result.usage
        for result in results
    )
    totals["planning_tokens"] = (
        sum(_number(result.usage.get("planning_tokens", 0)) for result in results)
        if planning_available and strategy == "dynamic_agent"
        else 0
    )
    totals["planning_calls"] = (
        sum(_number(result.usage.get("planning_calls", 0)) for result in results)
        if planning_available and strategy == "dynamic_agent"
        else 0
    )
    summary: dict[str, Any] = dict(totals)
    summary.update(
        cost_usd=round(cost, 8) if known else None,
        known_cost_usd=round(cost, 8),
        usage_available=bool(results and any(item.usage for item in results)),
        planning_usage_available=planning_available,
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        case_count=len(results),
    )
    return summary


def _delta(pair: PairedReplayResult) -> ResourceDelta:
    values: dict[str, int | float | None] = {}
    for field in _RESOURCE_FIELDS:
        dynamic = pair.dynamic.usage.get(field, 0)
        fixed = pair.fixed.usage.get(field, 0)
        values[field] = None if dynamic is None or fixed is None else _number(dynamic) - _number(fixed)
    return ResourceDelta(case_id=pair.case_id, valid_for_comparison=pair.valid_for_comparison, delta=values)


def _audit(audits: Sequence[StrategyAudit]) -> dict[str, Any]:
    return {
        "observed_case_count": len(audits),
        "valid_case_count": sum(item.valid for item in audits),
        "tool_call_count": sum(item.tool_call_count for item in audits),
        "findings": [finding.model_dump(mode="json") for item in audits for finding in item.findings],
    }


def _display_delta(value: float | None) -> str:
    return "N/A" if value is None else f"{value:+.3f}"


def _display_cost(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.6f}"


def _display_optional(value: Any) -> str:
    return "N/A" if value is None else f"{float(value):.1f}"


def _deterministic(result: ReplayResult | None, view: Literal["initial", "complete"]) -> dict[str, Any] | None:
    return None if result is None else _projection(getattr(result, f"{view}_result"))


def check_determinism(
    results_by_provider: Mapping[str, Sequence[ReplayResult]],
    *,
    strategy: StrategyName | None = None,
    provider_identities: Mapping[str, str] | None = None,
) -> DeterminismReport:
    providers = sorted(str(name) for name in results_by_provider)
    identity_map = {
        str(name): str(value)
        for name, value in (provider_identities or {}).items()
    }
    if len(providers) < 2:
        return DeterminismReport(
            strategy=strategy,
            passed=False,
            providers=providers,
            provider_identities=identity_map,
            reason="at_least_two_provider_models_required",
        )
    if provider_identities is not None and (
        any(not identity_map.get(name) for name in providers)
        or len({identity_map[name] for name in providers}) < 2
    ):
        return DeterminismReport(
            strategy=strategy,
            passed=False,
            providers=providers,
            provider_identities=identity_map,
            reason="distinct_provider_model_identities_required",
        )
    indexed = {
        name: {item.case_id: item for item in results_by_provider[name]}
        for name in providers
    }
    case_ids = sorted(set().union(*(set(items) for items in indexed.values())))
    if not case_ids:
        return DeterminismReport(
            strategy=strategy,
            passed=False,
            providers=providers,
            provider_identities=identity_map,
            reason="no_cases_compared",
        )
    mismatches: list[DeterminismMismatch] = []
    baseline = providers[0]
    for case_id in case_ids:
        reference = indexed[baseline].get(case_id)
        missing = [
            name for name in providers if case_id not in indexed[name]
        ]
        for view in ("initial", "complete"):
            expected = _deterministic(reference, view)
            actual = {
                name: _deterministic(indexed[name].get(case_id), view)
                for name in providers
            }
            if missing or any(value != expected for value in actual.values()):
                mismatches.append(
                    DeterminismMismatch(
                        case_id=case_id,
                        view=view,
                        providers=providers,
                        expected=expected,
                        actual=actual,
                    )
                )
    return DeterminismReport(
        strategy=strategy,
        passed=not mismatches,
        providers=providers,
        provider_identities=identity_map,
        compared_case_ids=case_ids,
        mismatches=mismatches,
    )


verify_determinism = check_determinism


def compare_treatments(
    dataset: CaseDataset,
    adapters: Mapping[str, TreatmentAdapter],
    *,
    config: TreatmentConfig | Mapping[str, Any] | None = None,
    verified_only: bool = False,
    formal: bool = False,
    signoff: Any | None = None,
    signing_key: str | None = None,
    determinism_adapters: Mapping[str, Mapping[str, TreatmentAdapter]] | None = None,
) -> TreatmentComparisonReport:
    return TreatmentComparisonRunner(adapters, config=config).run_dataset(
        dataset,
        verified_only=verified_only,
        formal=formal,
        signoff=signoff,
        signing_key=signing_key,
        determinism_adapters=determinism_adapters,
    )


def _artifact_contract_errors(
    artifact_config: Mapping[str, Any],
    config: TreatmentConfig,
    strategy: StrategyName,
    *,
    allow_model_provider_override: bool,
) -> list[str]:
    expected = config.model_dump(mode="json")
    names = set(config.shared_contract) | {
        "schema_version",
        "fixed_route_source",
        "fixed_workflow_llm_routing",
        "planning_cost_included",
        "dynamic_strategy_version",
        "fixed_workflow_version",
        "fixed_template_version",
        "max_clarification_rounds",
        "max_fact_requests",
    }
    errors: list[str] = []
    for name in sorted(names):
        if (
            allow_model_provider_override
            and name in {"model", "provider"}
        ):
            continue
        if artifact_config.get(name) != expected.get(name):
            errors.append(name)
    if artifact_config.get("strategy") != strategy:
        errors.append("strategy")
    if artifact_config.get("budget_enforced") is not True:
        errors.append("budget_enforced")
    if not allow_model_provider_override and (
        artifact_config.get("config_hash") != config.config_hash
    ):
        errors.append("config_hash")
    return errors


def _load_artifact(
    path: Path,
    *,
    config: TreatmentConfig | None = None,
    strategy: StrategyName | None = None,
    require_envelope: bool = False,
    allow_model_provider_override: bool = False,
) -> tuple[list[ReplayResult], dict[str, dict[str, Any]], dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    observations: dict[str, dict[str, Any]] = {}
    artifact_config: dict[str, Any] = {}
    enveloped = isinstance(payload, Mapping)
    if enveloped:
        raw_config = payload.get("config")
        if isinstance(raw_config, Mapping):
            artifact_config = dict(raw_config)
        raw_observations = payload.get("observations", {})
        if isinstance(raw_observations, Mapping):
            observations = {
                str(case_id): dict(value)
                for case_id, value in raw_observations.items()
                if isinstance(value, Mapping)
            }
        payload = payload.get("results")
    if require_envelope and (
        not enveloped
        or not artifact_config
        or not observations
    ):
        raise ValueError(
            f"result artifact {path} must include config and per-case observations"
        )
    if config is not None and strategy is not None:
        errors = _artifact_contract_errors(
            artifact_config,
            config,
            strategy,
            allow_model_provider_override=allow_model_provider_override,
        )
        if errors:
            raise ValueError(
                f"result artifact {path} does not match the T14 contract: "
                + ", ".join(errors)
            )
    if not isinstance(payload, list):
        raise TypeError(f"result artifact {path} must contain a JSON list")
    results = [ReplayResult.model_validate(item) for item in payload]
    if require_envelope and set(observations) != {item.case_id for item in results}:
        raise ValueError(
            f"result artifact {path} must provide observations for every result"
        )
    return results, observations, artifact_config


def _load_results(path: Path) -> list[ReplayResult]:
    return _load_artifact(path)[0]


def _observation_value(
    observations: Mapping[str, Mapping[str, Any]],
    case_id: str,
    key: str,
) -> Any:
    value = observations.get(case_id)
    return value.get(key) if isinstance(value, Mapping) and key in value else None


def _require_exact_case_ids(
    label: str,
    results: Mapping[str, ReplayResult],
    expected_case_ids: set[str],
) -> None:
    actual_case_ids = set(results)
    if actual_case_ids != expected_case_ids:
        missing = sorted(expected_case_ids - actual_case_ids)
        extra = sorted(actual_case_ids - expected_case_ids)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise ValueError(f"{label} artifact case coverage mismatch: " + "; ".join(details))


def _load_determinism_manifest(
    path: Path,
    config: TreatmentConfig,
    *,
    expected_case_ids: set[str] | None = None,
) -> list[DeterminismReport]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, Mapping) and "providers" in payload:
        payload = payload["providers"]
    if not isinstance(payload, Mapping):
        raise TypeError("determinism manifest must map provider/model names to artifacts")
    results: dict[StrategyName, dict[str, list[ReplayResult]]] = {
        "dynamic_agent": {},
        "fixed_workflow": {},
    }
    provider_identities: dict[str, str] = {}
    for provider_name, entry in payload.items():
        if not isinstance(entry, Mapping):
            raise TypeError(f"determinism entry {provider_name!r} must be an object")
        dynamic_raw = entry.get("dynamic_results")
        fixed_raw = entry.get("fixed_results")
        if not isinstance(dynamic_raw, str) or not dynamic_raw.strip():
            raise ValueError(
                f"determinism entry {provider_name!r} needs dynamic_results and fixed_results"
            )
        if not isinstance(fixed_raw, str) or not fixed_raw.strip():
            raise ValueError(
                f"determinism entry {provider_name!r} needs dynamic_results and fixed_results"
            )
        dynamic_path = Path(dynamic_raw)
        fixed_path = Path(fixed_raw)

        if not dynamic_path.is_absolute():
            dynamic_path = path.parent / dynamic_path
        if not fixed_path.is_absolute():
            fixed_path = path.parent / fixed_path
        dynamic_items, _, dynamic_config = _load_artifact(
            dynamic_path,
            config=config,
            strategy="dynamic_agent",
            require_envelope=True,
            allow_model_provider_override=True,
        )
        fixed_items, _, fixed_config = _load_artifact(
            fixed_path,
            config=config,
            strategy="fixed_workflow",
            require_envelope=True,
            allow_model_provider_override=True,
        )
        dynamic_identity = _provider_model_identity(dynamic_config)
        fixed_identity = _provider_model_identity(fixed_config)
        if dynamic_identity != fixed_identity:
            raise ValueError(
                f"determinism entry {provider_name!r} has mismatched arm identities"
            )
        provider_identities[str(provider_name)] = dynamic_identity
        dynamic_results = {item.case_id: item for item in dynamic_items}
        fixed_results = {item.case_id: item for item in fixed_items}
        if expected_case_ids is not None:
            _require_exact_case_ids(
                f"determinism dynamic {provider_name}",
                dynamic_results,
                expected_case_ids,
            )
            _require_exact_case_ids(
                f"determinism fixed {provider_name}",
                fixed_results,
                expected_case_ids,
            )
        results["dynamic_agent"][str(provider_name)] = dynamic_items
        results["fixed_workflow"][str(provider_name)] = fixed_items
    return [
        check_determinism(
            results[strategy],
            strategy=strategy,
            provider_identities=provider_identities,
        )
        for strategy in ("dynamic_agent", "fixed_workflow")
    ]


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(prog="rulecourt-compare")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--dynamic-results", type=Path)
    parser.add_argument("--fixed-results", type=Path)
    parser.add_argument(
        "--determinism-manifest",
        type=Path,
        help="JSON map of provider/model names to dynamic/fixed result artifacts",
    )
    parser.add_argument("--run-kind", choices=("development_trial", "formal"))
    parser.add_argument(
        "--signoff",
        type=Path,
        help="detached HumanSignoff JSON required for formal exported replay",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.output and args.output.exists():
        parser.error("output already exists; choose another artifact path")
    dataset = CaseDataset.from_json(args.dataset)
    values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    if args.run_kind:
        values["run_kind"] = args.run_kind
    config = TreatmentConfig.model_validate(values)
    if config.run_kind == "development_trial" and any(case.split == "holdout" for case in dataset.cases):
        parser.error("T14 development trials do not permit holdout cases")
    if args.dry_run:
        output = json.dumps(
            {
                "dataset_version": dataset.dataset_version,
                "dataset_path": str(args.dataset),
                "run_kind": config.run_kind,
                "config": {**config.model_dump(mode="json"), "config_hash": config.config_hash},
                "strategies": ["dynamic_agent", "fixed_workflow"],
                "case_count": len(dataset.cases),
                "verified_case_count": len(dataset.verified_cases),
                "paired_input_hash": _hash([case.initial_input for case in dataset.cases]),
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n"
    elif args.dynamic_results and args.fixed_results:
        formal_run = config.run_kind == "formal"
        signoff = HumanSignoff.from_json(args.signoff) if args.signoff else None
        if formal_run:
            if signoff is None:
                parser.error("--signoff is required for formal exported replay")
            cases = dataset.validate_for_scoring(
                require_split=True,
                require_coverage=True,
                signoff=signoff,
            )
        else:
            dataset.validate_for_scoring(require_split=False)
            cases = dataset.cases
        determinism = (
            _load_determinism_manifest(
                args.determinism_manifest,
                config,
                expected_case_ids={case.id for case in cases},
            )
            if args.determinism_manifest
            else None
        )
        if formal_run and (
            determinism is None
            or any(
                not item.passed
                or len(item.providers) < 2
                for item in determinism
            )
        ):
            parser.error(
                "--determinism-manifest with two provider/model entries is required "
                "for formal exported replay"
            )
        dynamic_items, dynamic_observations, _ = _load_artifact(
            args.dynamic_results,
            config=config,
            strategy="dynamic_agent",
            require_envelope=True,
        )
        fixed_items, fixed_observations, _ = _load_artifact(
            args.fixed_results,
            config=config,
            strategy="fixed_workflow",
            require_envelope=True,
        )
        dynamic = {item.case_id: item for item in dynamic_items}
        fixed = {item.case_id: item for item in fixed_items}
        expected_case_ids = {case.id for case in cases}
        _require_exact_case_ids("dynamic", dynamic, expected_case_ids)
        _require_exact_case_ids("fixed", fixed, expected_case_ids)
        runner = TreatmentComparisonRunner.from_artifacts(config)
        pairs: list[PairedReplayResult] = []
        for case in cases:
            da = VisibilityAuditor.audit(
                "dynamic_agent",
                initial_input=case.initial_input,
                initial_result=dynamic[case.id].initial_result,
                complete_result=dynamic[case.id].complete_result,
                context=_observation_value(dynamic_observations, case.id, "context"),
                tool_results=_observation_value(
                    dynamic_observations, case.id, "tool_results"
                ),
                log_projection=_observation_value(
                    dynamic_observations, case.id, "log_projection"
                ),
                observed_strategy=_observation_value(
                    dynamic_observations, case.id, "observed_strategy"
                ),
                observed_route=_observation_value(
                    dynamic_observations, case.id, "observed_route"
                ),
                usage=dynamic[case.id].usage,
                budget=InvestigationBudget.from_mapping(config.budget),
            )
            fa = VisibilityAuditor.audit(
                "fixed_workflow",
                initial_input=case.initial_input,
                initial_result=fixed[case.id].initial_result,
                complete_result=fixed[case.id].complete_result,
                context=_observation_value(fixed_observations, case.id, "context"),
                tool_results=_observation_value(
                    fixed_observations, case.id, "tool_results"
                ),
                log_projection=_observation_value(
                    fixed_observations, case.id, "log_projection"
                ),
                observed_strategy=_observation_value(
                    fixed_observations, case.id, "observed_strategy"
                ),
                observed_route=_observation_value(
                    fixed_observations, case.id, "observed_route"
                ),
                usage=fixed[case.id].usage,
                budget=InvestigationBudget.from_mapping(config.budget),
            )
            difference, reason = _classify_difference(dynamic[case.id], fixed[case.id])
            pairs.append(
                PairedReplayResult(
                    case_id=case.id,
                    family_id=case.family_id,
                    category=case.category,
                    dynamic=dynamic[case.id],
                    fixed=fixed[case.id],
                    audit=_pair_audit(case.initial_input, da, fa),
                    difference_class=difference,
                    difference_reason=reason,
                )
            )
        report = runner.build_report(
            dataset,
            cases,
            pairs,
            formal=formal_run,
            signoff=signoff,
            determinism=determinism,
        )
        output = (
            report.to_markdown()
            if args.format == "markdown"
            else json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
    else:
        parser.error("live comparison requires adapters; use dry-run or exported result artifacts")
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        sys.stdout.write(output)
    return 0


T14Config = TreatmentConfig
T14Report = TreatmentComparisonReport
PairComparisonRunner = TreatmentComparisonRunner
PairedComparisonRunner = TreatmentComparisonRunner
TreatmentRunReport = StrategyRunReport
T14ComparisonReport = TreatmentComparisonReport
run_comparison = compare_treatments
run_t14_comparison = compare_treatments


if __name__ == "__main__":
    raise SystemExit(main())
