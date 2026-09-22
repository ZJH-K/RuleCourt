import json

import pytest

from rulecourt.evaluation import (
    REQUIRED_COVERAGE_TAGS,
    CaseDataset,
    EvaluationCase,
    EvaluationReport,
    EvaluationRunner,
    FactRequest,
    FactResponder,
    HumanSignoff,
    ReplayResult,
    score_results,
)

TEST_SIGNOFF_KEY = "t15-test-signing-key"


def test_fact_responder_only_returns_requested_facts_and_repeats_deterministically():
    responder = FactResponder(
        {
            "clearings.A.presence.marquise.warriors": {
                "value": 3,
                "text": "Marquise 在 A 有 3 个 warriors。",
                "source": "fixture:case-1",
            },
            "clearings.A.presence.eyrie.warriors": {
                "value": 2,
                "text": "Eyrie 在 A 有 2 个 warriors。",
                "source": "fixture:case-1",
            },
        }
    )

    first = responder.answer(FactRequest(fields=["clearings.A.presence.marquise.warriors"]))
    repeated = responder.answer(FactRequest(fields=["clearings.A.presence.marquise.warriors"]))

    assert first.status == "answered"
    assert first.facts == {"clearings.A.presence.marquise.warriors": 3}
    assert first.text == "Marquise 在 A 有 3 个 warriors。"
    assert first.sources == ["fixture:case-1"]
    assert first.completeness == {"clearings.A.presence.marquise.warriors": False}
    assert repeated.model_dump() | {"round": first.round} == first.model_dump()
    assert responder.request_count == 2
    assert "clearings.A.presence.eyrie.warriors" not in first.facts


def test_fact_responder_keeps_unlabelled_facts_unknown():
    responder = FactResponder({"clearings.A.presence.marquise.warriors": 3})

    answer = responder.answer(FactRequest(fields=["clearings.B.presence.marquise.warriors"]))

    assert answer.status == "unknown"
    assert answer.facts == {}
    assert answer.unknown_fields == ["clearings.B.presence.marquise.warriors"]


def test_fact_responder_requires_an_explicit_target_for_free_form_questions():
    responder = FactResponder({"clearings.A.presence.marquise.warriors": 3})

    answer = responder.answer(FactRequest(text="告诉我完整局面。"))

    assert answer.status == "ambiguous"
    assert answer.facts == {}
    assert answer.requested_fields == []


def test_fact_responder_counts_repeated_requests_against_its_budget():
    responder = FactResponder({"clearings.A.presence.marquise.warriors": 3}, max_requests=1)
    request = FactRequest(fields=["clearings.A.presence.marquise.warriors"])

    assert responder.answer(request).status == "answered"
    exhausted = responder.answer(request)

    assert exhausted.status == "budget_exhausted"
    assert exhausted.round == 2
    assert responder.request_count == 2


def _case(
    case_id: str,
    family_id: str,
    review_status: str = "verified",
    coverage_tags: list[str] | None = None,
) -> dict:
    return {
        "id": case_id,
        "family_id": family_id,
        "category": "ordinary",
        "coverage_tags": list(coverage_tags or []),
        "scope": "local_move_conditions",
        "initial_input": "确认本次只裁决 Marquise 普通移动范围。Marquise 从 A 移动 1 个 warriors 到 B。",
        "initial_label": "INSUFFICIENT_INFORMATION",
        "complete_label": "LEGAL",
        "label_source": "reviewed:root-m0-v1"
        if review_status == "verified"
        else "candidate:unverified",
        "clarification_facts": {
            "clearings.A.adjacent_to": {
                "value": ["B"],
                "text": "A 只与 B 相邻。",
                "source": "golden:case-1",
                "complete": True,
            }
        },
        "fact_sources": {"clearings.A.adjacent_to": "golden:case-1"},
        "source_provenance": [
            {"source_id": "reviewed:root-m0-v1", "kind": "human_review"},
            {"source_id": "golden:case-1", "kind": "primary_fact"},
            {"source_id": "root-4.2", "kind": "official_rule"},
            {"source_id": "root-4.2.1", "kind": "official_rule"},
            {"source_id": "review-note:1", "kind": "human_review"},
        ],
        "evidence": ["root-4.2", "root-4.2.1"],
        "acceptable_questions": ["clearings.A.adjacent_to"],
        "review": {
            "status": review_status,
            "reviewer_id": "reviewer-1" if review_status == "verified" else None,
            "rule_version": "root-law-2025-10" if review_status == "verified" else None,
            "reviewed_at": "2026-09-22T00:00:00Z" if review_status == "verified" else None,
            "basis": "Checked against the fixed Root ruleset."
            if review_status == "verified"
            else "",
            "evidence": ["review-note:1"] if review_status == "verified" else [],
            "report": "Independent reviewer checked labels and evidence."
            if review_status == "verified"
            else "",
            "checks": (
                {
                    "labels": True,
                    "scope": True,
                    "fact_availability": True,
                    "evidence": True,
                    "acceptable_questions": True,
                    "independent_source": True,
                }
                if review_status == "verified"
                else {}
            ),
        },
    }


