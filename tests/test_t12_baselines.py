import asyncio
import json
from types import SimpleNamespace

import pytest

from rulecourt.baselines import (
    BaselineConfig,
    BaselineReport,
    LLMOnlyRunner,
    RuleCorpus,
    RuleDocument,
    VanillaVectorIndex,
    VanillaVectorRAGRunner,
    run_baselines,
)
from rulecourt.embeddings import EmbeddingBatch


class TestEmbeddings:
    __test__ = False
    model = "fixture-embedding-v1"

    async def embed(self, texts):
        return EmbeddingBatch(vectors=[[1.0, 0.0] for _ in texts], input_tokens=7)


def _rag(provider, corpus, **kwargs):
    return VanillaVectorRAGRunner(
        provider, index=VanillaVectorIndex(corpus, embedder=TestEmbeddings()), **kwargs
    )


from rulecourt.evaluation import CaseDataset, EvaluationCase


def _case(case_id: str = "case-1") -> EvaluationCase:
    return EvaluationCase.model_validate(
        {
            "id": case_id,
            "family_id": "family-1",
            "category": "ordinary",
            "scope": "local_move_conditions",
            "initial_input": "Move one warrior from A to B. A is adjacent to B.",
            "initial_label": "INSUFFICIENT_INFORMATION",
            "complete_label": "LEGAL",
            "label_source": "reviewed:root-m0-v1",
            "clarification_facts": {
                "clearings.A.presence.marquise.warriors": {
                    "value": 2,
                    "text": "Marquise has 2 warriors in A.",
                    "source": "golden:case-1",
                }
            },
            "fact_sources": {
                "clearings.A.presence.marquise.warriors": "golden:case-1",
            },
            "acceptable_questions": ["clearings.A.presence.marquise.warriors"],
            "evidence": ["root-4.2"],
            "review": {
                "status": "verified",
                "reviewer_id": "reviewer-1",
                "rule_version": "root-law-2025-10",
                "reviewed_at": "2026-09-22T00:00:00Z",
                "checks": {
                    "labels": True,
                    "scope": True,
                    "fact_availability": True,
                    "evidence": True,
                    "acceptable_questions": True,
                    "independent_source": True,
                },
                "basis": "Checked against the fixed Root ruleset.",
                "evidence": ["review-note:1"],
                "report": "Independent review completed.",
            },
        }
    )


class ScriptedProvider:
    provider_name = "test-provider"

    def __init__(self, responses, *, delay=0):
        self.responses = list(responses)
        self.delay = delay
        self.calls = []

    def get_default_model(self):
        return "test-model"

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        response = self.responses.pop(0)
        return SimpleNamespace(
            content=json.dumps(response),
            finish_reason="stop",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


def _legal_response(*, citation=None):
    payload = {"status": "LEGAL", "reason": "MOVE_LEGAL", "explanation": "The move is legal."}
    if citation is not None:
        payload["citations"] = citation
    return payload


def test_baselines_share_replay_contract_and_keep_hidden_labels_out_of_prompts():
    case = _case()
    dataset = CaseDataset(dataset_version="trial-v1", cases=[case])
    corpus = RuleCorpus(
        ruleset_id="root-law-2025-10",
        version="2025-10",
        documents=[
            RuleDocument(
                id="root-4.2",
                section="4.2",
                text="A faction may move warriors from one clearing to an adjacent clearing.",
            )
        ],
    )
    llm_provider = ScriptedProvider(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {
                        "field": "clearings.A.presence.marquise.warriors",
                        "question": "How many Marquise warriors are in A?",
                    }
                ],
            },
            _legal_response(),
        ]
    )
    rag_provider = ScriptedProvider(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {
                        "field": "clearings.A.presence.marquise.warriors",
                        "question": "How many Marquise warriors are in A?",
                    }
                ],
            },
            _legal_response(citation=["root-4.2"]),
        ]
    )

    report = run_baselines(
        dataset,
        {
            "llm_only": LLMOnlyRunner(llm_provider),
            "vanilla_rag": _rag(rag_provider, corpus),
        },
    )

    assert set(report.runs) == {"llm_only", "vanilla_rag"}
    assert report.runs["llm_only"].evaluation.complete.correct_ruling_rate.numerator == 1
    assert report.runs["vanilla_rag"].evaluation.complete.correct_ruling_rate.numerator == 1
    assert report.runs["vanilla_rag"].retrieval["index_version"]
    assert report.runs["vanilla_rag"].retrieval["corpus_hash"] == corpus.content_hash
    assert report.runs["llm_only"].usage["provider_calls"] == 2
    assert report.runs["llm_only"].usage["total_tokens"] == 30
    assert len(llm_provider.calls) == 2
    assert all(call["tools"] is None and call["tool_choice"] is None for call in llm_provider.calls)
    first_prompt = json.dumps(llm_provider.calls[0]["messages"])
    assert "complete_label" not in first_prompt
    assert "golden:case-1" not in first_prompt


