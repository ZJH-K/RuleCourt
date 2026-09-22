"""A constrained nanobot investigation round; verdict authority stays in Controller."""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from nanobot.agent.hook import AgentHook
from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.llm_runtime import LLMRuntime

from .budget import BudgetLimitReached, BudgetUsage, InvestigationBudget
from .state import NaturalLanguageStateExtractor
from .state_store import StateStore
from .store import CaseStore
from .workflow import FixedWorkflow


class InspectCase(Tool):
    def __init__(self, case_id: str, store: CaseStore):
        self.case_id = case_id
        self.store = store

    @property
    def name(self) -> str:
        return "inspect_case"

    @property
    def description(self) -> str:
        return "Read the current Case's accepted state and message count. This does not adjudicate."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs):
        case = self.store.get(self.case_id)
        assert case is not None
        return json.dumps(
            {
                "case_id": self.case_id,
                "revision": case["revision"],
                "confirmed_state": case["confirmed_state"],
                "message_count": len(case["messages"]),
            }
        )


async def _no_compaction(*args, **kwargs):
    return ""


class _BudgetTracker:
    def __init__(self, prior: BudgetUsage):
        self.prior = prior
        self.current = BudgetUsage()
        self.started_at = time.perf_counter()
        self.started_at_wall = datetime.now(UTC).isoformat()
        self.exhausted_dimension: str | None = None
        self.tool_details: list[dict[str, Any]] = []
        self._tool_by_id: dict[str, dict[str, Any]] = {}
        self._recorded_iterations: set[int] = set()
        self._recorded_usage_iterations: set[int] = set()

    @property
    def total(self) -> BudgetUsage:
        return self.prior.add(self.current)

    def start_iteration(
        self, iteration: int, budget: InvestigationBudget, *, provider_call: bool = True
    ) -> None:
        if iteration in self._recorded_iterations:
            return
        self._recorded_iterations.add(iteration)
        self.current.iterations += 1
        self.ensure_available(budget)

    def record_usage(self, usage: Any, iteration: int) -> None:
        if usage is None or iteration not in self._recorded_iterations:
            return
        if iteration in self._recorded_usage_iterations:
            return
        self._recorded_usage_iterations.add(iteration)
        values = usage.to_dict() if hasattr(usage, "to_dict") else {}
        self.current.input_tokens += max(0, int(values.get("input_tokens", 0)))
        self.current.output_tokens += max(0, int(values.get("output_tokens", 0)))
        self.current.total_tokens += max(0, int(values.get("total_tokens", 0)))
        request_count = max(0, int(values.get("request_count", 0)))
        self.current.provider_calls += max(0, request_count - 1)

    def ensure_available(self, budget: InvestigationBudget) -> None:
        total = self.total
        if total.iterations > budget.max_iterations:
            self.stop("iterations")
        if total.tool_calls > budget.max_tool_calls:
            self.stop("tool_calls")
        if total.total_tokens > budget.max_total_tokens:
            self.stop("total_tokens")
        if total.latency_ms + int((time.perf_counter() - self.started_at) * 1000) >= (
            budget.max_duration_seconds * 1000
        ):
            self.stop("duration")

    def stop(self, dimension: str) -> None:
        self.exhausted_dimension = dimension
        raise BudgetLimitReached(dimension)

    def register_tools(self, calls: list[Any], budget: InvestigationBudget) -> None:
        total_calls = self.total.tool_calls + len(calls)
        if total_calls > budget.max_tool_calls:
            self.stop("tool_calls")
        for call in calls:
            call_id = str(getattr(call, "id", "") or len(self.tool_details))
            params = getattr(call, "arguments", {})
            encoded = json.dumps(params, ensure_ascii=False, sort_keys=True, default=str)
            detail = {
                "name": str(getattr(call, "name", "unknown")),
                "call_id": call_id,
                "arguments_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
                "status": "pending",
                "duration_ms": 0,
                "result_size": 0,
                "_started": None,
            }
            self.current.tool_calls += 1
            self.tool_details.append(detail)
            self._tool_by_id[call_id] = detail

    def start_tool(self, call: Any) -> None:
        call_id = str(getattr(call, "id", "") or "")
        detail = self._tool_by_id.get(call_id)
        if detail is not None:
            detail["_started"] = time.perf_counter()

    def finish_tool(self, call: Any, *, success: bool, result: Any = None) -> None:
        call_id = str(getattr(call, "id", "") or "")
        detail = self._tool_by_id.get(call_id)
        if detail is None:
            return
        started = detail.pop("_started", None)
        detail["status"] = "ok" if success else "error"
        detail["duration_ms"] = (
            max(0, round((time.perf_counter() - started) * 1000)) if started else 0
        )
        detail["result_size"] = len(str(result)) if result is not None else 0

    def merge_tool_events(self, events: list[dict[str, Any]]) -> None:
        for index, event in enumerate(events):
            if index >= len(self.tool_details):
                break
            detail = self.tool_details[index]
            detail.pop("_started", None)
            detail["status"] = event.get("status", detail["status"])
            if event.get("detail") is not None:
                detail["result_size"] = len(str(event["detail"]))
        self.current.tool_failures = sum(
            1 for detail in self.tool_details if detail["status"] == "error"
        )

    def finish(self) -> None:
        self.current.latency_ms = max(0, round((time.perf_counter() - self.started_at) * 1000))


