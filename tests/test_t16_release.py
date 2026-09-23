import json

import pytest
from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse

from rulecourt.api import create_app
from rulecourt.budget import InvestigationBudget
from rulecourt.evaluation import (
    REQUIRED_COVERAGE_TAGS,
    CaseDataset,
    EvaluationCase,
    EvaluationReport,
    HumanSignoff,
    ReplayResult,
    score_results,
)
from rulecourt.release import (
    GROUPS,
    FrozenExperimentConfig,
    ReleaseBuilder,
    ReleaseReport,
)

SIGNING_KEY = "t16-test-signing-key"


class _PublicSmokeProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="t16-public-smoke")

    def get_default_model(self):
        return "t16-public-smoke-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="No authoritative ruling.")


def _review() -> dict:
    return {
        "status": "verified",
        "reviewer_id": "reviewer-t16",
        "rule_version": "root-law-2025-10",
        "reviewed_at": "2026-09-22T00:00:00Z",
        "basis": "Independently checked against the frozen Root ruleset.",
        "evidence": ["review-note:t16"],
        "report": "The labels, facts, scope, evidence, and questions were reviewed.",
        "checks": {
            "labels": True,
            "scope": True,
            "fact_availability": True,
            "evidence": True,
            "acceptable_questions": True,
            "independent_source": True,
        },
    }


def _case(case_id: str, family_id: str, *, category: str = "ordinary") -> EvaluationCase:
    return EvaluationCase.model_validate(
        {
            "id": case_id,
            "family_id": family_id,
            "category": category,
            "coverage_tags": list(REQUIRED_COVERAGE_TAGS),
            "scope": "local_move_conditions",
            "ruleset_id": "root-law-2025-10",
            "initial_input": f"Move in case {case_id}.",
            "initial_label": "LEGAL",
            "complete_label": "LEGAL",
            "label_source": "human:t16-labels",
            "evidence": ["root-4.2"],
            "acceptable_questions": [],
            "source_provenance": [
                {"source_id": "human:t16-labels", "kind": "human_review"},
                {"source_id": "root-4.2", "kind": "official_rule"},
                {"source_id": "review-note:t16", "kind": "human_review"},
            ],
            "review": _review(),
        }
    )


def _dataset(*, shared_holdout_family: bool = False) -> tuple[CaseDataset, HumanSignoff]:
    dataset = CaseDataset(
        dataset_version="golden-t16-v1",
        cases=[
            _case("case-dev-1", "family-dev-1"),
            _case("case-dev-2", "family-dev-2", category="eyrie"),
            _case("case-holdout-1", "family-holdout-1", category="bird"),
            _case(
                "case-holdout-2",
                "family-holdout-1" if shared_holdout_family else "family-holdout-2",
                category="unknown",
            ),
        ],
    )
    dataset.split_by_family(holdout_fraction=0.5, seed=11)
    signoff = HumanSignoff(
        dataset_version=dataset.dataset_version,
        dataset_digest=dataset.approval_digest(),
        approved_case_ids=[case.id for case in dataset.verified_cases],
        reviewer_ids=["reviewer-t16"],
        coverage_reviewed=list(REQUIRED_COVERAGE_TAGS),
        signed_by="reviewer-t16",
        signed_at="2026-09-22T01:00:00Z",
        approval_reference="t16-human-signoff",
        signature="pending",
    ).seal(SIGNING_KEY)
    return dataset, signoff


def _result(case: EvaluationCase, status: str = "LEGAL", *, run: int = 1) -> ReplayResult:
    response = {"status": status, "reason": "MOVE_LEGAL" if status == "LEGAL" else status}
    return ReplayResult(
        case_id=case.id,
        family_id=case.family_id,
        category=case.category,
        initial_result=response,
        complete_result=response,
        run_count=run,
        usage={
            "provider_calls": 1,
            "input_tokens": 10,
            "output_tokens": 5,
            "total_tokens": 15,
            "latency_ms": 20,
            "cost_usd": 0.01,
        },
    )


def _evaluation(dataset: CaseDataset, *, status: str = "LEGAL", run: int = 1) -> EvaluationReport:
    results = [_result(case, status, run=run) for case in dataset.verified_cases]
    return score_results(dataset, results)


def _frozen_config(dataset: CaseDataset, *, repeats: int = 1) -> FrozenExperimentConfig:
    return FrozenExperimentConfig.freeze_from_development(
        dataset,
        release_id="t16-release-1",
        budget=InvestigationBudget(
            max_duration_seconds=30,
            max_iterations=3,
            max_tool_calls=8,
            max_total_tokens=8192,
            configuration_status="frozen",
            _source="development-trial-1",
        ),
        max_clarification_rounds=8,
        max_fact_requests=8,
        repeat_count=repeats,
        statistical_method="paired_case_unanimous",
        min_quality_gain=0.05,
        wrong_allow_tolerance=0.0,
        minimum_decision_cases=1,
        group_configs={
            group: {
                "model_version": f"model-{group}-v1",
                "strategy_version": f"strategy-{group}-v1",
                "config_hash": f"config-{group}-v1",
                "cache_condition": "cold-index-v1",
            }
            for group in GROUPS
        },
        rule_material_version="root-law-2025-10-material-v1",
        coverage_table_version="root-coverage-v1",
        cache_condition="cold-index-v1",
        reproduction_steps=["uv sync --locked", "uv run rulecourt-release report release.json"],
    )


