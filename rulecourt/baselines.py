"""Reproducible LLM-only and vanilla vector-RAG evaluation baselines.

The baseline runners deliberately sit beside the RuleCourt controller.  They
only receive the public case messages and facts returned by ``EvaluationRunner``;
they do not import or call the Root domain adapter, workflow, or coverage table.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import os
import re
import time
from argparse import ArgumentParser
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .embeddings import EmbeddingProvider, HTTPEmbeddingProvider
from .evaluation import (
    CaseAdapter,
    CaseDataset,
    EvaluationCase,
    EvaluationReport,
    EvaluationRunner,
    ReplayResult,
    VerdictLabel,
    score_results,
)


class RuleDocument(BaseModel):
    """One immutable public rule excerpt available to a RAG index."""

    model_config = ConfigDict(extra="forbid")

    id: str
    section: str = ""
    title: str = ""
    text: str
    source: str = ""

    @field_validator("id", "text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("rule document id and text must not be empty")
        return value


class RuleCorpus(BaseModel):
    """Fixed-version rule material; coverage obligations are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    ruleset_id: str = "root-law-2025-10"
    version: str = "2025-10"
    documents: list[RuleDocument] = Field(min_length=1)

    @field_validator("ruleset_id", "version")
    @classmethod
    def nonempty_metadata(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ruleset metadata must not be empty")
        return value

    @model_validator(mode="after")
    def unique_documents(self) -> RuleCorpus:
        ids = [document.id for document in self.documents]
        if len(ids) != len(set(ids)):
            raise ValueError("rule document ids must be unique")
        return self

    @property
    def content_hash(self) -> str:
        payload = {
            "ruleset_id": self.ruleset_id,
            "version": self.version,
            "documents": [document.model_dump(mode="json") for document in self.documents],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @classmethod
    def from_json(
        cls, path: str | Path, *, ruleset_id: str | None = None, version: str | None = None
    ) -> RuleCorpus:
        """Load a compact corpus or the checked-in candidate rule-package format.

        Only public rule excerpts are copied.  Fields such as
        ``coverage_obligations`` and evaluation labels are intentionally ignored.
        """

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(payload, list):
            payload = {"documents": payload}
        if not isinstance(payload, Mapping):
            raise TypeError("rule corpus must be a JSON object or document list")

        raw_documents = payload.get("documents", payload.get("rules", []))
        documents: list[RuleDocument] = []
        source = str(payload.get("locator", payload.get("source", "")))
        for raw in raw_documents:
            if not isinstance(raw, Mapping):
                raise TypeError("rule corpus documents must be objects")
            documents.append(
                RuleDocument(
                    id=str(raw.get("id", raw.get("rule_id", ""))),
                    section=str(raw.get("section", "")),
                    title=str(raw.get("title", "")),
                    text=str(raw.get("text", raw.get("content", ""))),
                    source=str(raw.get("source", source)),
                )
            )
        inferred_version = str(
            version
            or payload.get("version", payload.get("revision", payload.get("ruleset_version", "")))
            or "unknown"
        )
        inferred_ruleset = str(
            ruleset_id
            or payload.get("ruleset_id", "")
            or f"{payload.get('game_id', 'root')}-law-{inferred_version}"
        )
        return cls(
            ruleset_id=inferred_ruleset,
            version=inferred_version,
            documents=documents,
        )


class RetrievedRule(BaseModel):
    """One auditable result from the vanilla vector index."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    section: str = ""
    title: str = ""
    text: str
    source: str = ""
    score: float
    rank: int = Field(ge=1)


class VanillaVectorIndex:
    """Dense embedding cosine search; one rule per chunk, no reranking."""

    def __init__(
        self,
        corpus: RuleCorpus,
        *,
        embedder: EmbeddingProvider,
        index_version: str = "vanilla-vector-v1",
    ):
        self.corpus = corpus.model_copy(deep=True)
        self.embedder = embedder
        self.index_version = index_version.strip()
        if not self.index_version:
            raise ValueError("index_version must not be empty")
        self._vectors: list[list[float]] = []
        self.embedding_calls = 0
        self.embedding_tokens = 0
        self.unknown_usage_calls = 0
        self.index_hash: str | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "index_version": self.index_version,
            "ruleset_id": self.corpus.ruleset_id,
            "ruleset_version": self.corpus.version,
            "corpus_hash": self.corpus.content_hash,
            "document_count": len(self.corpus.documents),
            "embedding_model": self.embedder.model,
            "index_hash": self.index_hash,
            "chunking": "one-rule-v1",
            "similarity": "cosine",
            "cache": "build-once-per-runner",
        }

    async def _embed(self, texts: list[str]) -> list[list[float]]:
        self.embedding_calls += 1
        self.unknown_usage_calls += 1
        batch = await self.embedder.embed(texts)
        if batch.input_tokens is not None:
            self.embedding_tokens += batch.input_tokens
            self.unknown_usage_calls -= 1
        if len(batch.vectors) != len(texts):
            raise ValueError("embedding count does not match input count")
        vectors: list[list[float]] = []
        dimensions = len(batch.vectors[0]) if batch.vectors else 0
        for vector in batch.vectors:
            norm = math.sqrt(sum(value * value for value in vector))
            if len(vector) != dimensions or not math.isfinite(norm) or norm <= 0:
                raise ValueError("invalid embedding dimensions or values")
            vectors.append([value / norm for value in vector])
        return vectors

    async def search(self, query: str, *, limit: int = 4) -> list[RetrievedRule]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not query.strip():
            return []
        if not self._vectors:
            self._vectors = await self._embed(
                [f"{document.title} {document.text}" for document in self.corpus.documents]
            )
            self.index_hash = hashlib.sha256(
                json.dumps(
                    {
                        "corpus": self.corpus.content_hash,
                        "model": self.embedder.model,
                        "version": self.index_version,
                        "vectors": self._vectors,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
        query_vector = (await self._embed([query]))[0]
        if len(query_vector) != len(self._vectors[0]):
            raise ValueError("query and corpus embedding dimensions differ")
        scored: list[tuple[float, int]] = []
        for index, vector in enumerate(self._vectors):
            score = sum(a * b for a, b in zip(query_vector, vector, strict=True))
            scored.append((score, index))
        scored.sort(key=lambda item: (-item[0], self.corpus.documents[item[1]].id))
        return [
            RetrievedRule(
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


VanillaVectorRAGIndex = VanillaVectorIndex


class BaselineConfig(BaseModel):
    """Frozen, serializable parameters shared by both T12 strategies."""

    model_config = ConfigDict(extra="forbid")

    config_version: str = "t12-baseline-v1"
    ruleset_id: str = "root-law-2025-10"
    ruleset_version: str = "2025-10"
    prompt_version: str = "t12-prompt-v1"
    model: str | None = None
    provider: str | None = None
    max_clarification_rounds: int = 8
    max_fact_requests: int = 8
    timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    max_tokens: int = 1024
    max_total_tokens: int = Field(default=32768, gt=0, strict=True)
    temperature: float = 0.0
    retrieval_k: int = 4
    embedding_model: str | None = None
    embedding_base_url: str = "https://api.openai.com/v1"
    input_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    output_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    embedding_cost_per_1k_tokens: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("config_version", "ruleset_id", "ruleset_version", "prompt_version")
    @classmethod
    def nonempty_version(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("configuration versions must not be empty")
        return value

    @field_validator("max_clarification_rounds", "max_fact_requests", "max_tokens", "retrieval_k")
    @classmethod
    def positive_integer(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("configuration limits must be positive")
        return value

    @field_validator("timeout_seconds", "temperature")
    @classmethod
    def nonnegative_number(cls, value: float) -> float:
        if value < 0:
            raise ValueError("configuration numeric values must be non-negative")
        return value

    @property
    def config_hash(self) -> str:
        encoded = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


BaselineRunConfig = BaselineConfig


class BaselineResponse(BaseModel):
    """The normalized response contract consumed by ``EvaluationRunner``."""

    model_config = ConfigDict(extra="forbid")

    status: VerdictLabel
    reason: str
    explanation: str = ""
    clarification_questions: list[dict[str, str]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    claimed_status: VerdictLabel | None = None
    citation_check: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    investigation: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    strategy: str
    model: str = "unknown"
    provider: str = "unknown"
    stop_reason: str | None = None
    failure_reason: str | None = None


class BaselineRunReport(BaseModel):
    """One strategy's common score, audit metadata, and cumulative cost."""

    model_config = ConfigDict(extra="forbid")

    strategy: str
    config: dict[str, Any]
    evaluation: EvaluationReport
    usage: dict[str, Any] = Field(default_factory=dict)
    retrieval: dict[str, Any] = Field(default_factory=dict)
    citation_error_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    evidence: dict[str, Any] = Field(default_factory=dict)


class BaselineReport(BaseModel):
    """Comparable report for the LLM-only and vanilla RAG pair."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "t12-baseline-report-v1"
    dataset_version: str
    ruleset_id: str
    ruleset_version: str
    runs: dict[str, BaselineRunReport]
    paired_case_ids: list[str] = Field(default_factory=list)
    run_kind: str = "development_trial"
    corpus_hash: str = ""
    dataset_hash: str = ""
    attribution_note: str = (
        "LLM-only and Vanilla Vector RAG are external baselines; differences from either "
        "baseline are not by themselves evidence of Dynamic Agent value."
    )

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def save_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    @classmethod
    def from_json(cls, path: str | Path) -> BaselineReport:
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))

    def to_markdown(self) -> str:
        lines = [
            f"# RuleCourt T12 baseline report ({self.dataset_version})",
            "",
            f"Ruleset: `{self.ruleset_id}` / `{self.ruleset_version}`",
            f"Paired cases: {len(self.paired_case_ids)}",
            "",
            "| Strategy | Complete correct ruling | Citation errors | Timeouts | Total tokens | Cost |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
        for strategy, run in sorted(self.runs.items()):
            metric = run.evaluation.complete.correct_ruling_rate
            lines.append(
                f"| `{strategy}` | {metric.display()} ({metric.numerator}/{metric.denominator}) | "
                f"{run.citation_error_count} | {run.timeout_count} | "
                f"{run.usage.get('total_tokens', 0)} | {_cost_display(run.usage.get('cost_usd'))} |"
            )
        lines.extend(["", self.attribution_note, ""])
        for strategy, run in sorted(self.runs.items()):
            lines.extend(
                [
                    "",
                    f"## `{strategy}`",
                    "",
                    run.evaluation.to_markdown().rstrip(),
                    "",
                    "Configuration:",
                    "",
                    "```json",
                    json.dumps(run.config, ensure_ascii=False, indent=2),
                    "```",
                    "",
                    f"Retrieval metadata: `{json.dumps(run.retrieval, ensure_ascii=False, sort_keys=True)}`",
                ]
            )
            lines.extend(
                [
                    "",
                    "Cumulative resource usage:",
                    "",
                    json.dumps(run.usage, sort_keys=True),
                    "",
                    "Citation provenance and reference coverage (not semantic entailment):",
                    "",
                    json.dumps(run.evidence, sort_keys=True),
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


@dataclass
class _UsageLedger:
    config: BaselineConfig
    provider_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    usage_available: bool = False
    embedding_calls: int = 0
    embedding_tokens: int = 0
    unknown_usage_calls: int = 0

    def add(self, usage: Any, elapsed_ms: int) -> None:
        self.latency_ms += max(0, int(elapsed_ms))
        values = _usage_values(usage)
        if values is None:
            self.unknown_usage_calls += 1
            return
        self.usage_available = True
        input_tokens, output_tokens, total_tokens, reported_cost = values
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.total_tokens += max(total_tokens, input_tokens + output_tokens)
        if reported_cost is None:
            self.cost_usd += (
                input_tokens * (self.config.input_cost_per_1k_tokens or 0)
                + output_tokens * (self.config.output_cost_per_1k_tokens or 0)
            ) / 1000
        else:
            self.cost_usd += reported_cost

    def to_dict(self) -> dict[str, Any]:
        prices_known = (
            not self.provider_calls
            or (
                self.config.input_cost_per_1k_tokens is not None
                and self.config.output_cost_per_1k_tokens is not None
            )
        ) and (not self.embedding_calls or self.config.embedding_cost_per_1k_tokens is not None)
        known_cost = (
            self.cost_usd
            + self.embedding_tokens * (self.config.embedding_cost_per_1k_tokens or 0) / 1000
        )
        return {
            "provider_calls": self.provider_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
            "cost_usd": round(known_cost, 8)
            if prices_known and not self.unknown_usage_calls
            else None,
            "known_cost_usd": round(known_cost, 8),
            "usage_available": self.usage_available,
            "embedding_calls": self.embedding_calls,
            "embedding_tokens": self.embedding_tokens,
            "unknown_usage_calls": self.unknown_usage_calls,
        }


def _mapping_value(value: Any, names: Sequence[str]) -> Any:
    if isinstance(value, Mapping):
        for name in names:
            if name in value:
                return value[name]
    else:
        for name in names:
            if hasattr(value, name):
                return getattr(value, name)
    return None


def _number(value: Any) -> int:
    return (
        max(0, int(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0
    )


def _usage_values(usage: Any) -> tuple[int, int, int, float | None] | None:
    if usage is None:
        return None
    input_tokens = _number(_mapping_value(usage, ("input_tokens", "prompt_tokens")))
    output_tokens = _number(_mapping_value(usage, ("output_tokens", "completion_tokens")))
    total_raw = _mapping_value(usage, ("total_tokens",))
    total_tokens = _number(total_raw) if total_raw is not None else input_tokens + output_tokens
    cost_raw = _mapping_value(usage, ("cost_usd", "cost"))
    cost = (
        float(cost_raw)
        if isinstance(cost_raw, (int, float)) and not isinstance(cost_raw, bool)
        else None
    )
    if not (input_tokens or output_tokens or total_tokens or cost is not None):
        return None
    return input_tokens, output_tokens, total_tokens, cost


@dataclass
class _Conversation:
    config: BaselineConfig
    messages: list[dict[str, str]] = field(default_factory=list)
    ledger: _UsageLedger = field(init=False)
    submission_count: int = 0
    started: float = 0.0
    rounds: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.messages = []
        self.ledger = _UsageLedger(self.config)


class _ProviderBaselineRunner(CaseAdapter):
    strategy: str
    require_citations: bool = False

    def __init__(
        self,
        provider: Any,
        *,
        config: BaselineConfig | Mapping[str, Any] | None = None,
        model: str | None = None,
    ):
        self.provider = provider
        resolved = BaselineConfig.model_validate(config or {})
        if model is not None:
            resolved = resolved.model_copy(update={"model": model})
        if resolved.provider is None:
            provider_name = getattr(provider, "provider_name", type(provider).__name__)
            resolved = resolved.model_copy(update={"provider": str(provider_name)})
        if resolved.model is None:
            getter = getattr(provider, "get_default_model", None)
            resolved = resolved.model_copy(
                update={"model": str(getter()) if callable(getter) else "unknown"}
            )
        self.config = resolved
        self._conversations: dict[str, _Conversation] = {}
        self._loop = asyncio.Runner()

    def close(self) -> None:
        self._loop.close()

    @property
    def provider_name(self) -> str:
        return self.config.provider or "unknown"

    @property
    def model_name(self) -> str:
        if self.config.model:
            return self.config.model
        getter = getattr(self.provider, "get_default_model", None)
        return str(getter()) if callable(getter) else "unknown"

    def create_case(self) -> str:
        case_id = str(uuid4())
        self._conversations[case_id] = _Conversation(self.config)
        return case_id

    def submit_message(self, case_id: str, text: str) -> Mapping[str, Any]:
        conversation = self._conversations.get(case_id)
        if conversation is None:
            raise KeyError(f"unknown baseline case {case_id}")
        text = text.strip()
        if not text:
            raise ValueError("baseline messages must not be empty")
        conversation.submission_count += 1
        conversation.started = time.perf_counter()
        query = "\n".join(
            [
                *(
                    message["content"]
                    for message in conversation.messages
                    if message["role"] == "user"
                ),
                text,
            ]
        )
        retrieval: dict[str, Any] = self.retrieval_metadata()
        if (
            conversation.ledger.total_tokens >= self.config.max_total_tokens
            or self._remaining_seconds(conversation) <= 0
        ):
            response = self._failure_response(
                conversation,
                reason="BUDGET_EXHAUSTED",
                stop_reason="budget_exhausted",
                retrieval=retrieval,
            )
        else:
            try:
                retrieval = self._retrieve(query, conversation)
            except TimeoutError:
                retrieval["failure_reason"] = "INVESTIGATION_TIMEOUT"
            except Exception:  # noqa: BLE001 - retrieval failures are evaluation observations
                retrieval["failure_reason"] = "RETRIEVAL_ERROR"
            response = self._respond(conversation, text, retrieval)
        conversation.ledger.latency_ms += max(
            0, round((time.perf_counter() - conversation.started) * 1000)
        )
        response["usage"] = conversation.ledger.to_dict()
        conversation.rounds.append(
            {
                "user_message": text,
                **{
                    key: response[key]
                    for key in (
                        "status",
                        "reason",
                        "citations",
                        "citation_check",
                        "retrieval",
                        "usage",
                        "clarification_questions",
                    )
                },
            }
        )
        response["investigation"] = self._investigation(conversation, response["usage"])
        conversation.messages.append({"role": "user", "content": text})
        # Audit metadata and retrieval text must not drive subsequent retrieval.
        conversation.messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        key: response[key]
                        for key in (
                            "status",
                            "reason",
                            "explanation",
                            "clarification_questions",
                            "citations",
                        )
                    },
                    ensure_ascii=False,
                ),
            }
        )
        return response

    def _remaining_seconds(self, conversation: _Conversation) -> float:
        return max(
            0.0,
            self.config.timeout_seconds
            - conversation.ledger.latency_ms / 1000
            - (time.perf_counter() - conversation.started),
        )

    def _respond(
        self, conversation: _Conversation, text: str, retrieval: dict[str, Any]
    ) -> dict[str, Any]:
        if retrieval.get("failure_reason"):
            response = self._failure_response(
                conversation,
                reason=str(retrieval["failure_reason"]),
                stop_reason=str(retrieval.get("stop_reason", "retrieval_failed")),
                retrieval=retrieval,
            )
        else:
            messages = self._build_messages(conversation, text, retrieval)
            response = self._call_and_normalize(conversation, messages, retrieval)
        return response

    def run_case(self, case: EvaluationCase) -> ReplayResult:
        if case.split == "holdout":
            raise ValueError("T12 development runners do not permit holdout cases")
        if case.ruleset_id != self.config.ruleset_id:
            raise ValueError("case and runner ruleset versions differ")
        return EvaluationRunner(
            self,
            max_clarification_rounds=self.config.max_clarification_rounds,
            max_fact_requests=self.config.max_fact_requests,
        ).run_case(case)

    def run_dataset(
        self, dataset: CaseDataset, *, verified_only: bool = False
    ) -> list[ReplayResult]:
        cases = dataset.verified_cases if verified_only else dataset.cases
        if any(case.split == "holdout" for case in cases):
            raise ValueError("T12 development runners do not permit holdout cases")
        if any(case.ruleset_id != self.config.ruleset_id for case in cases):
            raise ValueError("case and runner ruleset versions differ")
        return [self.run_case(case) for case in cases]

    def retrieval_metadata(self) -> dict[str, Any]:
        return {
            "mode": "none",
            "ruleset_id": self.config.ruleset_id,
            "ruleset_version": self.config.ruleset_version,
            "index_version": None,
            "corpus_hash": None,
        }

    def _retrieve(self, query: str, conversation: _Conversation) -> dict[str, Any]:
        del query, conversation
        return self.retrieval_metadata()

    def _system_prompt(self, retrieval: Mapping[str, Any]) -> str:
        del retrieval
        return (
            "You are the LLM-only evaluation baseline. Respond with one JSON object only. "
            "You may use only facts present in the conversation; never invent a missing fact, "
            "call a domain engine, or read a verification coverage table. Choose exactly one "
            "status from LEGAL, ILLEGAL, INSUFFICIENT_INFORMATION, UNRESOLVED. Ask for a "
            "specific fact field when more information is needed. This baseline has no rule "
            "retrieval, so leave citations empty."
        )

    def _build_messages(
        self, conversation: _Conversation, text: str, retrieval: Mapping[str, Any]
    ) -> list[dict[str, str]]:
        current = text
        excerpts = retrieval.get("documents", [])
        if excerpts:
            current += "\n\nRetrieved public rule excerpts:\n"
            for item in excerpts:
                current += f"[{item['rule_id']}] {item['text']}\n"
        return [
            {
                "role": "system",
                "content": (
                    self._system_prompt(retrieval)
                    + f"\nFixed ruleset: {self.config.ruleset_id}, revision {self.config.ruleset_version}."
                    + "\nReturn JSON: {status, reason, explanation, citations: [rule_id], "
                    "clarification_questions: [{field, question}]}."
                    + "\nFact targets use these public paths: actor, phase, action.actor, "
                    "action.origin, action.destination, action.warrior_count, decree.column, "
                    "decree.card_suit, clearings.<id>.suit, clearings.<id>.adjacent_to, "
                    "clearings.<id>.presence.<faction>.warriors or buildings. "
                    "Unmentioned facts are unknown; completeness applies only to its stated scope. "
                    "Use INSUFFICIENT_INFORMATION for missing facts and UNSUPPORTED_SCOPE as the "
                    "reason for an out-of-scope UNRESOLVED result. Judge only local move conditions."
                ),
            },
            *conversation.messages,
            {"role": "user", "content": current},
        ]

    def _allowed_citation_ids(self, retrieval: Mapping[str, Any]) -> set[str]:
        return {str(item) for item in retrieval.get("rule_ids", [])}

    def _call_and_normalize(
        self,
        conversation: _Conversation,
        messages: list[dict[str, str]],
        retrieval: Mapping[str, Any],
    ) -> dict[str, Any]:
        if conversation.ledger.total_tokens >= self.config.max_total_tokens:
            return self._failure_response(
                conversation,
                reason="BUDGET_EXHAUSTED",
                stop_reason="budget_exhausted",
                retrieval=retrieval,
            )
        conversation.ledger.provider_calls += 1
        try:
            raw = self._call_provider(messages, conversation)
        except TimeoutError:
            conversation.ledger.unknown_usage_calls += 1
            return self._failure_response(
                conversation,
                reason="INVESTIGATION_TIMEOUT",
                stop_reason="timeout",
                retrieval=retrieval,
            )
        except Exception:  # noqa: BLE001 - a baseline must classify provider failure
            conversation.ledger.unknown_usage_calls += 1
            response = self._failure_response(
                conversation,
                reason="PROVIDER_ERROR",
                stop_reason="provider_error",
                retrieval=retrieval,
            )
            return response
        usage = _mapping_value(raw, ("usage",))
        conversation.ledger.add(usage, 0)
        if conversation.ledger.total_tokens > self.config.max_total_tokens:
            return self._failure_response(
                conversation,
                reason="BUDGET_EXHAUSTED",
                stop_reason="budget_exhausted",
                retrieval=retrieval,
            )
        if str(_mapping_value(raw, ("finish_reason",)) or "") == "error":
            error_kind = str(_mapping_value(raw, ("error_kind", "error_type")) or "provider_error")
            reason = "INVESTIGATION_TIMEOUT" if error_kind == "timeout" else "PROVIDER_ERROR"
            return self._failure_response(
                conversation,
                reason=reason,
                stop_reason="timeout" if reason == "INVESTIGATION_TIMEOUT" else "provider_error",
                retrieval=retrieval,
            )
        return self._normalize_model_output(raw, conversation, retrieval)

    def _call_provider(self, messages: list[dict[str, str]], conversation: _Conversation) -> Any:
        kwargs = {
            "messages": messages,
            "tools": None,
            "model": self.model_name,
            "max_tokens": min(
                self.config.max_tokens,
                self.config.max_total_tokens - conversation.ledger.total_tokens,
            ),
            "temperature": self.config.temperature,
            "tool_choice": None,
        }

        async def invoke() -> Any:
            if not inspect.iscoroutinefunction(self.provider.chat):
                raise TypeError("baseline providers must implement asynchronous chat")
            return await asyncio.wait_for(
                self.provider.chat(**kwargs), timeout=self._remaining_seconds(conversation)
            )

        return self._loop.run(invoke())

    def _normalize_model_output(
        self, raw: Any, conversation: _Conversation, retrieval: Mapping[str, Any]
    ) -> dict[str, Any]:
        content = _mapping_value(raw, ("content",))
        payload: Mapping[str, Any]
        if isinstance(raw, Mapping) and "status" in raw:
            payload = raw
        elif isinstance(content, Mapping):
            payload = content
        else:
            try:
                payload = _parse_json_object(str(content or ""))
            except (ValueError, TypeError):
                return self._failure_response(
                    conversation,
                    reason="MALFORMED_OUTPUT",
                    stop_reason="malformed_output",
                    retrieval=retrieval,
                )

        raw_status = str(payload.get("status", payload.get("verdict", ""))).strip().upper()
        if raw_status not in {"LEGAL", "ILLEGAL", "INSUFFICIENT_INFORMATION", "UNRESOLVED"}:
            return self._failure_response(
                conversation,
                reason="MALFORMED_OUTPUT",
                stop_reason="malformed_output",
                retrieval=retrieval,
            )
        questions = _questions(payload.get("clarification_questions", []))
        citations, citation_shape_errors = _citations(payload.get("citations", []))
        citation_errors = [*citation_shape_errors]
        allowed_ids = self._allowed_citation_ids(retrieval)
        for citation in citations:
            rule_id = citation.get("rule_id", "")
            if rule_id not in allowed_ids:
                citation_errors.append(f"UNKNOWN_CITATION:{rule_id or '<missing>'}")
            supplied_ruleset = citation.get("ruleset_id")
            if supplied_ruleset and supplied_ruleset != self.config.ruleset_id:
                citation_errors.append("RULESET_VERSION_MISMATCH")
            if (
                citation.get("ruleset_version", self.config.ruleset_version)
                != self.config.ruleset_version
            ):
                citation_errors.append("RULESET_VERSION_MISMATCH")
            supplied_index = citation.get("index_version")
            expected_index = retrieval.get("index_version")
            if supplied_index and expected_index and supplied_index != expected_index:
                citation_errors.append("INDEX_VERSION_MISMATCH")
            document = next(
                (item for item in retrieval.get("documents", []) if item["rule_id"] == rule_id),
                None,
            )
            if document:
                for key in ("section", "source"):
                    if citation.get(key) and citation[key] != document[key]:
                        citation_errors.append(f"CITATION_{key.upper()}_MISMATCH")
        if self.require_citations and raw_status in {"LEGAL", "ILLEGAL"} and not citations:
            citation_errors.append("MISSING_CITATION")
        citation_check = {
            "valid": not citation_errors,
            "errors": list(dict.fromkeys(citation_errors)),
            "provided_rule_ids": [citation.get("rule_id", "") for citation in citations],
            "retrieved_rule_ids": sorted(allowed_ids),
        }
        usage = conversation.ledger.to_dict()
        investigation = self._investigation(conversation, usage)
        response: dict[str, Any] = {
            "status": raw_status,
            "reason": str(payload.get("reason", raw_status)),
            "explanation": str(payload.get("explanation", "")),
            "clarification_questions": questions,
            "citations": citations,
            "citation_check": citation_check,
            "retrieval": dict(retrieval),
            "investigation": investigation,
            "usage": usage,
            "strategy": self.strategy,
            "model": self.model_name,
            "provider": self.provider_name,
            "stop_reason": "clarification_requested"
            if raw_status == "INSUFFICIENT_INFORMATION"
            else "completed",
        }
        return BaselineResponse.model_validate(response).model_dump(mode="json", exclude_none=True)

    def _failure_response(
        self,
        conversation: _Conversation,
        *,
        reason: str,
        stop_reason: str,
        retrieval: Mapping[str, Any],
    ) -> dict[str, Any]:
        usage = conversation.ledger.to_dict()
        return {
            "status": "UNRESOLVED",
            "reason": reason,
            "explanation": "The baseline could not produce a reliable result.",
            "clarification_questions": [],
            "citations": [],
            "citation_check": {"valid": False, "errors": [reason], "provided_rule_ids": []},
            "retrieval": dict(retrieval),
            "investigation": self._investigation(conversation, usage),
            "usage": usage,
            "strategy": self.strategy,
            "model": self.model_name,
            "provider": self.provider_name,
            "stop_reason": stop_reason,
            "failure_reason": reason,
        }

    @staticmethod
    def _investigation(conversation: _Conversation, usage: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "run_count": conversation.submission_count,
            "usage": dict(usage),
            "rounds": list(conversation.rounds),
        }


class LLMOnlyRunner(_ProviderBaselineRunner):
    """A provider-only baseline with no retrieved rules or domain tools."""

    strategy = "llm_only"


class VanillaVectorRAGRunner(_ProviderBaselineRunner):
    """A plain vector-retrieval baseline over one fixed public rule corpus."""

    strategy = "vanilla_rag"
    require_citations = True

    def __init__(
        self,
        provider: Any,
        *,
        corpus: RuleCorpus | Mapping[str, Any] | None = None,
        index: VanillaVectorIndex | None = None,
        embedder: EmbeddingProvider | None = None,
        config: BaselineConfig | Mapping[str, Any] | None = None,
        model: str | None = None,
    ):
        if index is None:
            if corpus is None:
                raise ValueError("VanillaVectorRAGRunner requires a rule corpus or index")
            corpus_model = RuleCorpus.model_validate(corpus)
            if config is None:
                config = BaselineConfig(
                    ruleset_id=corpus_model.ruleset_id,
                    ruleset_version=corpus_model.version,
                )
            if embedder is None:
                raise ValueError("a configured embedding provider is required")
            index = VanillaVectorIndex(corpus_model, embedder=embedder)
        else:
            corpus_model = index.corpus
            if config is None:
                config = BaselineConfig(
                    ruleset_id=corpus_model.ruleset_id,
                    ruleset_version=corpus_model.version,
                )
        config_model = BaselineConfig.model_validate(config)
        if (
            config_model.ruleset_id != corpus_model.ruleset_id
            or config_model.ruleset_version != corpus_model.version
        ):
            raise ValueError("RAG corpus and baseline configuration must use the same rule version")
        super().__init__(provider, config=config_model, model=model)
        self.index = index

    def retrieval_metadata(self) -> dict[str, Any]:
        return {"mode": "vanilla_vector", **self.index.metadata}

    def _retrieve(self, query: str, conversation: _Conversation) -> dict[str, Any]:
        before = (
            self.index.embedding_calls,
            self.index.embedding_tokens,
            self.index.unknown_usage_calls,
        )

        async def retrieve():
            return await asyncio.wait_for(
                self.index.search(query, limit=self.config.retrieval_k),
                timeout=self._remaining_seconds(conversation),
            )

        try:
            hits = self._loop.run(retrieve())
        finally:
            ledger = conversation.ledger
            ledger.embedding_calls += self.index.embedding_calls - before[0]
            tokens = self.index.embedding_tokens - before[1]
            ledger.embedding_tokens += tokens
            ledger.total_tokens += tokens
            ledger.unknown_usage_calls += self.index.unknown_usage_calls - before[2]
        metadata = self.retrieval_metadata()
        metadata["query"] = query
        metadata["rule_ids"] = [hit.rule_id for hit in hits]
        metadata["documents"] = [hit.model_dump(mode="json") for hit in hits]
        if not hits:
            metadata["failure_reason"] = "RETRIEVAL_EMPTY"
            metadata["stop_reason"] = "retrieval_empty"
        return metadata

    def _system_prompt(self, retrieval: Mapping[str, Any]) -> str:
        del retrieval
        return (
            "You are the Vanilla Vector RAG evaluation baseline. Respond with one JSON object "
            "only. Use only the user facts and the retrieved public rule excerpts in the current "
            "prompt; never call a domain engine, read a verification coverage table, or invent "
            "a missing fact. Choose exactly one status from LEGAL, ILLEGAL, "
            "INSUFFICIENT_INFORMATION, UNRESOLVED. For LEGAL or ILLEGAL, cite one or more "
            "retrieved rule IDs exactly as supplied. Ask for a specific fact field when more "
            "information is needed."
        )


VanillaRAGRunner = VanillaVectorRAGRunner
VanillaRagRunner = VanillaVectorRAGRunner


def _questions(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return []
    result: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        field = str(item.get("field", "")).strip()
        question = str(item.get("question", "")).strip()
        if field or question:
            result.append({"field": field, "question": question})
    return result


def _citations(raw: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if raw is None:
        return [], []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, Mapping)):
        return [], ["MALFORMED_CITATION_LIST"]
    result: list[dict[str, Any]] = []
    errors: list[str] = []
    for item in raw:
        if isinstance(item, str):
            result.append({"rule_id": item.strip()})
        elif isinstance(item, Mapping):
            rule_id = str(item.get("rule_id", item.get("id", item.get("document_id", "")))).strip()
            citation = {"rule_id": rule_id}
            for key in ("section", "source", "ruleset_id", "ruleset_version", "index_version"):
                if key in item and item[key] is not None:
                    citation[key] = str(item[key])
            result.append(citation)
        else:
            errors.append("MALFORMED_CITATION")
    return result, errors


def _parse_json_object(content: str) -> Mapping[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise TypeError("model output must be a JSON object")
    return payload


def _cost_display(cost: Any) -> str:
    return "N/A" if cost is None else f"{cost:.6f}"


def _summarize_usage(results: Sequence[ReplayResult]) -> dict[str, Any]:
    totals: dict[str, Any] = {
        "provider_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "latency_ms": 0,
        "cost_usd": 0.0,
        "usage_available": False,
        "embedding_calls": 0,
        "embedding_tokens": 0,
        "unknown_usage_calls": 0,
        "known_cost_usd": 0.0,
    }
    cost_known = True
    for result in results:
        for key in (
            "provider_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "latency_ms",
            "embedding_calls",
            "embedding_tokens",
            "unknown_usage_calls",
            "known_cost_usd",
        ):
            value = result.usage.get(key, 0)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                totals[key] = totals[key] + value  # type: ignore[operator]
        cost = result.usage.get("cost_usd", 0.0)
        cost_known = cost_known and cost is not None
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            totals["cost_usd"] = float(totals["cost_usd"]) + float(cost)
        totals["usage_available"] = bool(
            totals["usage_available"] or result.usage.get("usage_available")
        )
    totals["cost_usd"] = round(float(totals["cost_usd"]), 8) if cost_known else None
    return totals


def _evidence_scores(dataset: CaseDataset, results: Sequence[ReplayResult]) -> dict[str, Any]:
    """Offline reference-ID coverage, not a claim of semantic entailment."""
    cases = {case.id: case for case in dataset.verified_cases}
    views: dict[str, Any] = {}
    for view in ("initial_result", "complete_result"):
        decisions = [
            (cases[result.case_id], getattr(result, view))
            for result in results
            if result.case_id in cases
            and getattr(result, view).get("status") in {"LEGAL", "ILLEGAL"}
        ]
        valid = sum(
            bool(response.get("citation_check", {}).get("valid"))
            and bool(response.get("citations"))
            for _, response in decisions
        )
        covered = sum(
            bool(case.evidence)
            and set(case.evidence).issubset(
                {citation["rule_id"] for citation in response.get("citations", [])}
            )
            and bool(response.get("citation_check", {}).get("valid"))
            for case, response in decisions
        )
        views[view] = {
            "decision_count": len(decisions),
            "valid_citation_count": valid,
            "required_reference_coverage_count": covered,
            "valid_citation_rate": valid / len(decisions) if decisions else None,
            "required_reference_coverage_rate": covered / len(decisions) if decisions else None,
        }
    return views


def _failure_count(results: Sequence[ReplayResult], reason: str) -> int:
    count = 0
    for result in results:
        for response in (result.initial_result, result.complete_result):
            if response.get("failure_reason") == reason or response.get("reason") == reason:
                count += 1
                break
    return count


def run_baselines(
    dataset: CaseDataset,
    runners: Mapping[str, _ProviderBaselineRunner],
    *,
    verified_only: bool = False,
) -> BaselineReport:
    """Run the two T12 strategies with one shared dataset and score them together."""

    dataset.validate_for_scoring(require_split=False)
    if any(case.split == "holdout" for case in dataset.cases):
        raise ValueError("T12 development runners do not permit holdout cases")
    normalized: dict[str, _ProviderBaselineRunner] = {}
    aliases = {
        "llm-only": "llm_only",
        "vanilla-rag": "vanilla_rag",
        "vanilla_vector_rag": "vanilla_rag",
    }
    for name, runner in runners.items():
        key = aliases.get(name, name)
        if key in normalized:
            raise ValueError(f"duplicate baseline strategy: {key}")
        normalized[key] = runner
    required = {"llm_only", "vanilla_rag"}
    missing = required - set(normalized)
    if missing:
        raise ValueError("T12 comparison requires both runners: " + ", ".join(sorted(missing)))
    if set(normalized) != required or any(
        name != runner.strategy for name, runner in normalized.items()
    ):
        raise ValueError("runner strategy names must match the two T12 strategies")
    configs = {
        runner.config.ruleset_id + "\0" + runner.config.ruleset_version
        for runner in normalized.values()
    }
    if len(configs) != 1:
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
    if any(
        case.ruleset_id != next(iter(normalized.values())).config.ruleset_id
        for case in dataset.cases
    ):
        raise ValueError("case and runner ruleset versions differ")

    run_reports: dict[str, BaselineRunReport] = {}
    result_ids: list[set[str]] = []
    for strategy, runner in normalized.items():
        try:
            results = runner.run_dataset(dataset, verified_only=verified_only)
        finally:
            runner.close()
        result_ids.append({result.case_id for result in results})
        run_reports[strategy] = BaselineRunReport(
            strategy=strategy,
            config={
                **runner.config.model_dump(mode="json"),
                "config_hash": runner.config.config_hash,
                "implementation_hash": hashlib.sha256(
                    Path(__file__).read_bytes()
                    + Path(__file__).with_name("embeddings.py").read_bytes()
                    + Path(__file__).with_name("evaluation.py").read_bytes()
                ).hexdigest(),
            },
            evaluation=score_results(dataset, results),
            usage=_summarize_usage(results),
            retrieval=runner.retrieval_metadata(),
            citation_error_count=sum(
                any(
                    response.get("citation_check", {}).get("errors")
                    for response in (result.initial_result, result.complete_result)
                    if response.get("status") in {"LEGAL", "ILLEGAL"}
                )
                for result in results
            ),
            timeout_count=_failure_count(results, "INVESTIGATION_TIMEOUT"),
            evidence=_evidence_scores(dataset, results),
        )
    paired_case_ids = sorted(set.intersection(*result_ids)) if result_ids else []
    config = next(iter(normalized.values())).config
    return BaselineReport(
        dataset_version=dataset.dataset_version,
        ruleset_id=config.ruleset_id,
        ruleset_version=config.ruleset_version,
        runs=run_reports,
        paired_case_ids=paired_case_ids,
        corpus_hash=str(normalized["vanilla_rag"].retrieval_metadata()["corpus_hash"]),
        dataset_hash=hashlib.sha256(
            json.dumps(dataset.to_dict(), sort_keys=True).encode()
        ).hexdigest(),
    )


run_t12_baselines = run_baselines


def _cli_provider(model: str | None) -> Any:
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider

    selected_model = model or os.getenv("RULECOURT_MODEL", "gpt-4o-mini")
    return OpenAICompatProvider(
        api_key=os.getenv("OPENAI_API_KEY"),
        api_base=os.getenv("OPENAI_BASE_URL"),
        default_model=selected_model,
    )


def main(argv: list[str] | None = None) -> int:
    """Run both baselines from a dataset and fixed rule corpus."""

    parser = ArgumentParser(prog="rulecourt-baselines")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("--rules", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--format", choices=("json", "markdown"), default="json")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--embedding-model")
    parser.add_argument("--embedding-base-url")
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--max-clarification-rounds", type=int)
    parser.add_argument("--max-fact-requests", type=int)
    parser.add_argument("--retrieval-k", type=int)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    dataset = CaseDataset.from_json(args.dataset)
    corpus = RuleCorpus.from_json(args.rules)
    if any(case.split == "holdout" for case in dataset.cases):
        parser.error("T12 development runs cannot access holdout cases")
    if args.output and args.output.exists():
        parser.error("output already exists; choose a new experiment artifact path")
    values = json.loads(args.config.read_text(encoding="utf-8")) if args.config else {}
    for name in (
        "model",
        "embedding_model",
        "embedding_base_url",
        "timeout_seconds",
        "max_clarification_rounds",
        "max_fact_requests",
        "retrieval_k",
    ):
        if getattr(args, name) is not None:
            values[name] = getattr(args, name)
    values.setdefault("ruleset_id", corpus.ruleset_id)
    values.setdefault("ruleset_version", corpus.version)
    config = BaselineConfig.model_validate(values)
    if config.ruleset_id != corpus.ruleset_id or config.ruleset_version != corpus.version:
        parser.error("corpus and configuration rule versions differ")
    if not config.embedding_model:
        parser.error("set embedding_model in --config or pass --embedding-model")
    dataset.validate_for_scoring(require_split=False)
    if args.dry_run:
        payload = {
            "dataset_version": dataset.dataset_version,
            "dataset_path": str(args.dataset),
            "rules_path": str(args.rules),
            "ruleset_id": corpus.ruleset_id,
            "ruleset_version": corpus.version,
            "corpus_hash": corpus.content_hash,
            "config": {**config.model_dump(mode="json"), "config_hash": config.config_hash},
            "strategies": ["llm_only", "vanilla_rag"],
        }
        output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    else:
        report = run_baselines(
            dataset,
            {
                "llm_only": LLMOnlyRunner(_cli_provider(config.model), config=config),
                "vanilla_rag": VanillaVectorRAGRunner(
                    _cli_provider(config.model),
                    corpus=corpus,
                    config=config,
                    embedder=HTTPEmbeddingProvider(
                        model=config.embedding_model,
                        base_url=config.embedding_base_url,
                    ),
                ),
            },
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