class _BudgetHook(AgentHook):
    def __init__(self, tracker: _BudgetTracker, budget: InvestigationBudget):
        self.tracker = tracker
        self.budget = budget

    async def before_iteration(self, context: Any) -> None:
        self.tracker.start_iteration(context.iteration, self.budget)

    async def before_execute_tools(self, context: Any) -> None:
        self.tracker.record_usage(context.usage, context.iteration)
        self.tracker.register_tools(context.tool_calls, self.budget)
        self.tracker.ensure_available(self.budget)

    async def before_execute_tool(
        self, context: Any, tool_call: Any, tool: Any, params: Any
    ) -> None:
        self.tracker.start_tool(tool_call)

    async def after_execute_tool(
        self, context: Any, tool_call: Any, tool: Any, params: Any, result: Any
    ) -> None:
        self.tracker.finish_tool(tool_call, success=True, result=result)

    async def on_execute_tool_error(
        self, context: Any, tool_call: Any, tool: Any, params: Any, error: Any
    ) -> None:
        self.tracker.finish_tool(tool_call, success=False, result=error)

    async def after_iteration(self, context: Any) -> None:
        self.tracker.record_usage(context.usage, context.iteration)
        if context.tool_calls and (
            self.tracker.total.tool_calls >= self.budget.max_tool_calls
            or self.tracker.total.total_tokens >= self.budget.max_total_tokens
        ):
            dimension = (
                "tool_calls"
                if self.tracker.total.tool_calls >= self.budget.max_tool_calls
                else "total_tokens"
            )
            self.tracker.stop(dimension)
        self.tracker.ensure_available(self.budget)