def test_vanilla_rag_rejects_citation_errors_for_scoring():
    case = _case()
    corpus = RuleCorpus(
        ruleset_id="root-law-2025-10",
        version="2025-10",
        documents=[RuleDocument(id="root-4.2", text="Move warriors to an adjacent clearing.")],
    )
    provider = ScriptedProvider(
        [
            {
                "status": "LEGAL",
                "reason": "MOVE_LEGAL",
                "citations": ["not-in-retrieval"],
            }
        ]
    )

    result = _rag(provider, corpus).run_case(case)

    assert result.complete_result["status"] == "LEGAL"
    assert result.complete_result["citation_check"]["valid"] is False
    assert not result.system_failure


def test_holdout_cannot_be_run_during_baseline_development():
    provider = ScriptedProvider([_legal_response()])
    case = _case().model_copy(update={"split": "holdout"})
    with pytest.raises(ValueError, match="holdout"):
        LLMOnlyRunner(provider).run_case(case)
    assert provider.calls == []


def test_dense_retrieval_uses_embeddings_not_word_overlap():
    class SemanticEmbeddings:
        model = "semantic-fixture"

        async def embed(self, texts):
            vectors = {
                " A feline travels.": [1.0, 0.0],
                " move move": [0.0, 1.0],
                "move": [1.0, 0.0],
            }
            return EmbeddingBatch(vectors=[vectors[text] for text in texts], input_tokens=3)

    corpus = RuleCorpus(
        documents=[
            RuleDocument(id="semantic", text="A feline travels."),
            RuleDocument(id="lexical", text="move move"),
        ]
    )
    index = VanillaVectorIndex(corpus, embedder=SemanticEmbeddings())
    hits = asyncio.run(index.search("move", limit=1))
    assert [hit.rule_id for hit in hits] == ["semantic"]


def test_rule_version_is_present_in_the_model_prompt():
    provider = ScriptedProvider([_legal_response()])
    LLMOnlyRunner(provider).run_case(_case())
    assert "root-law-2025-10" in provider.calls[0]["messages"][0]["content"]


def test_budget_is_cumulative_across_clarification():
    provider = ScriptedProvider(
        [
            {
                "status": "INSUFFICIENT_INFORMATION",
                "reason": "INSUFFICIENT_INFORMATION",
                "clarification_questions": [
                    {"field": "clearings.A.presence.marquise.warriors", "question": "Count?"}
                ],
            },
            _legal_response(),
        ]
    )
    result = LLMOnlyRunner(provider, config=BaselineConfig(max_total_tokens=15)).run_case(_case())
    assert result.complete_result["failure_reason"] == "BUDGET_EXHAUSTED"
    assert result.usage["provider_calls"] == 1


def test_timeout_is_recorded_as_a_failure_and_not_a_correct_refusal():
    case = _case()
    provider = ScriptedProvider([], delay=0.05)
    config = BaselineConfig(timeout_seconds=0.001)

    result = LLMOnlyRunner(provider, config=config).run_case(case)

    assert result.complete_result["status"] == "UNRESOLVED"
    assert result.complete_result["failure_reason"] == "INVESTIGATION_TIMEOUT"
    assert result.system_failure


