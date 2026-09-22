"""Hybrid lexical/vector retrieval with an auditable reranker adapter.

This module is intentionally layered on the T12 baseline runner.  Retrieval
only supplies public rule excerpts; it never imports the Root adapter, a
coverage table, or evaluation labels.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import sys
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import ConfigDict, Field, field_validator, model_validator

from . import baselines as _legacy
from .embeddings import EmbeddingProvider, HTTPEmbeddingProvider

_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class RerankBatch(_legacy.BaseModel):
    """Scores aligned to the candidate documents supplied to a reranker."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    scores: list[float]
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("scores")
    @classmethod
    def finite_scores(cls, value: list[float]) -> list[float]:
        if any(not math.isfinite(score) for score in value):
            raise ValueError("reranker scores must be finite")
        return value

    @model_validator(mode="after")
    def normalize_total_tokens(self) -> RerankBatch:
        if self.total_tokens is None and (
            self.input_tokens is not None or self.output_tokens is not None
        ):
            object.__setattr__(
                self,
                "total_tokens",
                (self.input_tokens or 0) + (self.output_tokens or 0),
            )
        return self

    @property
    def token_count(self) -> int | None:
        return self.total_tokens


class RerankerProvider(Protocol):
    model: str

    async def rerank(self, query: str, documents: list[str]) -> RerankBatch: ...


class HTTPRerankerProvider:
    """Call a Cohere/OpenAI-compatible JSON ``/rerank`` endpoint."""

    def __init__(self, *, model: str, base_url: str, api_key_env: str = "OPENAI_API_KEY"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env

    async def rerank(self, query: str, documents: list[str]) -> RerankBatch:
        endpoint = self.base_url if self.base_url.endswith("/rerank") else self.base_url + "/rerank"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                    "top_n": len(documents),
                    "return_documents": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, Mapping):
            raise TypeError("reranker response must be an object")
        scores = _aligned_rerank_scores(payload, len(documents))
        usage = payload.get("usage")
        if not isinstance(usage, Mapping):
            usage = {}
        return RerankBatch(
            scores=scores,
            input_tokens=_optional_nonnegative_int(
                usage.get("input_tokens", usage.get("prompt_tokens"))
            ),
            output_tokens=_optional_nonnegative_int(
                usage.get("output_tokens", usage.get("completion_tokens"))
            ),
            total_tokens=_optional_nonnegative_int(usage.get("total_tokens")),
            cost_usd=_optional_nonnegative_float(usage.get("cost_usd", usage.get("cost"))),
        )


class LexicalIndex:
    """Deterministic BM25-style search over one immutable public rule corpus."""

    def __init__(
        self,
        corpus: _legacy.RuleCorpus,
        *,
        index_version: str = "lexical-bm25-v1",
        k1: float = 1.2,
        b: float = 0.75,
    ):
        self.corpus = corpus.model_copy(deep=True)
        self.index_version = index_version.strip()
        if not self.index_version:
            raise ValueError("index_version must not be empty")
        if k1 < 0 or b < 0 or b > 1:
            raise ValueError("BM25 parameters must be non-negative and b must be at most one")
        self.k1 = float(k1)
        self.b = float(b)
        self.search_calls = 0
        self._tokens = [_tokens(f"{doc.title} {doc.text}") for doc in self.corpus.documents]
        self._lengths = [len(tokens) for tokens in self._tokens]
        self._average_length = sum(self._lengths) / max(len(self._lengths), 1)
        self._document_frequency: dict[str, int] = {}
        for tokens in self._tokens:
            for term in set(tokens):
                self._document_frequency[term] = self._document_frequency.get(term, 0) + 1

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "ruleset_id": self.corpus.ruleset_id,
            "ruleset_version": self.corpus.version,
            "corpus_hash": self.corpus.content_hash,
            "document_count": len(self.corpus.documents),
            "tokenizer": "unicode-word-v1",
            "similarity": "bm25",
            "k1": self.k1,
            "b": self.b,
            "cache": "build-once-per-runner",
        }

    async def search(self, query: str, *, limit: int = 4) -> list[_legacy.RetrievedRule]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        self.search_calls += 1
        query_terms = _tokens(query)
        if not query_terms:
            return []
        document_count = len(self.corpus.documents)
        query_frequency = {term: query_terms.count(term) for term in set(query_terms)}
        scored: list[tuple[float, int]] = []
        for index, terms in enumerate(self._tokens):
            if not terms:
                continue
            term_frequency: dict[str, int] = {}
            for term in terms:
                term_frequency[term] = term_frequency.get(term, 0) + 1
            length = len(terms)
            score = 0.0
            for term, query_count in query_frequency.items():
                frequency = term_frequency.get(term, 0)
                if not frequency:
                    continue
                document_frequency = self._document_frequency.get(term, 0)
                idf = math.log(
                    1.0 + (document_count - document_frequency + 0.5) / (document_frequency + 0.5)
                )
                denominator = frequency + self.k1 * (
                    1.0 - self.b + self.b * length / max(self._average_length, 1.0)
                )
                score += idf * (frequency * (self.k1 + 1.0) / denominator) * min(query_count, 2)
            if score > 0:
                scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], self.corpus.documents[item[1]].id))
        return [
            _legacy.RetrievedRule(
                rule_id=self.corpus.documents[index].id,
                section=self.corpus.documents[index].section,
                title=self.corpus.documents[index].title,
                text=self.corpus.documents[index].text,
                source=self.corpus.documents[index].source,
                score=score,
                rank=rank,
            )
            for rank, (score, index) in enumerate(scored[:limit], start=1)
        ]