def _reports(dataset: CaseDataset, *, repeats: int = 1):
    from rulecourt.baselines import BaselineReport, BaselineRunReport
    from rulecourt.comparison import (
        PairedMetric,
        PairedScoreView,
        StrategyRunReport,
        TreatmentComparisonReport,
    )

    baseline_runs = {}
    for group in ("llm_only", "vanilla_rag", "hybrid_rag"):
        baseline_runs[group] = BaselineRunReport(
            strategy=group,
            config={
                "model_version": f"model-{group}-v1",
                "strategy_version": f"strategy-{group}-v1",
                "config_hash": f"config-{group}-v1",
                "cache_condition": "cold-index-v1",
            },
            evaluation=_evaluation(dataset),
            usage={"total_tokens": 60, "cost_usd": 0.04, "latency_ms": 80},
            citation_error_count=0,
            timeout_count=0,
            results=[item.model_dump(mode="json") for item in _evaluation(dataset).outcomes],
        )
    baseline = BaselineReport(
        dataset_version=dataset.dataset_version,
        ruleset_id="root-law-2025-10",
        ruleset_version="2025-10",
        runs=baseline_runs,
        paired_case_ids=sorted(case.id for case in dataset.verified_cases),
        run_kind="formal",
    )

    evaluation = _evaluation(dataset)
    dynamic_run = StrategyRunReport(
        strategy="dynamic_agent",
        config={
            "model_version": "model-dynamic_agent-v1",
            "strategy_version": "strategy-dynamic_agent-v1",
            "config_hash": "config-dynamic_agent-v1",
            "cache_condition": "cold-index-v1",
        },
        evaluation=evaluation,
        usage={"total_tokens": 30, "cost_usd": 0.02, "latency_ms": 30},
        results=[item.model_dump(mode="json") for item in evaluation.outcomes],
        observations={case.id: {} for case in dataset.verified_cases},
    )
    fixed_run = StrategyRunReport(
        strategy="fixed_workflow",
        config={
            "model_version": "model-fixed_workflow-v1",
            "strategy_version": "strategy-fixed_workflow-v1",
            "config_hash": "config-fixed_workflow-v1",
            "cache_condition": "cold-index-v1",
        },
        evaluation=evaluation,
        usage={"total_tokens": 50, "cost_usd": 0.03, "latency_ms": 50},
        results=[item.model_dump(mode="json") for item in evaluation.outcomes],
        observations={case.id: {} for case in dataset.verified_cases},
    )
    paired_metric = PairedMetric(
        dynamic=evaluation.complete.correct_ruling_rate,
        fixed=evaluation.complete.correct_ruling_rate,
        numerator_delta=0,
        denominator_delta=0,
        value_delta=0.0,
    )
    paired_view = PairedScoreView(
        dynamic=evaluation.complete,
        fixed=evaluation.complete,
        metrics={"correct_ruling_rate": paired_metric},
    )
    treatment = TreatmentComparisonReport(
        dataset_version=dataset.dataset_version,
        ruleset_id="root-law-2025-10",
        ruleset_version="2025-10",
        run_kind="formal",
        config={"run_kind": "formal"},
        runs={"dynamic_agent": dynamic_run, "fixed_workflow": fixed_run},
        paired_case_ids=sorted(case.id for case in dataset.verified_cases),
        initial=paired_view,
        complete=paired_view,
    )
    return baseline, treatment


def test_formal_config_can_only_be_frozen_from_development_and_binds_dataset():
    dataset, _ = _dataset()
    config = _frozen_config(dataset)

    assert config.configuration_status == "frozen"
    assert config.run_kind == "formal"
    assert config.budget["configuration_status"] == "frozen"
    assert config.config_hash
    config.validate_for_formal(dataset)

    with pytest.raises(ValueError, match="holdout|development partition"):
        FrozenExperimentConfig.freeze_from_development(
            dataset.model_copy(deep=True).model_copy(update={"cases": [dataset.cases[-1]]}),
            release_id="bad",
            budget=config.budget,
            max_clarification_rounds=8,
            max_fact_requests=8,
            repeat_count=1,
            statistical_method="paired_case_unanimous",
            min_quality_gain=0.05,
            wrong_allow_tolerance=0,
            group_configs=config.group_configs,
            rule_material_version="rules-v1",
            coverage_table_version="coverage-v1",
            cache_condition="cold-v1",
            reproduction_steps=["reproduce"],
        )