@pytest.mark.parametrize(
    "actual,expected,reason,correct,wrong",
    [
        ("LEGAL", "LEGAL", "MOVE_LEGAL", 1, 0),
        ("ILLEGAL", "ILLEGAL", "MOVE_ILLEGAL", 1, 0),
        ("LEGAL", "ILLEGAL", "MOVE_LEGAL", 0, 1),
        ("UNRESOLVED", "UNRESOLVED", "UNSUPPORTED_SCOPE", 0, 0),
    ],
)
def test_shared_report_preserves_rulings_and_scores_citations_separately(
    tmp_path, actual, expected, reason, correct, wrong
):
    case = _case().model_copy(
        update={
            "initial_label": expected,
            "complete_label": expected,
            "initial_reason": reason,
            "complete_reason": reason,
        }
    )
    corpus = RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move.")])
    response = {"status": actual, "reason": reason}
    report = run_baselines(
        CaseDataset(dataset_version="synthetic-v1", cases=[case]),
        {
            "llm_only": LLMOnlyRunner(ScriptedProvider([response])),
            "vanilla_rag": _rag(
                ScriptedProvider([{**response, "citations": ["invented"]}]), corpus
            ),
        },
    )
    rag = report.runs["vanilla_rag"]
    assert rag.evaluation.complete.correct_ruling_rate.numerator == correct
    assert rag.evaluation.complete.wrong_allow_rate.numerator == wrong
    if actual in {"LEGAL", "ILLEGAL"}:
        assert rag.citation_error_count == 1
        assert rag.evidence["complete_result"]["valid_citation_count"] == 0
    else:
        assert rag.evaluation.complete.correct_refusal_rate.numerator == 1
    assert rag.usage["embedding_tokens"] == 14
    assert rag.usage["total_tokens"] == 29
    assert rag.usage["cost_usd"] is None
    path = tmp_path / "report.json"
    report.save_json(path)
    assert BaselineReport.from_json(path).to_dict() == report.to_dict()
    assert "not by themselves evidence" in report.to_markdown()


def test_embedding_failure_is_bounded_and_counted():
    class BrokenEmbeddings:
        model = "broken"

        async def embed(self, texts):
            raise RuntimeError("embedding service failed")

    corpus = RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move.")])
    provider = ScriptedProvider([])
    runner = VanillaVectorRAGRunner(provider, corpus=corpus, embedder=BrokenEmbeddings())
    result = runner.run_case(_case())
    runner.close()
    assert result.complete_result["failure_reason"] == "RETRIEVAL_ERROR"
    assert result.usage["embedding_calls"] == 1
    assert result.usage["unknown_usage_calls"] == 1
    assert result.usage["cost_usd"] is None
    assert provider.calls == []


def test_all_retrieval_rounds_and_only_requested_facts_remain_auditable():
    question = {
        "status": "INSUFFICIENT_INFORMATION",
        "reason": "INSUFFICIENT_INFORMATION",
        "clarification_questions": [
            {"field": "clearings.A.presence.marquise.warriors", "question": "Count?"}
        ],
    }
    provider = ScriptedProvider([question, question, _legal_response(citation=["root-4.2"])])
    corpus = RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move.")])
    runner = _rag(provider, corpus)
    result = runner.run_case(_case())
    runner.close()
    trace = result.complete_result["investigation"]["rounds"]
    assert len(trace) == 3
    assert result.initial_result["investigation"]["rounds"] == trace[:1]
    assert trace[1]["user_message"] == "Marquise has 2 warriors in A."
    assert all(item["retrieval"]["rule_ids"] == ["root-4.2"] for item in trace)
    assert all("golden:case-1" not in json.dumps(call["messages"]) for call in provider.calls)
    assert result.usage["provider_calls"] == 3
    assert result.usage["embedding_calls"] == 4


def test_cli_dry_run_is_reproducible_and_never_calls_a_provider(tmp_path, capsys):
    from rulecourt.baselines import main

    dataset_path = tmp_path / "dataset.json"
    CaseDataset(dataset_version="synthetic", cases=[_case()]).save_json(dataset_path)
    corpus_path = tmp_path / "rules.json"
    corpus_path.write_text(
        json.dumps(
            {
                "ruleset_id": "root-law-2025-10",
                "version": "2025-10",
                "documents": [{"id": "root-4.2", "text": "Move."}],
            }
        ),
        encoding="utf-8",
    )
    args = [
        str(dataset_path),
        "--rules",
        str(corpus_path),
        "--model",
        "test-model",
        "--embedding-model",
        "test-embedding",
        "--dry-run",
    ]
    assert main(args) == 0
    first = capsys.readouterr().out
    assert main(args) == 0
    assert capsys.readouterr().out == first
    assert json.loads(first)["config"]["embedding_model"] == "test-embedding"