class HybridRetrievedRule(_legacy.BaseModel):
    """A public rule candidate with both fusion and reranker provenance."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    rule_id: str
    section: str = ""
    title: str = ""
    text: str
    source: str = ""
    score: float
    rank: int = Field(ge=1)
    fusion_rank: int = Field(default=1, ge=1)
    fusion_score: float
    rerank_score: float | None = None
    lexical_score: float | None = None
    vector_score: float | None = None
    lexical_rank: int | None = Field(default=None, ge=1)
    vector_rank: int | None = Field(default=None, ge=1)
    rerank_rank: int | None = Field(default=None, ge=1)
    retrieval_sources: list[str] = Field(default_factory=list)


RetrievedHybridRule = HybridRetrievedRule
HybridRule = HybridRetrievedRule


class HybridRetriever:
    """Merge lexical/vector candidates and hide reranker protocol details."""

    def __init__(
        self,
        corpus: _legacy.RuleCorpus | Mapping[str, Any] | None = None,
        *,
        lexical: LexicalIndex | None = None,
        vector: _legacy.VanillaVectorIndex | None = None,
        lexical_index: LexicalIndex | None = None,
        vector_index: _legacy.VanillaVectorIndex | None = None,
        reranker: RerankerProvider | None = None,
        embedder: EmbeddingProvider | None = None,
        index_version: str = "hybrid-rag-v1",
        lexical_k: int = 4,
        vector_k: int = 4,
        candidate_k: int = 8,
        rrf_k: int = 60,
        lexical_weight: float = 1.0,
        vector_weight: float = 1.0,
        vector_min_score: float = 0.0,
    ):
        lexical = lexical or lexical_index
        vector = vector or vector_index
        if lexical is None or vector is None:
            if corpus is None:
                raise ValueError("HybridRetriever requires a corpus or both indexes")
            corpus_model = _legacy.RuleCorpus.model_validate(corpus)
            lexical = lexical or LexicalIndex(corpus_model)
            if vector is None:
                if embedder is None:
                    raise ValueError("a configured embedding provider is required")
                vector = _legacy.VanillaVectorIndex(corpus_model, embedder=embedder)
        assert lexical is not None
        assert vector is not None
        corpus_model = (
            _legacy.RuleCorpus.model_validate(corpus) if corpus is not None else lexical.corpus
        )
        if lexical.corpus.content_hash != corpus_model.content_hash:
            raise ValueError("lexical index and corpus versions differ")
        if vector.corpus.content_hash != corpus_model.content_hash:
            raise ValueError("vector index and corpus versions differ")
        if not index_version.strip():
            raise ValueError("index_version must not be empty")
        for name, value in (
            ("lexical_k", lexical_k),
            ("vector_k", vector_k),
            ("candidate_k", candidate_k),
            ("rrf_k", rrf_k),
        ):
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if lexical_weight < 0 or vector_weight < 0 or lexical_weight + vector_weight <= 0:
            raise ValueError("retrieval weights must be non-negative and not both zero")
        if not math.isfinite(vector_min_score):
            raise ValueError("vector_min_score must be finite")
        self.corpus = corpus_model.model_copy(deep=True)
        self.lexical = lexical
        self.vector = vector
        self.reranker = reranker
        self.index_version = index_version.strip()
        self.lexical_k = int(lexical_k)
        self.vector_k = int(vector_k)
        self.candidate_k = int(candidate_k)
        self.rrf_k = int(rrf_k)
        self.lexical_weight = float(lexical_weight)
        self.vector_weight = float(vector_weight)
        self.vector_min_score = float(vector_min_score)
        self.reranker_calls = 0
        self.reranker_input_tokens = 0
        self.reranker_output_tokens = 0
        self.reranker_tokens = 0
        self.reranker_unknown_usage_calls = 0
        self.reranker_usage_available = False
        self.reranker_reported_cost = 0.0
        self.reranker_reported_cost_calls = 0
        self.last_trace: dict[str, Any] = {}

    @property
    def metadata(self) -> dict[str, Any]:
        provider_name = (
            getattr(self.reranker, "provider_name", type(self.reranker).__name__)
            if self.reranker is not None
            else None
        )
        return {
            "index_version": self.index_version,
            "ruleset_id": self.corpus.ruleset_id,
            "ruleset_version": self.corpus.version,
            "corpus_hash": self.corpus.content_hash,
            "document_count": len(self.corpus.documents),
            "mode": "hybrid_rag",
            "fusion": "reciprocal-rank-fusion-v1",
            "rrf_k": self.rrf_k,
            "lexical_weight": self.lexical_weight,
            "vector_weight": self.vector_weight,
            "vector_min_score": self.vector_min_score,
            "lexical_k": self.lexical_k,
            "vector_k": self.vector_k,
            "candidate_k": self.candidate_k,
            "lexical_index": self.lexical.metadata,
            "vector_index": self.vector.metadata,
            "embedding_model": self.vector.embedder.model,
            "reranker_model": getattr(self.reranker, "model", None),
            "reranker_provider": provider_name,
            "reranker": "external-score-only-v1" if self.reranker is not None else None,
            "cache": "build-once-per-runner",
        }

    async def search(self, query: str, *, limit: int = 4) -> list[HybridRetrievedRule]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not query.strip():
            self.last_trace = {
                "query": query,
                "lexical_rule_ids": [],
                "vector_rule_ids": [],
                "candidate_rule_ids": [],
                "candidate_count": 0,
                "reranker_candidate_count": 0,
                "candidates": [],
                "reranked": False,
            }
            return []
        lexical_hits, vector_hits = await asyncio.gather(
            self.lexical.search(query, limit=self.lexical_k),
            self.vector.search(query, limit=self.vector_k),
        )
        vector_hits = [hit for hit in vector_hits if hit.score > self.vector_min_score]
        merged = self._merge(lexical_hits, vector_hits)
        candidates = merged[: self.candidate_k]
        if not candidates:
            self.last_trace = {
                "query": query,
                "lexical_rule_ids": [hit.rule_id for hit in lexical_hits],
                "vector_rule_ids": [hit.rule_id for hit in vector_hits],
                "candidate_rule_ids": [],
                "candidate_count": 0,
                "reranker_candidate_count": 0,
                "candidates": [],
                "reranked": False,
            }
            return []
        reranked = False
        if self.reranker is not None:
            await self._apply_reranker(query, candidates)
            candidates.sort(
                key=lambda item: (
                    -(item.rerank_score if item.rerank_score is not None else float("-inf")),
                    -item.fusion_score,
                    item.rule_id,
                )
            )
            reranked = True
        results: list[HybridRetrievedRule] = []
        for rank, candidate in enumerate(candidates[:limit], start=1):
            candidate.rank = rank
            candidate.score = (
                candidate.rerank_score
                if candidate.rerank_score is not None
                else candidate.fusion_score
            )
            results.append(candidate)
        self.last_trace = {
            "query": query,
            "lexical_rule_ids": [hit.rule_id for hit in lexical_hits],
            "vector_rule_ids": [hit.rule_id for hit in vector_hits],
            "candidate_rule_ids": [hit.rule_id for hit in merged],
            "candidate_count": len(merged),
            "reranker_candidate_count": len(candidates) if reranked else 0,
            "candidates": [item.model_dump(mode="json") for item in merged],
            "reranked": reranked,
        }
        return results

    def _merge(
        self,
        lexical_hits: Sequence[_legacy.RetrievedRule],
        vector_hits: Sequence[_legacy.RetrievedRule],
    ) -> list[HybridRetrievedRule]:
        merged: dict[str, dict[str, Any]] = {}
        for source, hits in (("lexical", lexical_hits), ("vector", vector_hits)):
            for hit in hits:
                item = merged.setdefault(
                    hit.rule_id,
                    {
                        "rule_id": hit.rule_id,
                        "section": hit.section,
                        "title": hit.title,
                        "text": hit.text,
                        "source": hit.source,
                        "fusion_score": 0.0,
                        "rerank_score": None,
                        "lexical_score": None,
                        "vector_score": None,
                        "lexical_rank": None,
                        "vector_rank": None,
                        "retrieval_sources": [],
                    },
                )
                weight = self.lexical_weight if source == "lexical" else self.vector_weight
                item["fusion_score"] += weight / (self.rrf_k + hit.rank)
                if source == "lexical":
                    item["lexical_score"] = hit.score
                    item["lexical_rank"] = hit.rank
                else:
                    item["vector_score"] = hit.score
                    item["vector_rank"] = hit.rank
                if source not in item["retrieval_sources"]:
                    item["retrieval_sources"].append(source)
        candidates = [
            HybridRetrievedRule(score=item["fusion_score"], rank=1, **item)
            for item in merged.values()
        ]
        candidates.sort(key=lambda item: (-item.fusion_score, item.rule_id))
        for rank, candidate in enumerate(candidates, start=1):
            candidate.fusion_rank = rank
        return candidates

    async def _apply_reranker(self, query: str, candidates: list[HybridRetrievedRule]) -> None:
        assert self.reranker is not None
        self.reranker_calls += 1
        self.reranker_unknown_usage_calls += 1
        documents = [f"{candidate.title}\n{candidate.text}" for candidate in candidates]
        raw = await self.reranker.rerank(query, documents)
        batch = _coerce_rerank_batch(raw, len(candidates))
        self._record_reranker_usage(batch)
        for candidate, score in zip(candidates, batch.scores, strict=True):
            candidate.rerank_score = score
        for rank, candidate in enumerate(
            sorted(
                candidates,
                key=lambda item: (
                    -(item.rerank_score if item.rerank_score is not None else float("-inf")),
                    -item.fusion_score,
                    item.rule_id,
                ),
            ),
            start=1,
        ):
            candidate.rerank_rank = rank

    def _record_reranker_usage(self, batch: RerankBatch) -> None:
        if (
            batch.input_tokens is not None
            or batch.output_tokens is not None
            or batch.total_tokens is not None
        ):
            self.reranker_unknown_usage_calls -= 1
            self.reranker_usage_available = True
            self.reranker_input_tokens += batch.input_tokens or 0
            self.reranker_output_tokens += batch.output_tokens or 0
            self.reranker_tokens += batch.total_tokens or 0
        if batch.cost_usd is not None:
            self.reranker_reported_cost += batch.cost_usd
            self.reranker_reported_cost_calls += 1


class HybridBaselineConfig(_legacy.BaselineConfig):
    """T13 parameters in addition to the shared T12 generation budget."""

    model_config = ConfigDict(extra="forbid")

    lexical_k: int = 4
    vector_k: int = 4
    candidate_k: int = 8
    rrf_k: int = 60
    lexical_weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    vector_weight: float = Field(default=1.0, ge=0, allow_inf_nan=False)
    vector_min_score: float = Field(default=0.0, allow_inf_nan=False)
    reranker_model: str | None = None
    reranker_base_url: str = "https://api.openai.com/v1"
    reranker_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("lexical_k", "vector_k", "candidate_k", "rrf_k")
    @classmethod
    def positive_retrieval_integer(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("hybrid retrieval limits must be positive")
        return value

    @model_validator(mode="after")
    def require_retrieval_weight(self) -> HybridBaselineConfig:
        if self.lexical_weight + self.vector_weight <= 0:
            raise ValueError("at least one retrieval weight must be positive")
        return self


HybridRunConfig = HybridBaselineConfig


def _hybrid_usage(
    usage: Mapping[str, Any],
    runner: HybridRAGRunner,
    stats: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add reranker accounting to the T12 usage snapshot."""
    enriched = dict(usage)
    stats = stats or runner._last_reranker_usage
    for key, value in stats.items():
        enriched[key] = value
    reranker_calls = int(enriched.get("reranker_calls", 0))
    reranker_tokens = int(enriched.get("reranker_tokens", 0))
    reranker_cost = float(enriched.get("reranker_reported_cost_usd", 0.0))
    if (
        reranker_cost == 0
        and reranker_tokens
        and runner.config.reranker_cost_per_1k_tokens is not None
    ):
        reranker_cost = reranker_tokens * runner.config.reranker_cost_per_1k_tokens / 1000
    enriched["reranker_cost_usd"] = round(reranker_cost, 8)
    enriched["known_cost_usd"] = round(
        float(enriched.get("known_cost_usd", 0.0)) + reranker_cost, 8
    )
    base_cost_known = enriched.get("cost_usd") is not None
    reranker_price_known = (
        not reranker_calls
        or runner.config.reranker_cost_per_1k_tokens is not None
        or int(enriched.get("reranker_reported_cost_calls", 0)) == reranker_calls
    )
    unknown = int(enriched.get("unknown_usage_calls", 0))
    if base_cost_known and reranker_price_known and not unknown:
        enriched["cost_usd"] = round(float(enriched["known_cost_usd"]), 8)
    else:
        enriched["cost_usd"] = None
    return enriched