def test_release_requires_all_five_groups_and_deduplicates_repeated_cases():
    dataset, signoff = _dataset()
    config = _frozen_config(dataset)
    baseline, treatment = _reports(dataset)

    report = ReleaseBuilder(config).build(
        dataset,
        baseline_reports=[baseline],
        treatment_reports=[treatment],
        signoff=signoff,
        signing_key=SIGNING_KEY,
    )

    assert set(report.groups) == set(GROUPS)
    assert report.holdout_case_ids
    assert report.groups["dynamic_agent"].sample_count == len(report.holdout_case_ids)
    assert report.groups["dynamic_agent"].family_count == len(report.holdout_family_ids)
    assert (
        report.groups["dynamic_agent"].observation_count
        == report.groups["dynamic_agent"].sample_count
    )
    assert report.paired.complete_case_ids == report.holdout_case_ids
    assert report.conclusion.primary == "insufficient_evidence"
    assert report.signature.startswith("hmac-sha256:")

    restored = ReleaseReport.model_validate_json(report.model_dump_json())
    restored.validate_integrity(signing_key=SIGNING_KEY)


def test_repeated_runs_and_shared_families_keep_unique_denominators():
    dataset, signoff = _dataset(shared_holdout_family=True)
    config = _frozen_config(dataset, repeats=2)
    baseline, treatment = _reports(dataset)
    report = ReleaseBuilder(config).build(
        dataset,
        baseline_reports=[baseline, baseline.model_copy(deep=True)],
        treatment_reports=[treatment, treatment.model_copy(deep=True)],
        signoff=signoff,
        signing_key=SIGNING_KEY,
    )

    dynamic = report.groups["dynamic_agent"]
    assert dynamic.repeat_count == 2
    assert dynamic.sample_count == len(report.holdout_case_ids)
    assert dynamic.family_count == len(report.holdout_family_ids)
    assert dynamic.family_count == len(set(dynamic.family_ids)) < dynamic.sample_count
    assert dynamic.observation_count == dynamic.sample_count
    assert dynamic.usage["repeat_count"] == 2


def test_release_rejects_missing_group_and_post_hoc_threshold_changes():
    dataset, signoff = _dataset()
    config = _frozen_config(dataset)
    baseline, treatment = _reports(dataset)
    baseline.runs.pop("hybrid_rag")

    with pytest.raises(ValueError, match="hybrid_rag"):
        ReleaseBuilder(config).build(
            dataset,
            baseline_reports=[baseline],
            treatment_reports=[treatment],
            signoff=signoff,
            signing_key=SIGNING_KEY,
        )

    baseline, treatment = _reports(dataset)
    report = ReleaseBuilder(config).build(
        dataset,
        baseline_reports=[baseline],
        treatment_reports=[treatment],
        signoff=signoff,
        signing_key=SIGNING_KEY,
    )
    payload = report.model_dump(mode="json")
    payload["config"]["min_quality_gain"] = 0.99
    tampered = ReleaseReport.model_validate(payload)
    with pytest.raises(ValueError, match="config hash|conclusion"):
        tampered.validate_integrity(signing_key=SIGNING_KEY)


def test_release_round_trip_and_cli_report(tmp_path, capsys, monkeypatch):
    dataset, signoff = _dataset()
    config = _frozen_config(dataset)
    baseline, treatment = _reports(dataset)
    report = ReleaseBuilder(config).build(
        dataset,
        baseline_reports=[baseline],
        treatment_reports=[treatment],
        signoff=signoff,
        signing_key=SIGNING_KEY,
    )
    path = tmp_path / "t16-release.json"
    report.save_json(path)
    monkeypatch.setenv("RULECOURT_T16_RELEASE_KEY", SIGNING_KEY)

    from rulecourt.release import main

    assert main(["report", str(path)]) == 0
    output = capsys.readouterr().out
    assert "quality" in output.lower()
    assert "holdout" in output.lower()
    assert json.loads(path.read_text(encoding="utf-8"))["run_kind"] == "formal"


def test_public_case_state_investigation_and_verdict_views_remain_available(tmp_path):
    with TestClient(
        create_app(tmp_path / "t16-public.sqlite3", provider=_PublicSmokeProvider())
    ) as client:
        assert client.get("/").status_code == 200
        created = client.post("/api/cases", json={})
        assert created.status_code == 201
        case_id = created.json()["id"]
        assert client.get("/api/investigation-budget").status_code == 200

        result = client.post(
            f"/api/cases/{case_id}/messages", json={"text": "T16 public smoke case"}
        )
        assert result.status_code == 200
        assert {"status", "reason", "investigation"} <= set(result.json())

        state = client.get(f"/api/cases/{case_id}")
        assert state.status_code == 200
        state_payload = state.json()
        assert {"messages", "confirmed_state", "investigation_runs", "verdicts"} <= set(
            state_payload
        )
        events = client.get(f"/api/cases/{case_id}/events")
        assert events.status_code == 200
        assert events.json()