class RuleCourtController:
    def __init__(
        self,
        store: CaseStore,
        provider,
        model: str,
        state_store: StateStore,
        rule_store=None,
        budget=None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.state_store = state_store
        self.extractor = NaturalLanguageStateExtractor()
        self.workflow = FixedWorkflow(rule_store) if rule_store is not None else None
        self.budget = InvestigationBudget.from_value(budget)

    def _case_view(self, case_id: str) -> dict[str, Any]:
        case = self.store.get(case_id)
        assert case is not None
        state_view = self.state_store.view(case_id)
        assert state_view is not None
        return {**case, **state_view}

    @staticmethod
    def _has_move_action(state: dict[str, Any]) -> bool:
        action = state.get("action")
        return isinstance(action, dict) and action.get("type") == "move"

    @staticmethod
    def _has_deterministic_move_query(state: dict[str, Any]) -> bool:
        return RuleCourtController._has_move_action(state) or isinstance(state.get("decree"), dict)

    def _record_workflow_result(
        self,
        case_id: str,
        run_id: str,
        result: dict[str, Any],
        state_update: dict[str, Any] | None,
        *,
        expected_revision: int,
    ) -> dict[str, Any] | None:
        details = {
            key: result[key]
            for key in (
                "scope",
                "scope_confirmation_ref",
                "scope_confirmation_revision",
                "validated_completeness_refs",
                "validated_completeness_revision",
                "not_checked",
                "applicable_rules",
                "derived_facts",
                "checks",
                "explanation",
                "missing_fields",
                "clarification_questions",
            )
            if key in result
        }
        recorded = self.store.record_adjudication(
            case_id,
            run_id,
            result,
            expected_revision=expected_revision,
            details=details,
        )
        if recorded is None:
            return None
        decision = recorded["decision"]
        verification = recorded["verification"]
        verdict = recorded["verdict"]
        result["decision"] = decision
        result["verification"] = verification
        result["decision_ref"] = decision["id"]
        result["verification_refs"] = [verification["id"]]
        if result["status"] == "INSUFFICIENT_INFORMATION":
            self.store.add_event(
                case_id,
                run_id,
                "clarification_requested",
                missing_fields=result.get("missing_fields", []),
                questions=result.get("clarification_questions", []),
            )
        elif any(
            event.get("type") == "clarification_requested" for event in self.store.events(case_id)
        ):
            self.store.add_event(
                case_id,
                run_id,
                "clarification_resumed",
                resolved_fields=result.get("missing_fields", []),
            )
        response = {**verdict, **result}
        if state_update is not None:
            response["state_update"] = state_update
        return response

    def _discard_stale_adjudication(
        self,
        case_id: str,
        run_id: str,
        *,
        expected_revision: int,
        state_update: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        current = self.store.get(case_id)
        assert current is not None
        actual_revision = current["revision"]
        self.store.add_event(
            case_id,
            run_id,
            "adjudication_discarded",
            expected_state_revision=expected_revision,
            actual_state_revision=actual_revision,
        )
        response: dict[str, Any] = {
            "case_id": case_id,
            "run_id": run_id,
            "revision": actual_revision,
            "state_revision": actual_revision,
            "status": "UNRESOLVED",
            "reason": "STATE_REVISION_CONFLICT",
            "evidence": [],
            "details": {
                "public_reason": (
                    "The case changed while this adjudication was running; "
                    "its result was discarded."
                ),
                "expected_state_revision": expected_revision,
                "actual_state_revision": actual_revision,
            },
        }

        if state_update is not None:
            response["state_update"] = state_update
        return response

    def _provider_name(self) -> str:
        return str(getattr(self.provider, "provider_name", type(self.provider).__name__))

    def _select_investigation(
        self, case_id: str, strategy: str
    ) -> tuple[dict[str, Any], bool, InvestigationBudget]:
        latest = self.store.latest_investigation(case_id)
        if latest is not None and latest["status"] in {"active", "exhausted"}:
            budget = InvestigationBudget.from_mapping(latest["budget"])
            return latest, bool(latest.get("last_run_id")), budget
        budget = self.budget
        return (
            self.store.create_investigation(case_id, strategy, budget.to_dict()),
            False,
            budget,
        )

    @staticmethod
    def _budget_dimension(usage: BudgetUsage, budget: InvestigationBudget) -> str | None:
        if usage.iterations >= budget.max_iterations:
            return "iterations"
        if usage.tool_calls >= budget.max_tool_calls:
            return "tool_calls"
        if usage.total_tokens >= budget.max_total_tokens:
            return "total_tokens"
        if usage.latency_ms >= budget.max_duration_seconds * 1000:
            return "duration"
        return None

    def _investigation_view(
        self,
        investigation: dict[str, Any],
        budget: InvestigationBudget,
        *,
        run_id: str,
        resumed: bool,
        run_usage: BudgetUsage,
        stop_reason: str,
        failure_reason: str | None,
        budget_dimension: str | None,
    ) -> dict[str, Any]:
        cumulative = BudgetUsage.from_mapping(investigation["usage"])
        runs = [
            item
            for item in self.store.investigation_runs(investigation["case_id"])
            if item["investigation_id"] == investigation["id"]
        ]
        view = {
            "id": investigation["id"],
            "run_id": run_id,
            "run_count": len(runs),
            "strategy": investigation["strategy"],
            "resumed": resumed,
            "model": self.model,
            "provider": self._provider_name(),
            "budget": budget.to_dict(),
            "usage": cumulative.to_dict(),
            "remaining": cumulative.remaining(budget),
            "run_usage": run_usage.to_dict(),
            "stop_reason": stop_reason,
            "failure_reason": failure_reason,
        }
        if budget_dimension is not None:
            view["budget_dimension"] = budget_dimension
        return view

    def _attach_tool_events(
        self,
        case_id: str,
        run_id: str,
        tracker: _BudgetTracker,
        events: list[dict[str, Any]],
    ) -> None:
        tracker.merge_tool_events(events)
        if events:
            for index, event in enumerate(events):
                telemetry = tracker.tool_details[index] if index < len(tracker.tool_details) else {}
                payload = {
                    **event,
                    **{key: value for key, value in telemetry.items() if not key.startswith("_")},
                }
                self.store.add_event(case_id, run_id, "tool_call", **payload)
            return
        for telemetry in tracker.tool_details:
            payload = {
                key: ("blocked" if key == "status" and value == "pending" else value)
                for key, value in telemetry.items()
                if not key.startswith("_")
            }
            self.store.add_event(case_id, run_id, "tool_call", **payload)

    def _record_run(
        self,
        case_id: str,
        investigation: dict[str, Any],
        budget: InvestigationBudget,
        *,
        run_id: str,
        strategy: str,
        resumed: bool,
        response: dict[str, Any],
        tracker: _BudgetTracker,
        stop_reason: str,
        failure_reason: str | None,
        budget_dimension: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        tracker.finish()
        run_usage = tracker.current
        cumulative = tracker.total
        sequence_status = (
            "exhausted"
            if failure_reason in {"BUDGET_EXHAUSTED", "TIME_LIMIT_EXCEEDED"}
            else (
                "active"
                if (
                    response.get("status") == "INSUFFICIENT_INFORMATION"
                    or failure_reason
                    in {
                        "PROVIDER_ERROR",
                        "PROVIDER_INTERRUPTED",
                        "TOOL_FAILED",
                        "STATE_REVISION_CONFLICT",
                        "EVIDENCE_UNAVAILABLE",
                    }
                    or response.get("reason")
                    in {"DOMAIN_NOT_IMPLEMENTED", "INVESTIGATION_INCOMPLETE"}
                )
                else "completed"
            )
        )
        run_record = {
            "run_id": run_id,
            "strategy": strategy,
            "model": self.model,
            "provider": self._provider_name(),
            "resumed": resumed,
            "status": response.get("status", "UNRESOLVED"),
            "reason": response.get("reason", "UNRESOLVED"),
            "failure_reason": failure_reason,
            "stop_reason": stop_reason,
            "usage": run_usage.to_dict(),
            "started_at": tracker.started_at_wall,
            "finished_at": datetime.now(UTC).isoformat(),
            "metadata": {
                **(metadata or {}),
                "tool_calls": [
                    {key: value for key, value in detail.items() if not key.startswith("_")}
                    for detail in tracker.tool_details
                ],
            },
        }
        self.store.add_investigation_run(case_id, investigation["id"], record=run_record)
        updated = self.store.update_investigation(
            investigation["id"],
            status=sequence_status,
            usage=cumulative.to_dict(),
            last_reason=run_record["reason"],
            last_run_id=run_id,
        )
        self.store.add_event(
            case_id,
            run_id,
            "investigation_finished",
            strategy=strategy,
            model=self.model,
            provider=self._provider_name(),
            stop_reason=stop_reason,
            failure_reason=failure_reason,
            budget_dimension=budget_dimension,
            usage=run_usage.to_dict(),
        )
        response = {
            **response,
            "model": self.model,
            "provider": self._provider_name(),
            "stop_reason": stop_reason,
            "failure_reason": failure_reason,
            "investigation": self._investigation_view(
                updated,
                budget,
                run_id=run_id,
                resumed=resumed,
                run_usage=run_usage,
                stop_reason=stop_reason,
                failure_reason=failure_reason,
                budget_dimension=budget_dimension,
            ),
        }
        if budget_dimension is not None:
            response["budget_dimension"] = budget_dimension
        return response

    def _budget_exhausted(
        self,
        case_id: str,
        investigation: dict[str, Any],
        budget: InvestigationBudget,
        *,
        run_id: str,
        strategy: str,
        resumed: bool,
        dimension: str,
        state_update: dict[str, Any] | None,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.store.add_event(
            case_id,
            run_id,
            "budget_exhausted",
            strategy=strategy,
            dimension=dimension,
            budget=budget.to_dict(),
        )
        tracker = _BudgetTracker(BudgetUsage.from_mapping(investigation["usage"]))
        verdict = self.store.add_verdict_if_current(
            case_id,
            run_id,
            "UNRESOLVED",
            "BUDGET_EXHAUSTED",
            expected_revision=expected_revision,
            details={
                "public_reason": "The investigation budget was exhausted before a reliable result.",
                "budget_dimension": dimension,
            },
        )
        if verdict is None:
            stale_response = self._discard_stale_adjudication(
                case_id,
                run_id,
                expected_revision=expected_revision,
                state_update=state_update,
            )
            return self._record_run(
                case_id,
                investigation,
                budget,
                run_id=run_id,
                strategy=strategy,
                resumed=resumed,
                response=stale_response,
                tracker=tracker,
                stop_reason="state_revision_conflict",
                failure_reason="STATE_REVISION_CONFLICT",
                budget_dimension=dimension,
            )
        response = self._record_run(
            case_id,
            investigation,
            budget,
            run_id=run_id,
            strategy=strategy,
            resumed=resumed,
            response=verdict,
            tracker=tracker,
            stop_reason="budget_exhausted",
            failure_reason="BUDGET_EXHAUSTED",
            budget_dimension=dimension,
        )
        if state_update is not None:
            response["state_update"] = state_update
        return response

    async def investigate(self, case_id: str, text: str) -> dict[str, Any]:
        run_id = str(uuid4())
        before = self.store.get(case_id)
        assert before is not None
        previous_verdict = before["verdicts"][-1] if before["verdicts"] else None
        was_clarification_pending = (
            previous_verdict is not None
            and previous_verdict["status"] == "INSUFFICIENT_INFORMATION"
        )
        message_id = self.store.add_message(case_id, text)
        self.store.add_event(case_id, run_id, "message_received")
        if was_clarification_pending:
            assert previous_verdict is not None
            self.store.add_event(
                case_id,
                run_id,
                "clarification_resumed",
                previous_verdict_id=previous_verdict["id"],
                state_revision=before["revision"],
                source_message_id=message_id,
            )
        proposal = self.extractor.extract(
            text,
            message_id=message_id,
            expected_revision=before["revision"],
            current_state=before["confirmed_state"],
        )
        state_update = None
        if proposal.patch is not None:
            state_update = self.state_store.apply_patch(case_id, proposal.patch)
        elif proposal.issues:
            state_update = {
                "accepted": False,
                "state_revision": before["revision"],
                "sufficient": False,
                "missing_fields": proposal.unknown_fields,
                "issues": proposal.issues,
            }
        if state_update is not None:
            self.store.add_event(
                case_id,
                run_id,
                "state_accepted" if state_update["accepted"] else "state_rejected",
                state_revision=state_update["state_revision"],
                issues=state_update["issues"],
            )
            if not state_update["accepted"]:
                issue_codes = {issue["code"] for issue in state_update["issues"]}
                conflict_fields = [
                    issue["field_path"]
                    for issue in state_update["issues"]
                    if issue.get("code") == "FACT_CONFLICT" and issue.get("field_path")
                ]
                if conflict_fields:
                    state_update["clarification_questions"] = [
                        {
                            "field": field,
                            "question": (
                                f"Please clarify the value for {field}, or explicitly correct "
                                "the earlier fact."
                            ),
                        }
                        for field in dict.fromkeys(conflict_fields)
                    ]
                reason = (
                    "STATE_SCOPE_OUT_OF_BOUNDS"
                    if "SCOPE_OUT_OF_BOUNDS" in issue_codes
                    else "STATE_CONFLICT"
                    if "FACT_CONFLICT" in issue_codes
                    else "STATE_UPDATE_REJECTED"
                )
                verdict = self.store.add_verdict_if_current(
                    case_id,
                    run_id,
                    "UNRESOLVED",
                    reason,
                    expected_revision=state_update["state_revision"],
                )
                if verdict is None:
                    return self._discard_stale_adjudication(
                        case_id,
                        run_id,
                        expected_revision=state_update["state_revision"],
                        state_update=state_update,
                    )
                return {**verdict, "state_update": state_update}
        case = self._case_view(case_id)
        expected_revision = case["revision"]
        strategy = (
            "fixed_workflow"
            if self.workflow is not None
            and self._has_deterministic_move_query(case["confirmed_state"])
            else "dynamic_agent"
        )
        investigation, resumed, budget = self._select_investigation(case_id, strategy)
        prior_usage = BudgetUsage.from_mapping(investigation["usage"])
        self.store.add_event(
            case_id,
            run_id,
            "investigation_started",
            strategy=strategy,
            resumed=resumed,
            model=self.model,
            provider=self._provider_name(),
            budget=budget.to_dict(),
            cumulative_usage=prior_usage.to_dict(),
        )
        dimension = self._budget_dimension(prior_usage, budget)
        if dimension is not None:
            return self._budget_exhausted(
                case_id,
                investigation,
                budget,
                run_id=run_id,
                strategy=strategy,
                resumed=resumed,
                dimension=dimension,
                state_update=state_update,
                expected_revision=expected_revision,
            )

        tracker = _BudgetTracker(prior_usage)
        if strategy == "fixed_workflow":
            try:
                tracker.start_iteration(0, budget, provider_call=False)
                remaining_seconds = float(prior_usage.remaining(budget)["duration_seconds"])
                result = await asyncio.wait_for(
                    asyncio.to_thread(self.workflow.run, case),  # type: ignore[union-attr]
                    timeout=max(0.001, remaining_seconds),
                )
            except BudgetLimitReached as exc:
                return self._budget_exhausted(
                    case_id,
                    investigation,
                    budget,
                    run_id=run_id,
                    strategy=strategy,
                    resumed=resumed,
                    dimension=exc.dimension,
                    state_update=state_update,
                    expected_revision=expected_revision,
                )
            except TimeoutError:
                current = self.store.get(case_id)
                assert current is not None
                if current["revision"] != expected_revision:
                    stale_response = self._discard_stale_adjudication(
                        case_id,
                        run_id,
                        expected_revision=expected_revision,
                        state_update=state_update,
                    )
                    return self._record_run(
                        case_id,
                        investigation,
                        budget,
                        run_id=run_id,
                        strategy=strategy,
                        resumed=resumed,
                        response=stale_response,
                        tracker=tracker,
                        stop_reason="state_revision_conflict",
                        failure_reason="STATE_REVISION_CONFLICT",
                    )
                verdict = self.store.add_verdict_if_current(
                    case_id,
                    run_id,
                    "UNRESOLVED",
                    "INVESTIGATION_TIMEOUT",
                    expected_revision=expected_revision,
                    details={"public_reason": "The investigation exceeded its time limit."},
                )
                if verdict is None:
                    stale_response = self._discard_stale_adjudication(
                        case_id,
                        run_id,
                        expected_revision=expected_revision,
                        state_update=state_update,
                    )
                    return self._record_run(
                        case_id,
                        investigation,
                        budget,
                        run_id=run_id,
                        strategy=strategy,
                        resumed=resumed,
                        response=stale_response,
                        tracker=tracker,
                        stop_reason="state_revision_conflict",
                        failure_reason="STATE_REVISION_CONFLICT",
                    )
                response = self._record_run(
                    case_id,
                    investigation,
                    budget,
                    run_id=run_id,
                    strategy=strategy,
                    resumed=resumed,
                    response=verdict,
                    tracker=tracker,
                    stop_reason="time_limit",
                    failure_reason="TIME_LIMIT_EXCEEDED",
                )
            else:
                self.store.add_event(
                    case_id,
                    run_id,
                    "fixed_workflow_started",
                    workflow="m0-root-move",
                )
                recorded_response = self._record_workflow_result(
                    case_id,
                    run_id,
                    result,
                    state_update,
                    expected_revision=expected_revision,
                )
                if recorded_response is None:
                    stale_response = self._discard_stale_adjudication(
                        case_id,
                        run_id,
                        expected_revision=expected_revision,
                        state_update=state_update,
                    )
                    response = self._record_run(
                        case_id,
                        investigation,
                        budget,
                        run_id=run_id,
                        strategy=strategy,
                        resumed=resumed,
                        response=stale_response,
                        tracker=tracker,
                        stop_reason="state_revision_conflict",
                        failure_reason="STATE_REVISION_CONFLICT",
                    )
                else:
                    self.store.add_event(
                        case_id,
                        run_id,
                        "decision_computed",
                        status=result["decision"]["status"],
                        reason_codes=result["decision"]["reason_codes"],
                        rule_ids=result["decision"]["rule_ids"],
                    )
                    self.store.add_event(
                        case_id,
                        run_id,
                        "verification_finished",
                        status=result["verification"]["status"],
                        rule_ids=result["verification"]["rule_ids"],
                    )
                    evidence_unavailable = result["verification"]["status"] != "passed"
                    response = self._record_run(
                        case_id,
                        investigation,
                        budget,
                        run_id=run_id,
                        strategy=strategy,
                        resumed=resumed,
                        response=recorded_response,
                        tracker=tracker,
                        stop_reason="evidence_unavailable" if evidence_unavailable else "completed",
                        failure_reason="EVIDENCE_UNAVAILABLE" if evidence_unavailable else None,
                    )
            return response

        tools = ToolRegistry()
        tools.register(InspectCase(case_id, self.store))
        messages = [
            {
                "role": "system",
                "content": (
                    "You are investigating a RuleCourt Case. Call inspect_case once. "
                    "Domain rules and verified evidence are not installed. You cannot issue a legal "
                    "or illegal ruling. Your response is advisory and cannot set a verdict."
                ),
            },
            *({"role": "user", "content": item["text"]} for item in case["messages"]),
        ]
        result = None
        failure_reason: str | None = None
        stop_reason = "completed"
        budget_dimension: str | None = None
        metadata: dict[str, Any] = {}
        try:
            max_iterations = int(prior_usage.remaining(budget)["iterations"])
            result = await asyncio.wait_for(
                AgentRunner().run(
                    AgentRunSpec(
                        initial_messages=messages,
                        tools=tools,
                        runtime=LLMRuntime.capture(
                            self.provider, self.model, context_window_tokens=8192
                        ),
                        max_iterations=max_iterations,
                        max_tool_result_chars=2000,
                        consolidate_history=_no_compaction,
                        session_key=f"rulecourt:{case_id}",
                        hook=_BudgetHook(tracker, budget),
                        finalize_on_max_iterations=False,
                    )
                ),
                timeout=max(
                    0.001,
                    float(prior_usage.remaining(budget)["duration_seconds"]),
                ),
            )
        except BudgetLimitReached as exc:
            failure_reason = "BUDGET_EXHAUSTED"
            stop_reason = "budget_exhausted"
            budget_dimension = exc.dimension
        except TimeoutError:
            failure_reason = "TIME_LIMIT_EXCEEDED"
            stop_reason = "time_limit"
        except asyncio.CancelledError:
            failure_reason = "PROVIDER_INTERRUPTED"
            stop_reason = "provider_interrupted"
            self.store.add_event(
                case_id,
                run_id,
                "provider_failed",
                failure_kind="interrupted",
            )
        except Exception as exc:  # noqa: BLE001 - provider failures become public run results
            failure_reason = "PROVIDER_ERROR"
            stop_reason = "provider_error"
            metadata["provider_error_kind"] = type(exc).__name__
            self.store.add_event(
                case_id,
                run_id,
                "provider_failed",
                failure_kind=type(exc).__name__,
            )

        self._attach_tool_events(
            case_id, run_id, tracker, result.tool_events if result is not None else []
        )
        if result is not None:
            metadata["agent_stop_reason"] = result.stop_reason
            metadata["failure_error_kind"] = result.failure_error_kind
            if result.stop_reason == "max_iterations":
                failure_reason = "BUDGET_EXHAUSTED"
                stop_reason = "budget_exhausted"
                budget_dimension = "iterations"
            elif result.error:
                failure_reason = "PROVIDER_ERROR"
                stop_reason = "provider_error"
                self.store.add_event(
                    case_id,
                    run_id,
                    "provider_failed",
                    failure_kind=result.failure_error_kind or "provider_error",
                )
            elif tracker.current.tool_failures:
                failure_reason = "TOOL_FAILED"
                stop_reason = "tool_failure"
            elif not any(
                event.get("name") == "inspect_case" and event.get("status") == "ok"
                for event in result.tool_events
            ):
                failure_reason = "EVIDENCE_UNAVAILABLE"
                stop_reason = "evidence_unavailable"
            else:
                stop_reason = "completed"

        if (
            result is not None
            and failure_reason in {None, "EVIDENCE_UNAVAILABLE"}
            and tracker.total.total_tokens >= budget.max_total_tokens
        ):
            failure_reason = "BUDGET_EXHAUSTED"
            stop_reason = "budget_exhausted"
            budget_dimension = "total_tokens"
        if failure_reason == "BUDGET_EXHAUSTED":
            reason = "BUDGET_EXHAUSTED"
        elif failure_reason == "TIME_LIMIT_EXCEEDED":
            reason = "INVESTIGATION_TIMEOUT"
        elif failure_reason in {"PROVIDER_ERROR", "PROVIDER_INTERRUPTED"}:
            reason = "INVESTIGATION_FAILED"
        elif failure_reason == "TOOL_FAILED":
            reason = "INVESTIGATION_INCOMPLETE"
        else:
            reason = "DOMAIN_NOT_IMPLEMENTED"
            if result is not None and not result.tool_events:
                reason = "INVESTIGATION_INCOMPLETE"
        current = self.store.get(case_id)
        assert current is not None
        if current["revision"] != expected_revision:
            stale_response = self._discard_stale_adjudication(
                case_id,
                run_id,
                expected_revision=expected_revision,
                state_update=state_update,
            )
            return self._record_run(
                case_id,
                investigation,
                budget,
                run_id=run_id,
                strategy=strategy,
                resumed=resumed,
                response=stale_response,
                tracker=tracker,
                stop_reason="state_revision_conflict",
                failure_reason="STATE_REVISION_CONFLICT",
                budget_dimension=budget_dimension,
                metadata=metadata,
            )
        verdict = self.store.add_verdict_if_current(
            case_id,
            run_id,
            "UNRESOLVED",
            reason,
            expected_revision=expected_revision,
            details={
                "public_reason": {
                    "BUDGET_EXHAUSTED": "The investigation budget was exhausted before a reliable result.",
                    "TIME_LIMIT_EXCEEDED": "The investigation exceeded its time limit.",
                    "PROVIDER_ERROR": "The model provider failed before a reliable result was available.",
                    "PROVIDER_INTERRUPTED": "The model provider interrupted the investigation.",
                    "TOOL_FAILED": "A required investigation tool failed.",
                    "EVIDENCE_UNAVAILABLE": "No authoritative evidence was available for this investigation.",
                }.get(
                    failure_reason or "",
                    "The investigation did not produce an authoritative ruling.",
                ),
            },
        )
        if verdict is None:
            stale_response = self._discard_stale_adjudication(
                case_id,
                run_id,
                expected_revision=expected_revision,
                state_update=state_update,
            )
            return self._record_run(
                case_id,
                investigation,
                budget,
                run_id=run_id,
                strategy=strategy,
                resumed=resumed,
                response=stale_response,
                tracker=tracker,
                stop_reason="state_revision_conflict",
                failure_reason="STATE_REVISION_CONFLICT",
                budget_dimension=budget_dimension,
                metadata=metadata,
            )
        response = self._record_run(
            case_id,
            investigation,
            budget,
            run_id=run_id,
            strategy=strategy,
            resumed=resumed,
            response=verdict,
            tracker=tracker,
            stop_reason=stop_reason,
            failure_reason=failure_reason,
            budget_dimension=budget_dimension,
            metadata=metadata,
        )
        if state_update is not None:
            response["state_update"] = state_update
        return response
