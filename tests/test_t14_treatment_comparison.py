import json
from collections.abc import Mapping

import pytest

from rulecourt.comparison import (
    DeterminismReport,
    TreatmentComparisonRunner,
    TreatmentConfig,
    VisibilityAuditor,
    check_determinism,
)
from rulecourt.evaluation import CaseDataset, EvaluationCase, ReplayResult


def _case(case_id: str = "case-1", *, split: str | None = None) -> EvaluationCase:
    return EvaluationCase.model_validate(
        {
            "id": case_id,
            "family_id": "family-" + case_id,
            "category": "ordinary",
            "scope": "local_move_conditions",
            "initial_input": "确认本次只裁决普通移动范围。Marquise 从 A 移动 1 个 warriors 到 B。",
            "initial_label": "INSUFFICIENT_INFORMATION",
            "complete_label": "LEGAL",
            "label_source": "reviewed:root-m0-v1",
            "clarification_facts": {
                "clearings.A.adjacent_to": {
                    "value": ["B"],
                    "text": "A 只与 B 相邻。",
                    "source": "golden:case-1",
                    "complete": True,
                }
            },
            "fact_sources": {"clearings.A.adjacent_to": "golden:case-1"},
            "acceptable_questions": ["clearings.A.adjacent_to"],
            "evidence": ["root-4.2"],
            "review": {
                "status": "verified",
                "reviewer_id": "reviewer-1",
                "rule_version": "root-law-2025-10",
                "reviewed_at": "2026-09-23T00:00:00Z",
                "basis": "Independent review.",
                "evidence": ["review-note:1"],
                "report": "Checked independently.",
                "checks": {
                    "labels": True,
                    "scope": True,
                    "fact_availability": True,
                    "evidence": True,
                    "acceptable_questions": True,
                    "independent_source": True,
                },
            },
            "split": split,
        }
    )


def _response(status: str, *, usage: Mapping[str, int] | None = None, **extra):
    payload = {
        "status": status,
        "reason": status,
        "clarification_questions": (
            [{"field": "clearings.A.adjacent_to", "question": "Which clearings?"}]
            if status == "INSUFFICIENT_INFORMATION"
            else []
        ),
    }
    if usage is not None:
        payload["investigation"] = {"usage": dict(usage), "run_count": 1}
    payload.update(extra)
    return payload


class _Adapter:
    def __init__(self, strategy: str, responses, *, events=None, metadata=None):
        self.strategy = strategy
        self.responses = list(responses)
        self.events = list(events or [])
        self.metadata = metadata or {}
        self.messages: list[str] = []
        self.case_id = f"runtime-{strategy}"
        self.configured_budget = None

    def configure_budget(self, budget):
        self.configured_budget = budget.to_dict()
        self.metadata.update(
            {
                "budget": self.configured_budget,
                "budget_enforced": True,
            }
        )

    def create_case(self):
        return self.case_id

    def submit_message(self, case_id, text):
        assert case_id == self.case_id
        self.messages.append(text)
        return self.responses.pop(0)

    def get_case(self, case_id):
        return {"id": case_id, "strategy": self.strategy, "messages": self.messages}

    def get_events(self, case_id):
        return [
            {"type": "investigation_started", "strategy": self.strategy},
            *self.events,
        ]

    def comparison_metadata(self):
        return {"strategy": self.strategy, **self.metadata}


def _runner(dynamic, fixed, **config):
    treatment = TreatmentConfig(**config)
    for adapter in (dynamic, fixed):
        adapter.metadata.update(
            {
                **treatment.shared_contract,
                "fixed_route_source": treatment.fixed_route_source,
                "fixed_template_version": treatment.fixed_template_version,
                "fixed_workflow_llm_routing": False,
                "strategy_version": (
                    treatment.dynamic_strategy_version
                    if adapter.strategy == "dynamic_agent"
                    else treatment.fixed_workflow_version
                ),
            }
        )
    return TreatmentComparisonRunner(
        {"dynamic_agent": dynamic, "fixed_workflow": fixed},
        config=treatment,
    )


