import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from test_t12_baselines import _case

from rulecourt.baselines import (
    BaselineConfig,
    HTTPRerankerProvider,
    HybridBaselineConfig,
    HybridRAGRunner,
    HybridRetriever,
    LexicalIndex,
    LLMOnlyRunner,
    RerankBatch,
    RuleCorpus,
    RuleDocument,
    VanillaVectorIndex,
    VanillaVectorRAGRunner,
    run_baselines,
)
from rulecourt.embeddings import EmbeddingBatch
from rulecourt.evaluation import CaseDataset


class FixtureEmbeddings:
    model = "fixture-embedding-v1"

    async def embed(self, texts):
        vectors = {
            "Move Move warriors from one clearing to an adjacent clearing.": [1.0, 0.0],
            "Build Build a building in a clearing.": [0.0, 1.0],
            "move warriors": [1.0, 0.0],
        }
        return EmbeddingBatch(vectors=[vectors[text] for text in texts], input_tokens=3)


class FixtureReranker:
    model = "fixture-reranker-v1"

    async def rerank(self, query, documents):
        del query
        assert len(documents) == 2
        return RerankBatch(scores=[0.2, 0.9], input_tokens=11)


class AnyEmbeddings:
    model = "fixture-embedding-v2"

    async def embed(self, texts):
        return EmbeddingBatch(vectors=[[1.0, 0.0] for _ in texts], input_tokens=4)


class OrthogonalEmbeddings:
    model = "fixture-orthogonal-v1"

    async def embed(self, texts):
        return EmbeddingBatch(
            vectors=[[0.0, 1.0] if text == "unrelated" else [1.0, 0.0] for text in texts],
            input_tokens=2,
        )


class CountingReranker:
    model = "fixture-reranker-v2"

    def __init__(self):
        self.calls = 0

    async def rerank(self, query, documents):
        del query
        self.calls += 1
        return RerankBatch(scores=[0.7 for _ in documents], input_tokens=10)


class ScriptedProvider:
    provider_name = "test-provider"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get_default_model(self):
        return "test-model"

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=json.dumps(self.responses.pop(0)),
            finish_reason="stop",
            usage=SimpleNamespace(input_tokens=10, output_tokens=5, total_tokens=15),
        )


def _legal_response():
    return {
        "status": "LEGAL",
        "reason": "MOVE_LEGAL",
        "explanation": "The move is legal.",
        "citations": ["root-4.2"],
    }


def test_hybrid_retriever_merges_duplicates_and_round_trips_scores():
    corpus = RuleCorpus(
        documents=[
            RuleDocument(
                id="move",
                title="Move",
                text="Move warriors from one clearing to an adjacent clearing.",
            ),
            RuleDocument(id="build", title="Build", text="Build a building in a clearing."),
        ]
    )
    retriever = HybridRetriever(
        lexical=LexicalIndex(corpus),
        vector=VanillaVectorIndex(corpus, embedder=FixtureEmbeddings()),
        reranker=FixtureReranker(),
        vector_min_score=-1.0,
    )

    hits = asyncio.run(retriever.search("move warriors", limit=2))

    assert [hit.rule_id for hit in hits] == ["build", "move"]
    assert len({hit.rule_id for hit in hits}) == 2
    move = next(hit for hit in hits if hit.rule_id == "move")
    assert move.retrieval_sources == ["lexical", "vector"]
    assert move.lexical_rank == 1
    assert move.vector_rank == 1
    assert move.rerank_score == 0.2
    assert hits[0].rerank_score == 0.9
    assert hits[0].rank == 1
    assert hits[1].rank == 2


