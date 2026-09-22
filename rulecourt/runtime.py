"""A constrained nanobot investigation round; verdict authority stays in Controller."""

import json
from typing import Any
from uuid import uuid4

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.llm_runtime import LLMRuntime

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


class RuleCourtController:
    def __init__(
        self,
        store: CaseStore,
        provider,
        model: str,
        state_store: StateStore,
        rule_store=None,
    ):
        self.store = store
        self.provider = provider
        self.model = model
        self.state_store = state_store
        self.extractor = NaturalLanguageStateExtractor()
        self.workflow = FixedWorkflow(rule_store) if rule_store is not None else None

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

    def _record_workflow_result(
        self, case_id: str, run_id: str, result: dict[str, Any], state_update: dict[str, Any] | None
    ) -> dict[str, Any]:
        decision = self.store.add_decision(
            case_id, run_id, result["decision"], result["ruleset_id"]
        )
        verification = self.store.add_verification(
            case_id,
            run_id,
            result["verification"],
            decision_id=decision["id"],
        )
        result["decision"] = decision
        result["verification"] = verification
        result["decision_ref"] = decision["id"]
        result["verification_refs"] = [verification["id"]]
        details = {
            key: result[key]
            for key in (
                "scope",
                "scope_confirmation_ref",
                "not_checked",
                "decision_ref",
                "verification_refs",
                "applicable_rules",
                "derived_facts",
                "checks",
                "explanation",
                "missing_fields",
                "clarification_questions",
            )
            if key in result
        }
        verdict = self.store.add_verdict(
            case_id,
            run_id,
            result["status"],
            result["reason"],
            evidence=result["evidence"],
            details=details,
        )
        response = {**verdict, **result}
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
                reason = (
                    "STATE_SCOPE_OUT_OF_BOUNDS"
                    if "SCOPE_OUT_OF_BOUNDS" in issue_codes
                    else "STATE_UPDATE_REJECTED"
                )
                verdict = self.store.add_verdict(case_id, run_id, "UNRESOLVED", reason)
                return {**verdict, "state_update": state_update}
        case = self._case_view(case_id)
        if self.workflow is not None and self._has_move_action(case["confirmed_state"]):
            self.store.add_event(
                case_id, run_id, "fixed_workflow_started", workflow="m0-marquise-move"
            )
            result = self.workflow.run(case)
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
            response = self._record_workflow_result(case_id, run_id, result, state_update)
            if response["status"] == "INSUFFICIENT_INFORMATION":
                self.store.add_event(
                    case_id,
                    run_id,
                    "clarification_requested",
                    verdict_id=response["id"],
                    state_revision=response["revision"],
                    missing_fields=response["missing_fields"],
                    questions=response["clarification_questions"],
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
        try:
            result = await AgentRunner().run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    runtime=LLMRuntime.capture(
                        self.provider, self.model, context_window_tokens=8192
                    ),
                    max_iterations=3,
                    max_tool_result_chars=2000,
                    consolidate_history=_no_compaction,
                    session_key=f"rulecourt:{case_id}",
                )
            )
            for event in result.tool_events:
                self.store.add_event(case_id, run_id, "tool_call", **event)
            self.store.add_event(
                case_id,
                run_id,
                "investigation_finished",
                stop_reason=result.stop_reason,
                model=self.model,
            )
            if result.error:
                reason = "INVESTIGATION_FAILED"
            elif not any(
                event.get("name") == "inspect_case" and event.get("status") == "ok"
                for event in result.tool_events
            ):
                reason = "INVESTIGATION_INCOMPLETE"
            else:
                reason = "DOMAIN_NOT_IMPLEMENTED"
        except Exception as exc:  # noqa: BLE001 - provider failures become public run results
            self.store.add_event(
                case_id, run_id, "investigation_failed", error_type=type(exc).__name__
            )
            reason = "INVESTIGATION_FAILED"
        verdict = self.store.add_verdict(case_id, run_id, "UNRESOLVED", reason)
        return verdict if state_update is None else {**verdict, "state_update": state_update}