def test_treatment_runner_pairs_same_input_and_reports_first_complete_resource_deltas():
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    dynamic = _Adapter(
        "dynamic_agent",
        [
            _response(
                "INSUFFICIENT_INFORMATION",
                usage={"provider_calls": 1, "input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            ),
            _response(
                "LEGAL",
                usage={"provider_calls": 2, "input_tokens": 20, "output_tokens": 10, "total_tokens": 30},
            ),
        ],
    )
    fixed = _Adapter(
        "fixed_workflow",
        [
            _response("INSUFFICIENT_INFORMATION", usage={"iterations": 1, "total_tokens": 0}),
            _response("LEGAL", usage={"iterations": 2, "total_tokens": 0}),
        ],
    )

    report = _runner(dynamic, fixed).run_dataset(dataset)

    assert report.schema_version == "t14-treatment-comparison-v1"
    assert report.run_kind == "development_trial"
    assert report.paired_case_ids == [case.id]
    assert report.runs["dynamic_agent"].evaluation.complete.correct_ruling_rate.numerator == 1
    assert report.runs["fixed_workflow"].evaluation.complete.correct_ruling_rate.numerator == 1
    assert report.runs["dynamic_agent"].usage["total_tokens"] == 30
    assert report.runs["fixed_workflow"].usage["total_tokens"] == 0
    assert report.resource_deltas[0].case_id == case.id
    assert report.resource_deltas[0].delta["total_tokens"] == 30
    assert dynamic.configured_budget == fixed.configured_budget == TreatmentConfig().budget
    assert report.runs["dynamic_agent"].usage["planning_usage_available"] is False
    assert dynamic.messages[0] == fixed.messages[0] == case.initial_input
    assert dynamic.messages[1] == fixed.messages[1] == "A 只与 B 相邻。"


def test_visibility_audit_excludes_a_pair_when_coverage_or_diagnostic_route_leaks():
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    leaked = _Adapter(
        "dynamic_agent",
        [_response("LEGAL", coverage_obligations=["hidden-obligation"])],
    )
    fixed = _Adapter(
        "fixed_workflow",
        [_response("LEGAL", usage={"iterations": 1})],
        events=[{"type": "diagnostic_route", "next_tool": "inspect_rule"}],
    )

    report = _runner(leaked, fixed).run_dataset(dataset)

    assert report.paired_case_ids == []
    assert report.pairs[0].valid_for_comparison is False
    assert report.pairs[0].audit.valid is False
    assert {finding.code for finding in report.pairs[0].audit.findings} == {
        "coverage_table_leak",
        "diagnostic_route_leak",
    }
    assert report.excluded_case_ids == {"invalid_audit": [case.id]}


def test_tool_order_is_audit_data_not_a_quality_success_metric():
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    dynamic = _Adapter(
        "dynamic_agent",
        [_response("LEGAL", usage={"provider_calls": 1, "total_tokens": 7})],
        events=[
            {"type": "tool_call", "name": "inspect_case", "status": "ok"},
            {"type": "tool_call", "name": "simulate_action", "status": "ok"},
        ],
    )
    fixed = _Adapter(
        "fixed_workflow",
        [_response("LEGAL", usage={"iterations": 1, "total_tokens": 0})],
        events=[
            {"type": "tool_call", "name": "simulate_action", "status": "ok"},
            {"type": "tool_call", "name": "inspect_case", "status": "ok"},
        ],
    )

    report = _runner(dynamic, fixed).run_dataset(dataset)

    assert report.paired_case_ids == [case.id]
    assert report.pairs[0].difference_class == "none"
    assert report.pairs[0].audit.dynamic.tool_call_count == 2
    assert report.pairs[0].audit.fixed.tool_call_count == 2


def test_missing_audit_projection_is_invalid():
    audit = VisibilityAuditor.audit(
        "dynamic_agent",
        initial_input="input",
        initial_result={"status": "LEGAL"},
        complete_result={"status": "LEGAL"},
    )

    assert audit.valid is False
    assert audit.observation_available is False
    assert {finding.code for finding in audit.findings} == {
        "missing_audit_projection"
    }


def test_treatment_config_rejects_route_generated_from_private_coverage_table():
    with pytest.raises(ValueError, match="coverage table"):
        TreatmentConfig(fixed_route_source="coverage_table")


def test_development_trial_rejects_holdout_cases():
    dataset = CaseDataset(dataset_version="trial-v1", cases=[_case(split="holdout")])
    with pytest.raises(ValueError, match="holdout"):
        _runner(
            _Adapter("dynamic_agent", [_response("LEGAL")]),
            _Adapter("fixed_workflow", [_response("LEGAL")]),
        ).run_dataset(dataset)


def _replay(case_id: str, status: str, *, explanation: str = "") -> ReplayResult:
    response = {"status": status, "reason": status, "explanation": explanation}
    return ReplayResult(
        case_id=case_id,
        family_id="family-" + case_id,
        category="ordinary",
        initial_result=response,
        complete_result=response,
    )


def test_determinism_check_ignores_explanation_and_tool_order_but_reports_verdict_mismatch():
    same = check_determinism(
        {
            "provider-a/model-a": [_replay("case-1", "LEGAL", explanation="A")],
            "provider-b/model-b": [_replay("case-1", "LEGAL", explanation="B")],
        }
    )
    mismatch = check_determinism(
        {
            "provider-a/model-a": [_replay("case-1", "LEGAL")],
            "provider-b/model-b": [_replay("case-1", "ILLEGAL")],
        }
    )

    assert isinstance(same, DeterminismReport)
    assert same.passed is True
    assert mismatch.passed is False
    assert mismatch.mismatches[0].case_id == "case-1"


def test_runner_records_determinism_for_two_provider_model_sets():
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    base = _runner(
        _Adapter("dynamic_agent", [_response("LEGAL")]),
        _Adapter("fixed_workflow", [_response("LEGAL")]),
    )
    provider_a = _runner(
        _Adapter("dynamic_agent", [_response("LEGAL")]),
        _Adapter("fixed_workflow", [_response("LEGAL")]),
    ).adapters
    provider_b = _runner(
        _Adapter("dynamic_agent", [_response("LEGAL")]),
        _Adapter("fixed_workflow", [_response("LEGAL")]),
    ).adapters

    report = base.run_dataset(
        dataset,
        determinism_adapters={
            "provider-a/model-a": provider_a,
            "provider-b/model-b": provider_b,
        },
    )

    assert [item.strategy for item in report.determinism] == [
        "dynamic_agent",
        "fixed_workflow",
    ]
    assert all(item.passed for item in report.determinism)
    assert all(
        item.providers == ["provider-a/model-a", "provider-b/model-b"]
        for item in report.determinism
    )


def test_treatment_report_round_trips_and_cli_dry_run_is_provider_free(tmp_path, capsys):
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    dynamic = _Adapter("dynamic_agent", [_response("LEGAL")])
    fixed = _Adapter("fixed_workflow", [_response("LEGAL")])
    report = _runner(dynamic, fixed).run_dataset(dataset)
    path = tmp_path / "t14-report.json"
    report.save_json(path)

    loaded = type(report).from_json(path)
    assert loaded.to_dict() == report.to_dict()

    dataset_path = tmp_path / "cases.json"
    dataset.save_json(dataset_path)
    from rulecourt.comparison import main

    assert main([str(dataset_path), "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["strategies"] == ["dynamic_agent", "fixed_workflow"]
    assert payload["run_kind"] == "development_trial"