class HybridRAGRunner(_legacy._ProviderBaselineRunner):
    """T13 baseline using public lexical/vector candidates and a reranker."""

    strategy = "hybrid_rag"
    require_citations = True

    def __init__(
        self,
        provider: Any,
        *,
        corpus: _legacy.RuleCorpus | Mapping[str, Any] | None = None,
        retriever: HybridRetriever | None = None,
        lexical: LexicalIndex | None = None,
        vector: _legacy.VanillaVectorIndex | None = None,
        lexical_index: LexicalIndex | None = None,
        vector_index: _legacy.VanillaVectorIndex | None = None,
        embedder: EmbeddingProvider | None = None,
        reranker: RerankerProvider | None = None,
        config: HybridBaselineConfig | _legacy.BaselineConfig | Mapping[str, Any] | None = None,
        model: str | None = None,
    ):
        resolved = HybridBaselineConfig.model_validate(config or {})
        if retriever is None:
            if corpus is None and vector is None and vector_index is None:
                raise ValueError("HybridRAGRunner requires a rule corpus or vector index")
            corpus_model = (
                _legacy.RuleCorpus.model_validate(corpus)
                if corpus is not None
                else (vector or vector_index).corpus  # type: ignore[union-attr]
            )
            vector_model = vector or vector_index
            lexical_model = lexical or lexical_index
            if lexical_model is None:
                lexical_model = LexicalIndex(corpus_model)
            if vector_model is None:
                if embedder is None:
                    raise ValueError("a configured embedding provider is required")
                vector_model = _legacy.VanillaVectorIndex(corpus_model, embedder=embedder)
            retriever = HybridRetriever(
                corpus_model,
                lexical=lexical_model,
                vector=vector_model,
                reranker=reranker,
                index_version="hybrid-rag-v1",
                lexical_k=resolved.lexical_k,
                vector_k=resolved.vector_k,
                candidate_k=resolved.candidate_k,
                rrf_k=resolved.rrf_k,
                lexical_weight=resolved.lexical_weight,
                vector_weight=resolved.vector_weight,
                vector_min_score=resolved.vector_min_score,
            )
        elif reranker is not None and retriever.reranker is None:
            retriever.reranker = reranker
        if retriever.reranker is None:
            raise ValueError("HybridRAGRunner requires a reranker adapter")
        corpus_model = retriever.corpus
        if config is None:
            resolved = resolved.model_copy(
                update={
                    "ruleset_id": corpus_model.ruleset_id,
                    "ruleset_version": corpus_model.version,
                }
            )
        if (
            resolved.ruleset_id != corpus_model.ruleset_id
            or resolved.ruleset_version != corpus_model.version
        ):
            raise ValueError(
                "hybrid corpus and baseline configuration must use the same rule version"
            )
        super_config = {
            name: value
            for name, value in resolved.model_dump(mode="python").items()
            if name in _legacy.BaselineConfig.model_fields
        }
        super().__init__(provider, config=super_config, model=model)
        self.config = resolved.model_copy(
            update={
                "model": self.config.model,
                "provider": self.config.provider,
                "embedding_model": resolved.embedding_model
                or getattr(retriever.vector.embedder, "model", None),
                "reranker_model": resolved.reranker_model
                or getattr(retriever.reranker, "model", None),
            }
        )
        self.retriever = retriever
        self._reranker_usage_by_conversation: dict[int, dict[str, Any]] = {}
        self._last_reranker_usage: dict[str, Any] = {
            "reranker_calls": 0,
            "reranker_input_tokens": 0,
            "reranker_output_tokens": 0,
            "reranker_tokens": 0,
            "reranker_unknown_usage_calls": 0,
            "reranker_usage_available": False,
            "reranker_reported_cost_usd": 0.0,
            "reranker_reported_cost_calls": 0,
        }

    @staticmethod
    def _empty_reranker_usage() -> dict[str, Any]:
        return {
            "reranker_calls": 0,
            "reranker_input_tokens": 0,
            "reranker_output_tokens": 0,
            "reranker_tokens": 0,
            "reranker_unknown_usage_calls": 0,
            "reranker_usage_available": False,
            "reranker_reported_cost_usd": 0.0,
            "reranker_reported_cost_calls": 0,
        }

    def create_case(self) -> str:
        case_id = super().create_case()
        self._reranker_usage_by_conversation[id(self._conversations[case_id])] = (
            self._empty_reranker_usage()
        )
        return case_id

    def close(self) -> None:
        super().close()
        self._reranker_usage_by_conversation.clear()

    def submit_message(self, case_id: str, text: str) -> Mapping[str, Any]:
        response = dict(super().submit_message(case_id, text))
        conversation = self._conversations[case_id]
        stats = self._reranker_usage_by_conversation.get(
            id(conversation), self._last_reranker_usage
        )
        usage = _hybrid_usage(response.get("usage", {}), self, stats)
        response["usage"] = usage
        response["investigation"] = dict(response.get("investigation", {}))
        response["investigation"]["usage"] = usage
        if conversation.rounds:
            conversation.rounds[-1]["usage"] = usage
        response["investigation"]["rounds"] = list(conversation.rounds)
        return response

    def retrieval_metadata(self) -> dict[str, Any]:
        return {
            "mode": "hybrid_rag",
            **self.retriever.metadata,
            "retrieval_k": self.config.retrieval_k,
            "reranker_model": getattr(self.retriever.reranker, "model", None),
            "reranker_provider": getattr(
                self.retriever.reranker,
                "provider_name",
                type(self.retriever.reranker).__name__,
            ),
        }

    def _retrieve(self, query: str, conversation: Any) -> dict[str, Any]:
        vector_before = (
            self.retriever.vector.embedding_calls,
            self.retriever.vector.embedding_tokens,
            self.retriever.vector.unknown_usage_calls,
        )
        reranker_before = self._retriever_stats()

        async def retrieve():
            return await asyncio.wait_for(
                self.retriever.search(query, limit=self.config.retrieval_k),
                timeout=self._remaining_seconds(conversation),
            )

        try:
            hits = self._loop.run(retrieve())
        finally:
            ledger = conversation.ledger
            ledger.embedding_calls += self.retriever.vector.embedding_calls - vector_before[0]
            embedding_tokens = self.retriever.vector.embedding_tokens - vector_before[1]
            ledger.embedding_tokens += embedding_tokens
            ledger.total_tokens += embedding_tokens
            ledger.unknown_usage_calls += (
                self.retriever.vector.unknown_usage_calls - vector_before[2]
            )
            reranker_delta = self._retriever_stats_delta(reranker_before)
            prior = self._reranker_usage_by_conversation.get(id(conversation))
            if prior is None:
                cumulative = dict(reranker_delta)
            else:
                cumulative = {
                    "reranker_calls": prior["reranker_calls"] + reranker_delta["reranker_calls"],
                    "reranker_input_tokens": prior["reranker_input_tokens"]
                    + reranker_delta["reranker_input_tokens"],
                    "reranker_output_tokens": prior["reranker_output_tokens"]
                    + reranker_delta["reranker_output_tokens"],
                    "reranker_tokens": prior["reranker_tokens"] + reranker_delta["reranker_tokens"],
                    "reranker_unknown_usage_calls": prior["reranker_unknown_usage_calls"]
                    + reranker_delta["reranker_unknown_usage_calls"],
                    "reranker_usage_available": bool(
                        prior["reranker_usage_available"]
                        or reranker_delta["reranker_usage_available"]
                    ),
                    "reranker_reported_cost_usd": prior["reranker_reported_cost_usd"]
                    + reranker_delta["reranker_reported_cost_usd"],
                    "reranker_reported_cost_calls": prior["reranker_reported_cost_calls"]
                    + reranker_delta["reranker_reported_cost_calls"],
                }
            self._reranker_usage_by_conversation[id(conversation)] = cumulative
            self._last_reranker_usage = cumulative
            ledger.total_tokens += reranker_delta["reranker_tokens"]
            ledger.unknown_usage_calls += reranker_delta["reranker_unknown_usage_calls"]
        metadata = self.retrieval_metadata()
        metadata["query"] = query
        metadata["rule_ids"] = [hit.rule_id for hit in hits]
        metadata["documents"] = [hit.model_dump(mode="json") for hit in hits]
        metadata["trace"] = dict(self.retriever.last_trace)
        if not hits:
            metadata["failure_reason"] = "RETRIEVAL_EMPTY"
            metadata["stop_reason"] = "retrieval_empty"
        return metadata

    def _retriever_stats(self) -> dict[str, Any]:
        return {
            "reranker_calls": self.retriever.reranker_calls,
            "reranker_input_tokens": self.retriever.reranker_input_tokens,
            "reranker_output_tokens": self.retriever.reranker_output_tokens,
            "reranker_tokens": self.retriever.reranker_tokens,
            "reranker_unknown_usage_calls": self.retriever.reranker_unknown_usage_calls,
            "reranker_usage_available": self.retriever.reranker_usage_available,
            "reranker_reported_cost_usd": self.retriever.reranker_reported_cost,
            "reranker_reported_cost_calls": self.retriever.reranker_reported_cost_calls,
        }

    def _retriever_stats_delta(self, before: Mapping[str, Any]) -> dict[str, Any]:
        after = self._retriever_stats()
        return {
            "reranker_calls": after["reranker_calls"] - before["reranker_calls"],
            "reranker_input_tokens": after["reranker_input_tokens"]
            - before["reranker_input_tokens"],
            "reranker_output_tokens": after["reranker_output_tokens"]
            - before["reranker_output_tokens"],
            "reranker_tokens": after["reranker_tokens"] - before["reranker_tokens"],
            "reranker_unknown_usage_calls": after["reranker_unknown_usage_calls"]
            - before["reranker_unknown_usage_calls"],
            "reranker_usage_available": bool(
                after["reranker_usage_available"] or not before["reranker_usage_available"]
            ),
            "reranker_reported_cost_usd": after["reranker_reported_cost_usd"]
            - before["reranker_reported_cost_usd"],
            "reranker_reported_cost_calls": after["reranker_reported_cost_calls"]
            - before["reranker_reported_cost_calls"],
        }

    def _system_prompt(self, retrieval: Mapping[str, Any]) -> str:
        del retrieval
        return (
            "You are the Hybrid RAG plus Reranker evaluation baseline. Respond with one JSON "
            "object only. Use the user facts and the retrieved public rule excerpts. Lexical "
            "and vector retrieval only propose candidates; the reranker only orders those "
            "candidates and is not an authority for rule meaning. Never call a domain engine, "
            "read a verification coverage table, or invent a missing fact. Choose exactly one "
            "status from LEGAL, ILLEGAL, INSUFFICIENT_INFORMATION, UNRESOLVED. For LEGAL or "
            "ILLEGAL, cite retrieved rule IDs exactly as supplied. Ask for a specific fact field "
            "when more information is needed. Retrieval failure must be reported as UNRESOLVED."
        )