def test_case_dataset_round_trips_reviews_and_excludes_unverified_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "dataset_version": "candidate-v1",
                "cases": [_case("verified-1", "family-1"), _case("draft-1", "family-2", "draft")],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    dataset = CaseDataset.from_json(path)

    assert [case.id for case in dataset.verified_cases] == ["verified-1"]
    assert dataset.excluded_counts == {"draft": 1, "disputed": 0}
    assert dataset.to_dict()["dataset_version"] == "candidate-v1"


def test_case_dataset_split_keeps_each_family_in_one_partition():
    dataset = CaseDataset(
        dataset_version="candidate-v1",
        cases=[
            EvaluationCase.model_validate(_case("a-1", "family-a")),
            EvaluationCase.model_validate(_case("a-2", "family-a")),
            EvaluationCase.model_validate(_case("b-1", "family-b")),
            EvaluationCase.model_validate(_case("c-1", "family-c")),
        ],
    )

    partitions = dataset.split_by_family(holdout_fraction=0.34, seed=7)

    assert {case.family_id for case in partitions["development"]}.isdisjoint(
        {case.family_id for case in partitions["holdout"]}
    )
    assert sorted(case.id for case in partitions["development"] + partitions["holdout"]) == [
        "a-1",
        "a-2",
        "b-1",
        "c-1",
    ]


def test_case_dataset_rejects_invalid_holdout_fraction():
    dataset = CaseDataset(dataset_version="candidate-v1", cases=[])

    with pytest.raises(ValueError, match="holdout_fraction"):
        dataset.split_by_family(holdout_fraction=1.0)


def _outcome(case_id: str, initial_status: str, complete_status: str, failure_reason=None):
    return ReplayResult(
        case_id=case_id,
        family_id="family-" + case_id,
        category="ordinary",
        initial_result={"status": initial_status, "reason": initial_status},
        complete_result={"status": complete_status, "reason": complete_status},
        failure_reason=failure_reason,
    )


def test_score_results_separates_first_and_complete_domains_and_excludes_drafts():
    verified_legal = EvaluationCase.model_validate(_case("legal", "family-legal"))
    verified_legal.complete_label = "LEGAL"
    verified_illegal = EvaluationCase.model_validate(_case("illegal", "family-illegal"))
    verified_illegal.initial_label = "ILLEGAL"
    verified_illegal.complete_label = "ILLEGAL"
    verified_unknown = EvaluationCase.model_validate(_case("unknown", "family-unknown"))
    verified_unknown.complete_label = "UNRESOLVED"
    draft = EvaluationCase.model_validate(_case("draft", "family-draft", "draft"))
    outcomes = [
        _outcome("legal", "INSUFFICIENT_INFORMATION", "LEGAL"),
        _outcome("illegal", "ILLEGAL", "ILLEGAL"),
        _outcome("unknown", "UNRESOLVED", "UNRESOLVED", "PROVIDER_ERROR"),
        _outcome("draft", "LEGAL", "LEGAL"),
    ]

    report = score_results(
        CaseDataset(
            dataset_version="trial-1",
            cases=[verified_legal, verified_illegal, verified_unknown, draft],
        ),
        outcomes,
    )

    assert isinstance(report, EvaluationReport)
    assert report.scored_case_count == 3
    assert report.excluded_counts == {"draft": 1, "disputed": 0}
    assert report.initial.correct_ruling_rate.numerator == 1
    assert report.initial.correct_ruling_rate.denominator == 1
    assert report.initial.correct_refusal_rate.numerator == 1
    assert report.initial.correct_refusal_rate.denominator == 2
    assert report.complete.correct_ruling_rate.numerator == 2
    assert report.complete.correct_ruling_rate.denominator == 2
    assert report.complete.wrong_allow_rate.numerator == 0
    assert len(report.outcomes) == 4
    assert report.excluded_reviews["draft"][0]["report"] == ""
    assert "N/A" not in report.to_markdown()