def test_hybrid_runner_uses_shared_replay_and_reports_reranker_usage(tmp_path):
    case = _case()
    dataset = CaseDataset(dataset_version="t13-trial", cases=[case])
    corpus = RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move warriors.")])
    config = BaselineConfig(
        model="test-model",
        provider="test-provider",
        embedding_model="fixture-embedding-v2",
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=2.0,
        embedding_cost_per_1k_tokens=3.0,
    )
    hybrid_config = HybridBaselineConfig(
        model="test-model",
        provider="test-provider",
        embedding_model="fixture-embedding-v2",
        reranker_model="fixture-reranker-v2",
        input_cost_per_1k_tokens=1.0,
        output_cost_per_1k_tokens=2.0,
        embedding_cost_per_1k_tokens=3.0,
        reranker_cost_per_1k_tokens=4.0,
    )
    llm = ScriptedProvider(
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
    vanilla = ScriptedProvider(
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
    hybrid = ScriptedProvider(
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
    reranker = CountingReranker()
    report = run_baselines(
        dataset,
        {
            "llm_only": LLMOnlyRunner(llm, config=config),
            "vanilla_rag": VanillaVectorRAGRunner(
                vanilla,
                corpus=corpus,
                embedder=AnyEmbeddings(),
                config=config,
            ),
            "hybrid_rag": HybridRAGRunner(
                hybrid,
                corpus=corpus,
                embedder=AnyEmbeddings(),
                reranker=reranker,
                config=hybrid_config,
            ),
        },
    )

    assert report.schema_version == "t13-baseline-report-v1"
    assert set(report.runs) == {"llm_only", "vanilla_rag", "hybrid_rag"}
    hybrid_run = report.runs["hybrid_rag"]
    assert hybrid_run.evaluation.complete.correct_ruling_rate.numerator == 1
    assert hybrid_run.config["reranker_model"] == "fixture-reranker-v2"
    assert hybrid_run.retrieval["mode"] == "hybrid_rag"
    assert hybrid_run.retrieval["reranker_model"] == "fixture-reranker-v2"
    assert hybrid_run.usage["reranker_calls"] == 2
    assert hybrid_run.usage["reranker_tokens"] == 20
    assert hybrid_run.usage["cost_usd"] is not None
    assert len(hybrid_run.results) == 1
    assert hybrid_run.results[0]["initial_result"]["status"] == "INSUFFICIENT_INFORMATION"
    assert hybrid_run.results[0]["complete_result"]["status"] == "LEGAL"
    assert "complete_label" not in json.dumps(hybrid.calls)
    report_path = tmp_path / "t13-report.json"
    report.save_json(report_path)
    assert report.from_json(report_path).to_dict() == report.to_dict()


def test_hybrid_retriever_classifies_empty_query_without_calling_reranker():
    reranker = CountingReranker()
    corpus = RuleCorpus(documents=[RuleDocument(id="rule", text="Move warriors.")])
    retriever = HybridRetriever(
        lexical=LexicalIndex(corpus),
        vector=VanillaVectorIndex(corpus, embedder=AnyEmbeddings()),
        reranker=reranker,
    )

    assert asyncio.run(retriever.search("   ")) == []
    assert reranker.calls == 0


def test_hybrid_retriever_reports_ordinary_no_hit_without_reranking():
    reranker = CountingReranker()
    corpus = RuleCorpus(documents=[RuleDocument(id="rule", text="Move warriors.")])
    retriever = HybridRetriever(
        lexical=LexicalIndex(corpus),
        vector=VanillaVectorIndex(corpus, embedder=OrthogonalEmbeddings()),
        reranker=reranker,
    )

    assert asyncio.run(retriever.search("unrelated")) == []
    assert retriever.last_trace["candidate_count"] == 0
    assert reranker.calls == 0


def test_http_reranker_reorders_indexed_results_and_records_usage(monkeypatch):
    captured = []

    def serve(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.2},
                    {"index": 0, "relevance_score": 0.9},
                ],
                "usage": {"prompt_tokens": 13},
            },
        )

    original_client = httpx.AsyncClient
    monkeypatch.setenv("T13_TEST_KEY", "synthetic-test-key")
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(serve), **kwargs),
    )
    provider = HTTPRerankerProvider(
        model="fixed-reranker-v1",
        base_url="https://fixture.invalid/v1",
        api_key_env="T13_TEST_KEY",
    )

    result = asyncio.run(provider.rerank("move", ["first", "second"]))

    assert result.scores == [0.9, 0.2]
    assert result.input_tokens == 13
    assert captured == [
        {
            "model": "fixed-reranker-v1",
            "query": "move",
            "documents": ["first", "second"],
            "top_n": 2,
            "return_documents": False,
        }
    ]


def test_reranker_failure_is_unresolved_retrieval_error_without_provider_call():
    class BrokenReranker:
        model = "broken-reranker"

        async def rerank(self, query, documents):
            del query, documents
            raise RuntimeError("reranker unavailable")

    provider = ScriptedProvider([])
    runner = HybridRAGRunner(
        provider,
        corpus=RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move warriors.")]),
        embedder=AnyEmbeddings(),
        reranker=BrokenReranker(),
    )

    result = runner.run_case(_case())
    runner.close()

    assert result.complete_result["status"] == "UNRESOLVED"
    assert result.complete_result["failure_reason"] == "RETRIEVAL_ERROR"
    assert result.complete_result["retrieval"]["mode"] == "hybrid_rag"
    assert provider.calls == []


def test_t13_rejects_review_rule_version_before_running_providers():
    base_case = _case()
    mismatched_review = base_case.review.model_copy(update={"rule_version": "root-law-2024"})
    case = base_case.model_copy(update={"review": mismatched_review})
    dataset = CaseDataset(dataset_version="t13-version-gate", cases=[case])
    corpus = RuleCorpus(documents=[RuleDocument(id="root-4.2", text="Move warriors.")])
    config = BaselineConfig(model="test-model", provider="test-provider")
    hybrid_config = HybridBaselineConfig(model="test-model", provider="test-provider")
    llm = ScriptedProvider([])
    vanilla = ScriptedProvider([])
    hybrid = ScriptedProvider([])
    runners = {
        "llm_only": LLMOnlyRunner(llm, config=config),
        "vanilla_rag": VanillaVectorRAGRunner(
            vanilla, corpus=corpus, embedder=AnyEmbeddings(), config=config
        ),
        "hybrid_rag": HybridRAGRunner(
            hybrid,
            corpus=corpus,
            embedder=AnyEmbeddings(),
            reranker=CountingReranker(),
            config=hybrid_config,
        ),
    }

    with pytest.raises(ValueError, match="review.*rule versions"):
        run_baselines(dataset, runners)

    assert llm.calls == []
    assert vanilla.calls == []
    assert hybrid.calls == []
    for runner in runners.values():
        runner.close()


def test_t13_cli_dry_run_lists_hybrid_strategy(tmp_path, capsys):
    from rulecourt.baselines import main

    dataset_path = tmp_path / "dataset.json"
    CaseDataset(dataset_version="t13-cli", cases=[_case()]).save_json(dataset_path)
    corpus_path = tmp_path / "rules.json"
    corpus_path.write_text(
        json.dumps(
            {
                "ruleset_id": "root-law-2025-10",
                "version": "2025-10",
                "documents": [{"id": "root-4.2", "text": "Move warriors."}],
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                str(dataset_path),
                "--rules",
                str(corpus_path),
                "--embedding-model",
                "fixture-embedding-v2",
                "--reranker-model",
                "fixture-reranker-v2",
                "--dry-run",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["strategies"] == ["llm_only", "vanilla_rag", "hybrid_rag"]
    assert payload["config"]["reranker_model"] == "fixture-reranker-v2"