HybridRagRunner = HybridRAGRunner


def _aligned_rerank_scores(payload: Mapping[str, Any], count: int) -> list[float]:
    raw_scores = payload.get("scores")
    if isinstance(raw_scores, Sequence) and not isinstance(raw_scores, (str, bytes, Mapping)):
        scores = [float(value) for value in raw_scores]
        if len(scores) != count:
            raise ValueError("reranker score count does not match candidates")
        return scores
    raw_results = payload.get("results", payload.get("data", []))
    if not isinstance(raw_results, Sequence) or isinstance(raw_results, (str, bytes, Mapping)):
        raise TypeError("reranker response has no scores")
    aligned: list[float | None] = [None] * count
    for position, row in enumerate(raw_results):
        if not isinstance(row, Mapping):
            raise TypeError("reranker results must be objects")
        index = int(row.get("index", position))
        if index < 0 or index >= count or aligned[index] is not None:
            raise ValueError("reranker result indices do not match candidates")
        value = row.get("relevance_score", row.get("score"))
        if value is None:
            raise ValueError("reranker result has no score")
        aligned[index] = float(value)
    if any(value is None for value in aligned):
        raise ValueError("reranker score count does not match candidates")
    scores: list[float] = []
    for value in aligned:
        assert value is not None
        scores.append(float(value))
    return scores