def test_score_results_reports_na_for_empty_metric_denominators():
    case = EvaluationCase.model_validate(_case("legal", "family-legal"))
    case.initial_label = "LEGAL"
    case.complete_label = "LEGAL"
    report = score_results(
        CaseDataset(dataset_version="trial-1", cases=[case]),
        [_outcome("legal", "LEGAL", "LEGAL")],
    )

    assert report.initial.correct_refusal_rate.value is None
    assert "N/A" in report.to_markdown()


def test_evaluation_cli_validates_datasets_and_views_exported_reports(tmp_path, capsys):
    dataset_path = tmp_path / "cases.json"
    dataset_path.write_text(
        json.dumps(
            {"dataset_version": "candidate-v1", "cases": [_case("cli-1", "family-cli")]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from rulecourt.evaluation import main

    assert main(["validate", str(dataset_path)]) == 0
    assert '"verified_count": 1' in capsys.readouterr().out

    report = score_results(
        CaseDataset.from_json(dataset_path),
        [_outcome("cli-1", "INSUFFICIENT_INFORMATION", "LEGAL")],
    )
    report_path = tmp_path / "report.json"
    report.save_json(report_path)
    assert main(["report", str(report_path)]) == 0
    assert "Complete investigation" in capsys.readouterr().out


def test_runner_score_and_export_form_one_evaluation_pipeline(tmp_path):
    case = EvaluationCase.model_validate(_case("pipeline-1", "family-pipeline"))
    adapter = _ScriptedCaseAdapter(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {"field": "clearings.A.adjacent_to", "question": "Which clearings?"}
                ],
            },
            {"status": "LEGAL", "reason": "MOVE_LEGAL"},
        ]
    )

    outcome = EvaluationRunner(adapter).run_case(case)
    report = score_results(
        CaseDataset(dataset_version="trial-1", cases=[case]),
        [outcome],
    )
    report_path = tmp_path / "pipeline-report.json"
    report.save_json(report_path)

    loaded = EvaluationReport.from_json(report_path)
    assert loaded.outcomes[0].case_id == case.id
    assert loaded.complete.correct_ruling_rate.numerator == 1


class _ScriptedCaseAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.messages = []

    def create_case(self):
        return "runtime-case-1"

    def submit_message(self, case_id, text):
        assert case_id == "runtime-case-1"
        self.messages.append(text)
        return self.responses.pop(0)


def test_evaluation_runner_replays_only_requested_facts_and_keeps_first_result():
    case = EvaluationCase.model_validate(_case("replay-1", "family-replay"))
    adapter = _ScriptedCaseAdapter(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {"field": "clearings.A.adjacent_to", "question": "Which clearings?"}
                ],
                "investigation": {"usage": {"iterations": 1}, "run_count": 1},
            },
            {
                "status": "LEGAL",
                "reason": "MOVE_LEGAL",
                "investigation": {"usage": {"iterations": 2}, "run_count": 2},
            },
        ]
    )

    result = EvaluationRunner(adapter, max_clarification_rounds=2).run_case(case)

    assert result.initial_result["status"] == "INSUFFICIENT_INFORMATION"
    assert result.complete_result["status"] == "LEGAL"
    assert result.clarification_rounds == 1
    assert result.run_count == 2
    assert result.usage["iterations"] == 2
    assert adapter.messages == [case.initial_input, "A 只与 B 相邻。"]
    assert result.fact_answers[0].requested_fields == ["clearings.A.adjacent_to"]
    assert "LEGAL" not in adapter.messages[0]


def test_evaluation_runner_stops_when_requested_fact_is_unknown():
    case_data = _case("replay-unknown", "family-replay-unknown")
    case_data["clarification_facts"] = {}
    case_data["fact_sources"] = {}
    case = EvaluationCase.model_validate(case_data)
    adapter = _ScriptedCaseAdapter(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {"field": "clearings.A.adjacent_to", "question": "Which clearings?"}
                ],
            }
        ]
    )

    result = EvaluationRunner(adapter).run_case(case)

    assert result.complete_result["status"] == "INSUFFICIENT_INFORMATION"
    assert result.fact_answers[0].status == "unknown"
    assert adapter.messages == [case.initial_input]


