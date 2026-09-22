"""Shared investigation budget configuration and usage accounting."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from math import isfinite
from typing import Any


class BudgetConfigurationError(ValueError):
    """Raised when an investigation budget is not safe to execute."""


@dataclass(frozen=True, slots=True)
class InvestigationBudget:
    """Finite resources shared by dynamic and fixed investigation strategies.

    The defaults are deliberately labelled as development trial values. They are
    guardrails for the local M0 application, not frozen evaluation thresholds.
    ``max_total_tokens`` is the cost limit because the locked nanobot provider
    contract exposes token usage, while provider prices are not part of that
    contract.
    """

    max_duration_seconds: float = 30.0
    max_iterations: int = 3
    max_tool_calls: int = 8
    max_total_tokens: int = 8192
    configuration_status: str = "development_trial"
    _source: str = field(default="default", repr=False, compare=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.max_duration_seconds, (int, float))
            or isinstance(self.max_duration_seconds, bool)
            or not isfinite(float(self.max_duration_seconds))
            or self.max_duration_seconds <= 0
        ):
            raise BudgetConfigurationError("max_duration_seconds must be a finite positive number")
        for name in ("max_iterations", "max_tool_calls", "max_total_tokens"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise BudgetConfigurationError(f"{name} must be a positive integer")
        if self.configuration_status not in {"development_trial", "frozen"}:
            raise BudgetConfigurationError(
                "configuration_status must be development_trial or frozen"
            )

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> InvestigationBudget:
        """Build a budget from API/config input with explicit compatibility aliases."""

        aliases = {
            "max_time_seconds": "max_duration_seconds",
            "max_timeout_seconds": "max_duration_seconds",
            "max_tokens": "max_total_tokens",
            "source": "_source",
            "max_cost_tokens": "max_total_tokens",
            "status": "configuration_status",
        }
        normalized: dict[str, Any] = {}
        allowed = {
            "max_duration_seconds",
            "max_iterations",
            "max_tool_calls",
            "max_total_tokens",
            "configuration_status",
            "_source",
        }
        for key, item in value.items():
            target = aliases.get(key, key)
            if target not in allowed:
                raise BudgetConfigurationError(f"unknown budget field: {key}")
            normalized[target] = item
        return cls(**normalized)

    @classmethod
    def from_value(cls, value: InvestigationBudget | dict[str, Any] | None) -> InvestigationBudget:
        if value is None:
            return cls.from_env()
        if isinstance(value, cls):
            return value
        if isinstance(value, dict):
            return cls.from_mapping(value)
        raise BudgetConfigurationError("budget must be an InvestigationBudget or mapping")

    @classmethod
    def from_env(cls) -> InvestigationBudget:
        """Load local trial values without implying frozen evaluation settings."""

        def read(name: str, default: Any, parser):
            raw = os.getenv(name)
            if raw is None or not raw.strip():
                return default
            try:
                return parser(raw)
            except (TypeError, ValueError) as exc:
                raise BudgetConfigurationError(f"invalid {name}") from exc

        return cls(
            max_duration_seconds=read("RULECOURT_MAX_DURATION_SECONDS", 30.0, float),
            max_iterations=read("RULECOURT_MAX_ITERATIONS", 3, int),
            max_tool_calls=read("RULECOURT_MAX_TOOL_CALLS", 8, int),
            max_total_tokens=read("RULECOURT_MAX_TOTAL_TOKENS", 8192, int),
            _source="environment"
            if any(
                os.getenv(name)
                for name in (
                    "RULECOURT_MAX_DURATION_SECONDS",
                    "RULECOURT_MAX_ITERATIONS",
                    "RULECOURT_MAX_TOOL_CALLS",
                    "RULECOURT_MAX_TOTAL_TOKENS",
                )
            )
            else "default",
        )

    @property
    def max_time_seconds(self) -> float:
        """Compatibility name for callers that call the wall-clock cap a time limit."""

        return self.max_duration_seconds

    @property
    def max_tokens(self) -> int:
        """Compatibility name for the token-based cost cap."""

        return self.max_total_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_duration_seconds": self.max_duration_seconds,
            "max_iterations": self.max_iterations,
            "max_tool_calls": self.max_tool_calls,
            "max_total_tokens": self.max_total_tokens,
            "configuration_status": self.configuration_status,
            "source": self._source,
        }


@dataclass(slots=True)
class BudgetUsage:
    """Additive usage ledger for one investigation across clarification runs."""

    iterations: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    provider_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int = 0

    @classmethod
    def from_mapping(cls, value: dict[str, Any] | None) -> BudgetUsage:
        value = value or {}
        names = (
            "iterations",
            "tool_calls",
            "tool_failures",
            "provider_calls",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "latency_ms",
        )
        return cls(**{name: max(0, int(value.get(name, 0))) for name in names})

    def add(self, other: BudgetUsage) -> BudgetUsage:
        return BudgetUsage(
            iterations=self.iterations + other.iterations,
            tool_calls=self.tool_calls + other.tool_calls,
            tool_failures=self.tool_failures + other.tool_failures,
            provider_calls=self.provider_calls + other.provider_calls,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            latency_ms=self.latency_ms + other.latency_ms,
        )

    def add_llm_usage(self, usage: Any) -> None:
        if usage is None:
            return
        values = usage.to_dict() if hasattr(usage, "to_dict") else {}
        self.input_tokens += max(0, int(values.get("input_tokens", 0)))
        self.output_tokens += max(0, int(values.get("output_tokens", 0)))
        self.total_tokens += max(0, int(values.get("total_tokens", 0)))
        self.provider_calls += max(0, int(values.get("request_count", 0)))

    def to_dict(self) -> dict[str, int]:
        return {
            "iterations": self.iterations,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "provider_calls": self.provider_calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "latency_ms": self.latency_ms,
        }

    def remaining(self, budget: InvestigationBudget) -> dict[str, int | float]:
        return {
            "duration_seconds": max(0.0, budget.max_duration_seconds - self.latency_ms / 1000),
            "iterations": max(0, budget.max_iterations - self.iterations),
            "tool_calls": max(0, budget.max_tool_calls - self.tool_calls),
            "total_tokens": max(0, budget.max_total_tokens - self.total_tokens),
        }


class BudgetLimitReached(RuntimeError):
    """Internal signal used to stop the agent before it can make another call."""

    def __init__(self, dimension: str):
        self.dimension = dimension
        super().__init__(f"investigation budget exhausted: {dimension}")