def _coerce_rerank_batch(raw: Any, count: int) -> RerankBatch:
    if isinstance(raw, RerankBatch):
        batch = raw
    elif isinstance(raw, Mapping):
        payload = dict(raw)
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            payload.setdefault(
                "input_tokens", usage.get("input_tokens", usage.get("prompt_tokens"))
            )
            payload.setdefault(
                "output_tokens", usage.get("output_tokens", usage.get("completion_tokens"))
            )
            payload.setdefault("total_tokens", usage.get("total_tokens"))
            payload.setdefault("cost_usd", usage.get("cost_usd", usage.get("cost")))
        payload["scores"] = _aligned_rerank_scores(payload, count)
        batch = RerankBatch.model_validate(payload)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        batch = RerankBatch(scores=[float(value) for value in raw])
    else:
        scores = getattr(raw, "scores", None)
        if scores is None:
            raise ValueError("reranker response has no scores")
        batch = RerankBatch(
            scores=[float(value) for value in scores],
            input_tokens=_optional_nonnegative_int(getattr(raw, "input_tokens", None)),
            output_tokens=_optional_nonnegative_int(getattr(raw, "output_tokens", None)),
            total_tokens=_optional_nonnegative_int(getattr(raw, "total_tokens", None)),
            cost_usd=_optional_nonnegative_float(getattr(raw, "cost_usd", None)),
        )
    if len(batch.scores) != count:
        raise ValueError("reranker score count does not match candidates")
    return batch


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(value)]


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("token usage must be an integer")
    return max(0, int(value))