def test_evaluation_runner_denies_hidden_facts_when_no_question_target_is_allowed():
    case_data = _case("replay-denied", "family-replay-denied")
    case_data["acceptable_questions"] = []
    case = EvaluationCase.model_validate(case_data)
    adapter = _ScriptedCaseAdapter(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {"field": "clearings.A.adjacent_to", "question": "Which clearings?"}
                ],
            }
        ]
    )

    result = EvaluationRunner(adapter).run_case(case)

    assert result.fact_answers[0].status == "unknown"
    assert adapter.messages == [case.initial_input]


def test_initial_score_is_not_poisoned_by_a_later_complete_failure():
    case = EvaluationCase.model_validate(_case("late-failure", "family-late-failure"))
    case.initial_label = "LEGAL"
    case.complete_label = "UNRESOLVED"
    outcome = ReplayResult(
        case_id=case.id,
        family_id=case.family_id,
        category=case.category,
        initial_result={"status": "LEGAL", "reason": "MOVE_LEGAL"},
        complete_result={"status": "UNRESOLVED", "reason": "INVESTIGATION_FAILED"},
        initial_failure_reason=None,
        complete_failure_reason="INVESTIGATION_FAILED",
        failure_reason="INVESTIGATION_FAILED",
    )

    report = score_results(
        CaseDataset(dataset_version="trial-1", cases=[case]),
        [outcome],
    )

    assert report.initial.correct_ruling_rate.numerator == 1
    assert report.complete.correct_refusal_rate.numerator == 0


def _human_review() -> dict:
    return {
        "status": "verified",
        "reviewer_id": "root-reviewer-1",
        "rule_version": "root-law-2025-10",
        "reviewed_at": "2026-09-22T00:00:00Z",
        "basis": "Compared the case against the fixed Root ruleset independently of the engine.",
        "evidence": ["review-note:golden-1"],
        "report": "Labels, facts, evidence, and allowed questions were checked.",
        "checks": {
            "labels": True,
            "scope": True,
            "fact_availability": True,
            "evidence": True,
            "acceptable_questions": True,
            "independent_source": True,
        },
    }


def _signoff(
    dataset: CaseDataset,
    *,
    coverage_reviewed: list[str] | None = None,
    coverage_waivers: dict[str, str] | None = None,
) -> HumanSignoff:
    return HumanSignoff(
        dataset_version=dataset.dataset_version,
        dataset_digest=dataset.approval_digest(),
        approved_case_ids=[case.id for case in dataset.verified_cases],
        reviewer_ids=[
            case.review.reviewer_id for case in dataset.verified_cases if case.review.reviewer_id
        ],
        coverage_reviewed=coverage_reviewed or list(REQUIRED_COVERAGE_TAGS),
        coverage_waivers=coverage_waivers or {},
        signed_by="human-reviewer-1",
        signed_at="2026-09-22T01:00:00Z",
        approval_reference="t15-manual-signoff-1",
        signature="pending",
    ).seal(TEST_SIGNOFF_KEY)


def test_formal_scoring_requires_a_complete_human_review_and_family_split():
    cases = [
        EvaluationCase.model_validate(
            _case(
                f"formal-{tag}",
                f"family-formal-{tag}",
                coverage_tags=[tag],
            )
        )
        for tag in REQUIRED_COVERAGE_TAGS
    ]
    dataset = CaseDataset(dataset_version="golden-v1", cases=cases)

    with pytest.raises(ValueError, match="family split"):
        dataset.scoring_manifest()

    dataset.split_by_family(holdout_fraction=0.2, seed=11)
    with pytest.raises(ValueError, match="detached human signoff"):
        dataset.scoring_manifest()

    manifest = dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)

    assert manifest.dataset_version == "golden-v1"
    assert manifest.case_ids == [case.id for case in cases]
    assert manifest.disputed_case_ids == []
    assert manifest.human_signoff.approval_reference == "t15-manual-signoff-1"
    assert "initial_label" not in manifest.model_dump()