def _optional_nonnegative_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError("cost must be numeric")
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise ValueError("cost must be finite and non-negative")
    return result


def _normalize_runners(runners: Mapping[str, Any]) -> dict[str, Any]:
    aliases = {
        "llm-only": "llm_only",
        "vanilla-rag": "vanilla_rag",
        "vanilla_vector_rag": "vanilla_rag",
        "hybrid-rag": "hybrid_rag",
        "hybrid_reranker": "hybrid_rag",
        "hybrid_vector_rag": "hybrid_rag",
    }
    normalized: dict[str, Any] = {}
    for name, runner in runners.items():
        key = aliases.get(name, name)
        if key in normalized:
            raise ValueError(f"duplicate baseline strategy: {key}")
        normalized[key] = runner
    return normalized


_T12_RUN_BASELINES = _legacy._t12_run_baselines


def run_baselines(
    dataset: _legacy.CaseDataset,
    runners: Mapping[str, _legacy._ProviderBaselineRunner],
    *,
    verified_only: bool = False,
) -> _legacy.BaselineReport:
    """Run T12's pair or the complete T13 three-strategy comparison."""
    normalized = _normalize_runners(runners)
    if "hybrid_rag" not in normalized:
        return _T12_RUN_BASELINES(dataset, runners, verified_only=verified_only)
    required = {"llm_only", "vanilla_rag", "hybrid_rag"}
    if set(normalized) != required:
        missing = required - set(normalized)
        if missing:
            raise ValueError("T13 comparison requires both T12 runners and hybrid_rag")
        raise ValueError("runner strategy names must match the T13 strategies")
    if any(name != runner.strategy for name, runner in normalized.items()):
        raise ValueError("runner strategy names must match the T13 strategies")
    dataset.validate_for_scoring(require_split=False)
    if any(case.split == "holdout" for case in dataset.cases):
        raise ValueError("T13 development runners do not permit holdout cases")
    config_keys = {
        runner.config.ruleset_id + "\0" + runner.config.ruleset_version
        for runner in normalized.values()
    }
    if len(config_keys) != 1:
        raise ValueError("all baseline runners must use the same fixed ruleset version")
    shared = (
        "model",
        "provider",
        "max_clarification_rounds",
        "max_fact_requests",
        "timeout_seconds",
        "max_total_tokens",
        "max_tokens",
        "temperature",
        "prompt_version",
    )
    for name in shared:
        if len({getattr(runner.config, name) for runner in normalized.values()}) != 1:
            raise ValueError(f"baseline runners must share {name}")
    config = next(iter(normalized.values())).config
    for case in dataset.cases:
        if case.ruleset_id != config.ruleset_id:
            raise ValueError("case and runner ruleset versions differ")
        if case.review.rule_version and case.review.rule_version not in {
            config.ruleset_version,
            config.ruleset_id,
        }:
            raise ValueError("case review and runner rule versions differ")
    corpus_hashes = {
        str(normalized[name].retrieval_metadata().get("corpus_hash"))
        for name in ("vanilla_rag", "hybrid_rag")
    }
    if len(corpus_hashes) != 1:
        raise ValueError("vanilla and hybrid runners must use the same rule corpus")
    reports: dict[str, _legacy.BaselineRunReport] = {}
    result_ids: list[set[str]] = []
    for strategy, runner in normalized.items():
        try:
            results = runner.run_dataset(dataset, verified_only=verified_only)
        finally:
            runner.close()
        result_ids.append({result.case_id for result in results})
        reports[strategy] = _legacy.BaselineRunReport(
            strategy=strategy,
            config={
                **runner.config.model_dump(mode="json"),
                "config_hash": runner.config.config_hash,
                "implementation_hash": hashlib.sha256(
                    Path(_legacy.__file__).read_bytes()
                    + Path(__file__).read_bytes()
                    + Path(_legacy.__file__).with_name("embeddings.py").read_bytes()
                    + Path(_legacy.__file__).with_name("evaluation.py").read_bytes()
                ).hexdigest(),
            },
            evaluation=_legacy.score_results(dataset, results),
            usage=(
                _summarize_hybrid_usage(results)
                if strategy == "hybrid_rag"
                else _legacy._summarize_usage(results)
            ),
            retrieval=runner.retrieval_metadata(),
            citation_error_count=sum(
                any(
                    response.get("citation_check", {}).get("errors")
                    for response in (result.initial_result, result.complete_result)
                    if response.get("status") in {"LEGAL", "ILLEGAL"}
                )
                for result in results
            ),
            timeout_count=_legacy._failure_count(results, "INVESTIGATION_TIMEOUT"),
            evidence=_legacy._evidence_scores(dataset, results),
            results=[result.model_dump(mode="json") for result in results],
        )
    return _legacy.BaselineReport(
        schema_version="t13-baseline-report-v1",
        dataset_version=dataset.dataset_version,
        ruleset_id=config.ruleset_id,
        ruleset_version=config.ruleset_version,
        runs=reports,
        paired_case_ids=sorted(set.intersection(*result_ids)) if result_ids else [],
        corpus_hash=next(iter(corpus_hashes)),
        dataset_hash=hashlib.sha256(
            json.dumps(dataset.to_dict(), sort_keys=True).encode()
        ).hexdigest(),
        attribution_note=(
            "LLM-only, Vanilla Vector RAG, and Hybrid RAG + Reranker are external baselines; "
            "differences from any of them are not by themselves evidence of Dynamic Agent value."
        ),
    )


run_t13_baselines = run_baselines


def _summarize_hybrid_usage(results: Sequence[_legacy.ReplayResult]) -> dict[str, Any]:
    totals = _legacy._summarize_usage(results)
    for key in (
        "reranker_calls",
        "reranker_input_tokens",
        "reranker_output_tokens",
        "reranker_tokens",
        "reranker_unknown_usage_calls",
        "reranker_reported_cost_calls",
    ):
        totals[key] = sum(
            int(result.usage.get(key, 0))
            for result in results
            if isinstance(result.usage.get(key, 0), (int, float))
        )
    totals["reranker_usage_available"] = any(
        bool(result.usage.get("reranker_usage_available")) for result in results
    )
    totals["reranker_cost_usd"] = round(
        sum(float(result.usage.get("reranker_cost_usd", 0.0)) for result in results),
        8,
    )
    if any(result.usage.get("cost_usd") is None for result in results):
        totals["cost_usd"] = None
    return totals


def main(argv: list[str] | None = None) -> int:
    """Run T12 or T13 from a dataset and fixed public rule corpus."""
    command_args = list(sys.argv[1:] if argv is None else argv)
    if "--no-hybrid" in command_args:
        return _legacy._t12_main(
            [argument for argument in command_args if argument != "--no-hybrid"]
        )
    parser = ArgumentParser(prog="rulecourt-baselines")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-base-url")
    parser.add_argument("--reranker-model")
    parser.add_argument("--reranker-base-url")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-clarification-rounds", type=int)
    parser.add_argument("--max-fact-requests", type=int)
    parser.add_argument("--retrieval-k", type=int)
    parser.add_argument("--lexical-k", type=int)
    parser.add_argument("--vector-k", type=int)
    parser.add_argument("--candidate-k", type=int)
    parser.add_argument("--rrf-k", type=int)
    parser.add_argument("--vector-min-score", type=float)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-hybrid", action="store_true")
    args = parser.parse_args(command_args)
    dataset = _legacy.CaseDataset.from_json(args.dataset)
    corpus = _legacy.RuleCorpus.from_json(args.rules)
    if any(case.split == "holdout" for case in dataset.cases):
        parser.error("T13 development runs cannot access holdout cases")
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new experiment artifact path")
    values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    for name in (
        "model",
        "embedding_model",
        "embedding_base_url",
        "reranker_model",
        "reranker_base_url",
        "timeout_seconds",
        "max_clarification_rounds",
        "max_fact_requests",
        "retrieval_k",
        "lexical_k",
        "vector_k",
        "candidate_k",
        "rrf_k",
        "vector_min_score",
    ):
        if getattr(args, name) is not None:
            values[name] = getattr(args, name)
    values.setdefault("ruleset_id", corpus.ruleset_id)
    values.setdefault("ruleset_version", corpus.version)
    config = HybridBaselineConfig.model_validate(values)
    if config.ruleset_id != corpus.ruleset_id or config.ruleset_version != corpus.version:
        parser.error("corpus and configuration rule versions differ")
    shared_config = _legacy.BaselineConfig.model_validate(
        {
            name: value
            for name, value in config.model_dump(mode="python").items()
            if name in _legacy.BaselineConfig.model_fields
        }
    )
    if not config.embedding_model:
        parser.error("set embedding_model in --config or pass --embedding-model")
    if not args.no_hybrid and not config.reranker_model and not args.dry_run:
        parser.error("set reranker_model in --config or pass --reranker-model, or use --no-hybrid")
    dataset.validate_for_scoring(require_split=False)
    strategies = (
        ["llm_only", "vanilla_rag"]
        if args.no_hybrid
        else [
            "llm_only",
            "vanilla_rag",
            "hybrid_rag",
        ]
    )
    if args.dry_run:
        payload = {
            "dataset_version": dataset.dataset_version,
            "dataset_path": str(args.dataset),
            "rules_path": str(args.rules),
            "ruleset_id": corpus.ruleset_id,
            "ruleset_version": corpus.version,
            "corpus_hash": corpus.content_hash,
            "config": {**config.model_dump(mode="json"), "config_hash": config.config_hash},
            "strategies": strategies,
        }
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        embedder = HTTPEmbeddingProvider(
            model=config.embedding_model,
            base_url=config.embedding_base_url,
        )
        llm_provider = _legacy._cli_provider(config.model)
        runners: dict[str, Any] = {
            "llm_only": _legacy.LLMOnlyRunner(llm_provider, config=shared_config),
            "vanilla_rag": _legacy.VanillaVectorRAGRunner(
                _legacy._cli_provider(config.model),
                corpus=corpus,
                config=shared_config,
                embedder=embedder,
            ),
        }
        if not args.no_hybrid:
            assert config.reranker_model is not None
            runners["hybrid_rag"] = HybridRAGRunner(
                _legacy._cli_provider(config.model),
                corpus=corpus,
                config=config,
                embedder=embedder,
                reranker=HTTPRerankerProvider(
                    model=config.reranker_model,
                    base_url=config.reranker_base_url,
                ),
            )
        report = run_baselines(dataset, runners)
        output = (
            report.to_markdown()
            if args.format == "markdown"
            else json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
        )
    if args.output:
        args.output.write_text(output, encoding="utf-8")
    else:
        print(output, end="")
    return 0