def test_formal_release_rejects_an_empty_holdout_partition():
    data = _case(
        "single-family",
        "family-single",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="holdout_families"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_unverified_cases_do_not_satisfy_formal_coverage():
    verified = _case("verified-no-tags", "family-verified-no-tags")
    draft = _case(
        "draft-coverage",
        "family-draft-coverage",
        review_status="draft",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(verified), EvaluationCase.model_validate(draft)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="coverage_cases"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_formal_gate_rejects_missing_or_wrong_signoff_key():
    data = _case(
        "signed-case",
        "family-signed-case",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    companion = _case("signed-companion", "family-signed-companion")
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data), EvaluationCase.model_validate(companion)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)
    signoff = _signoff(dataset)

    with pytest.raises(ValueError, match="signature"):
        dataset.scoring_manifest(signoff)
    with pytest.raises(ValueError, match="signature"):
        dataset.scoring_manifest(signoff, signing_key="wrong-key")


def test_formal_score_requires_split_and_detached_signoff():
    data = _case(
        "formal-score",
        "family-formal-score",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    case = EvaluationCase.model_validate(data)
    companion = EvaluationCase.model_validate(
        _case("formal-score-companion", "family-formal-score-companion")
    )
    dataset = CaseDataset(dataset_version="golden-v1", cases=[case, companion])
    outcomes = [
        _outcome("formal-score", "INSUFFICIENT_INFORMATION", "LEGAL"),
        _outcome("formal-score-companion", "INSUFFICIENT_INFORMATION", "LEGAL"),
    ]

    with pytest.raises(ValueError, match="family split"):
        score_results(dataset, outcomes, formal=True)

    dataset.split_by_family(holdout_fraction=0.2, seed=11)
    with pytest.raises(ValueError, match="detached human signoff"):
        score_results(dataset, outcomes, formal=True)

    report = score_results(
        dataset,
        outcomes,
        formal=True,
        signoff=_signoff(dataset),
        signing_key=TEST_SIGNOFF_KEY,
    )
    assert report.scored_case_count == 2


def test_verified_case_without_review_checklist_cannot_be_published():
    data = _case(
        "incomplete-review",
        "family-incomplete",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    data["review"] = {
        "status": "verified",
        "reviewer_id": "root-reviewer-1",
        "basis": "A note exists.",
        "evidence": ["review-note:incomplete"],
        "report": "The checklist was not completed.",
    }
    companion = _case("complete-review", "family-complete-review")
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data), EvaluationCase.model_validate(companion)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="checks"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_engine_output_cannot_be_the_only_verified_source():
    data = _case(
        "engine-only",
        "family-engine-only",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    data["label_source"] = "engine-output"
    companion = _case("engine-companion", "family-engine-companion")
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data), EvaluationCase.model_validate(companion)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="independent_label_source"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_formal_provenance_kind_must_match_the_field_using_it():
    data = _case(
        "wrong-provenance-kind",
        "family-wrong-provenance-kind",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    data["source_provenance"][1]["kind"] = "official_rule"
    companion = _case("provenance-companion", "family-provenance-companion")
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data), EvaluationCase.model_validate(companion)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="source_kind"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_formal_provenance_rejects_one_source_used_for_multiple_roles():
    data = _case(
        "ambiguous-provenance",
        "family-ambiguous-provenance",
        coverage_tags=list(REQUIRED_COVERAGE_TAGS),
    )
    shared = "reviewed:shared-source"
    data["label_source"] = shared
    data["clarification_facts"]["clearings.A.adjacent_to"]["source"] = shared
    data["fact_sources"]["clearings.A.adjacent_to"] = shared
    data["evidence"] = [shared]
    data["review"]["evidence"] = [shared]
    data["source_provenance"] = [{"source_id": shared, "kind": "human_review"}]
    companion = _case("ambiguous-provenance-companion", "family-ambiguous-provenance-companion")
    dataset = CaseDataset(
        dataset_version="golden-v1",
        cases=[EvaluationCase.model_validate(data), EvaluationCase.model_validate(companion)],
    )
    dataset.split_by_family(holdout_fraction=0.2, seed=11)

    with pytest.raises(ValueError, match="source_kind"):
        dataset.scoring_manifest(_signoff(dataset), signing_key=TEST_SIGNOFF_KEY)


def test_disputed_case_requires_a_reviewer():
    data = _case("disputed-no-reviewer", "family-disputed-no-reviewer", "disputed")
    data["review"] = {
        "status": "disputed",
        "rule_version": "root-law-2025-10",
        "reviewed_at": "2026-09-22T00:00:00Z",
        "basis": "The source is ambiguous.",
        "evidence": ["review-note:disputed"],
        "report": "The case needs adjudication.",
        "dispute_reasons": ["ambiguous_source_text"],
    }

    with pytest.raises(ValueError, match="reviewer_id"):
        EvaluationCase.model_validate(data)


def test_human_signoff_rejects_conflicting_coverage_attestations():
    with pytest.raises(ValueError, match="both reviewed and waived"):
        HumanSignoff(
            dataset_version="golden-v1",
            dataset_digest="0" * 64,
            approved_case_ids=[],
            reviewer_ids=[],
            coverage_reviewed=["partial"],
            coverage_waivers={"partial": "not in scope"},
            signed_by="human-reviewer-1",
            signed_at="2026-09-22T01:00:00Z",
            approval_reference="t15-conflict",
            signature="pending",
        )


def test_case_correction_history_and_dispute_reasons_round_trip(tmp_path):
    data = _case("disputed-1", "family-disputed", "disputed")
    data["review"] = {
        "status": "disputed",
        "reviewer_id": "root-reviewer-2",
        "rule_version": "root-law-2025-10",
        "basis": "The source text is ambiguous.",
        "evidence": ["review-note:disputed-1"],
        "report": "The original wording does not identify whether the list is complete.",
        "dispute_reasons": ["ambiguous_source_text"],
    }
    data["history"] = [
        {
            "revision": 1,
            "kind": "correction",
            "recorded_at": "2026-09-21T00:00:00Z",
            "reason": "Corrected the original transcription and retained the old wording.",
            "changes": {"initial_input": {"old": "old wording", "new": "new wording"}},
        }
    ]
    path = tmp_path / "dataset.json"
    path.write_text(
        json.dumps({"dataset_version": "golden-v1", "cases": [data]}, ensure_ascii=False),
        encoding="utf-8",
    )

    dataset = CaseDataset.from_json(path)
    case = dataset.cases[0]

    assert case.history[0].reason.startswith("Corrected")
    assert case.review.dispute_reasons == ["ambiguous_source_text"]
    assert dataset.excluded_counts == {"draft": 0, "disputed": 1}


def test_scoring_manifest_cli_is_versioned_and_contains_no_gold_labels(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("RULECOURT_GOLDEN_SIGNOFF_KEY", TEST_SIGNOFF_KEY)
    cases = [
        EvaluationCase.model_validate(
            _case(
                f"manifest-{tag}",
                f"family-manifest-{tag}",
                coverage_tags=[tag],
            )
        )
        for tag in REQUIRED_COVERAGE_TAGS
    ]
    dataset = CaseDataset(dataset_version="golden-v2", cases=cases)
    dataset.split_by_family(holdout_fraction=0.5, seed=3)
    dataset_path = tmp_path / "golden.json"
    dataset.save_json(dataset_path)
    signoff_path = tmp_path / "golden-signoff.json"
    signoff_path.write_text(
        json.dumps(_signoff(dataset).model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )

    from rulecourt.evaluation import main

    assert (
        main(
            [
                "validate",
                str(dataset_path),
                "--formal",
                "--signoff",
                str(signoff_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["manifest", str(dataset_path), "--signoff", str(signoff_path)]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dataset_version"] == "golden-v2"
    assert payload["case_ids"] == [case.id for case in cases]
    assert payload["split"]["holdout_families"]
    assert payload["human_signoff"]["signed_by"] == "human-reviewer-1"
    assert "initial_label" not in payload
    assert "complete_label" not in payload


def test_family_split_rejects_a_family_crossing_partitions():
    first = EvaluationCase.model_validate(_case("crossing-1", "same-family"))
    second = EvaluationCase.model_validate(_case("crossing-2", "same-family"))
    first.split = "development"
    second.split = "holdout"

    with pytest.raises(ValueError, match="family cannot cross"):
        CaseDataset(dataset_version="golden-v1", cases=[first, second])


def test_strategy_payload_does_not_expose_evaluation_labels_or_review_records():
    case = EvaluationCase.model_validate(_case("strategy-1", "family-strategy"))

    payload = case.strategy_payload()

    assert payload == {"id": "strategy-1", "initial_input": case.initial_input}
    assert "initial_label" not in payload
    assert "complete_label" not in payload
    assert "review" not in payload
    assert "clarification_facts" not in payload
