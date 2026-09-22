"""Constrained public tools exposed to the Dynamic Agent.

The tools in this module are deliberately a thin boundary around the existing
stores and Root adapter.  They expose public rule/state/domain results, while
the Controller remains the only component that records an authoritative
verdict.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from nanobot.agent.tools.base import Tool, ToolResult
from nanobot.agent.tools.registry import ToolRegistry

from .root_adapter import RootAdapter
from .rules import AmbiguousPublicRuleError
from .state import M0_RULESET_VERSION, ProposedStatePatch
from .state_store import StateStore
from .store import CaseStore
from .workflow import FixedWorkflow

DYNAMIC_AGENT_STRATEGY_VERSION = "dynamic-agent-v1"
_SUPPORTED_SEARCH_MODES = {"support", "challenge", "clarification"}


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _error(message: str) -> ToolResult:
    return ToolResult.error(message)


class DynamicAgentSession:
    """Per-run state shared by the public Dynamic Agent tools."""

    def __init__(
        self,
        *,
        case_id: str,
        run_id: str,
        store: CaseStore,
        state_store: StateStore,
        rule_store: Any,
        workflow: FixedWorkflow | None,
        adapter: RootAdapter,
        on_submit: Callable[[dict[str, Any], dict[str, Any] | None, int], dict[str, Any] | None],
    ) -> None:
        self.case_id = case_id
        self.run_id = run_id
        self.store = store
        self.state_store = state_store
        self.rule_store = rule_store
        self.workflow = workflow
        self.adapter = adapter
        self.on_submit = on_submit
        self.inspected_rule_ids: set[str] = set()
        self.conflict_rule_ids: set[str] = set()
        self.challenge_search_performed = False
        self.challenge_rule_ids: set[str] = set()
        self.challenge_inspected_rule_ids: set[str] = set()
        self.challenge_applicability: dict[str, bool] | None = None
        self.challenge_unresolved_rule_ids: set[str] = set()
        self.conflict_resolution: dict[str, Any] | None = None
        self.last_simulation: dict[str, Any] | None = None
        self.last_simulation_revision: int | None = None
        self.latest_state_update: dict[str, Any] | None = None
        self.finalized_response: dict[str, Any] | None = None

    def registry(self) -> ToolRegistry:
        tools = ToolRegistry()
        tools.register(InspectCaseTool(self))
        tools.register(SearchRulesTool(self))
        tools.register(InspectRuleTool(self))
        tools.register(UpdateCaseStateTool(self))
        tools.register(ResolveRuleConflictsTool(self))
        tools.register(SimulateActionTool(self))
        tools.register(SubmitVerdictTool(self))
        return tools

    def case_view(self) -> dict[str, Any] | None:
        case = self.store.get(self.case_id)
        if case is None:
            return None
        state_view = self.state_store.view(self.case_id)
        if state_view is None:
            return None
        return {**case, **state_view}

    def event(self, event_type: str, **payload: Any) -> None:
        self.store.add_event(self.case_id, self.run_id, event_type, **payload)

    def enabled_package(self, ruleset_id: str = M0_RULESET_VERSION) -> dict[str, Any] | None:
        if ruleset_id not in {M0_RULESET_VERSION, M0_RULESET_VERSION.removeprefix("root-law-")}:
            return None
        if self.rule_store is None:
            return None
        try:
            package = self.rule_store.get_enabled_package(
                "root", M0_RULESET_VERSION.removeprefix("root-law-")
            )
            if package is None:
                package = self.rule_store.get_enabled_package("root", M0_RULESET_VERSION)
        except AmbiguousPublicRuleError:
            return None
        return package

    @staticmethod
    def public_simulation(result: dict[str, Any]) -> dict[str, Any]:
        """Project a workflow result without exposing private coverage data."""

        return {
            key: result[key]
            for key in (
                "case_id",
                "state_revision",
                "status",
                "reason",
                "ruleset_id",
                "ruleset_version_ref",
                "scope",
                "scope_confirmation_ref",
                "not_checked",
                "decision",
                "derived_facts",
                "evidence",
                "missing_fields",
                "clarification_questions",
                "explanation",
            )
            if key in result
        }

    def gate_ready(self, result: dict[str, Any]) -> bool:
        """Require public evidence and conflict checks before a ruling is submitted."""

        if result.get("status") not in {"LEGAL", "ILLEGAL"}:
            return True
        decision = result.get("decision") or {}
        required_rule_ids = set(decision.get("rule_ids", []))
        return (
            required_rule_ids <= self.inspected_rule_ids
            and required_rule_ids <= self.conflict_rule_ids
            and self.challenge_search_performed
            and bool(self.challenge_rule_ids)
            and self.challenge_rule_ids <= self.challenge_inspected_rule_ids
            and self.challenge_rule_ids <= self.conflict_rule_ids
            and self.challenge_applicability is not None
            and self.conflict_resolution is not None
            and self.conflict_resolution.get("status") == "resolved"
            and not self.challenge_unresolved_rule_ids
        )

    def evaluate_counter_evidence(self, result: dict[str, Any]) -> dict[str, Any] | None:
        """Project deterministic applicability for the public challenge results."""

        if (
            not self.challenge_search_performed
            or not self.challenge_rule_ids <= self.challenge_inspected_rule_ids
            or self.conflict_resolution is None
        ):
            return None
        decision_rule_ids = set((result.get("decision") or {}).get("rule_ids", []))
        applicability = {
            rule_id: rule_id in decision_rule_ids for rule_id in sorted(self.challenge_rule_ids)
        }
        self.challenge_applicability = applicability
        self.challenge_unresolved_rule_ids = set(
            self.conflict_resolution.get("unresolved_rule_ids", [])
        )
        applicable_rule_ids = [
            rule_id for rule_id, applicable in applicability.items() if applicable
        ]
        return {
            "candidate_rule_ids": sorted(self.challenge_rule_ids),
            "applicability": applicability,
            "applicable_rule_ids": applicable_rule_ids,
            "unresolved_rule_ids": sorted(self.challenge_unresolved_rule_ids),
        }


class InspectCaseTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "inspect_case"

    @property
    def description(self) -> str:
        return "Read the Case's accepted state, source messages, and current revision."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str | ToolResult:
        case = self.session.case_view()
        if case is None:
            return _error("Case not found.")
        self.session.event("case_inspected", state_revision=case["revision"])
        return _json(
            {
                "case_id": self.session.case_id,
                "state_revision": case["revision"],
                "strategy": case.get("strategy", "auto"),
                "confirmed_state": case["confirmed_state"],
                "unknown_fields": case.get("unknown_fields", []),
                "state_facts": case.get("state_facts", []),
                "scope_assumptions": case.get("scope_assumptions", []),
                "completeness_assertions": case.get("completeness_assertions", []),
                "messages": [
                    {"id": item["id"], "text": item["text"]} for item in case.get("messages", [])
                ],
            }
        )


class SearchRulesTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "search_rules"

    @property
    def description(self) -> str:
        return (
            "Search enabled public Root rules for supporting, challenging, or clarifying evidence."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500},
                "mode": {"type": "string", "enum": sorted(_SUPPORTED_SEARCH_MODES)},
                "faction": {"type": ["string", "null"]},
                "action": {"type": ["string", "null"]},
                "ruleset_id": {"type": "string", "enum": [M0_RULESET_VERSION]},
            },
            "required": ["query", "mode", "ruleset_id"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(
        self,
        query: str,
        mode: str,
        ruleset_id: str,
        faction: str | None = None,
        action: str | None = None,
        **kwargs: Any,
    ) -> str | ToolResult:
        if mode not in _SUPPORTED_SEARCH_MODES:
            return _error("Unsupported search mode.")
        package = self.session.enabled_package(ruleset_id)
        if package is None:
            return _error("No enabled verified rule package is available.")
        results = self.session.rule_store.search_public(query.strip(), limit=50)
        filtered: list[dict[str, Any]] = []
        for result in results:
            source = result.get("source", {})
            if source.get("id") != package["id"]:
                continue
            scope = result.get("scope", {})
            factions = set(scope.get("factions", []))
            actions = set(scope.get("actions", []))
            if faction and factions and faction not in factions:
                continue
            if action and actions and action not in actions:
                continue
            filtered.append(result)
        if mode == "challenge":
            self.session.challenge_search_performed = True
            self.session.challenge_rule_ids.update(item["id"] for item in filtered)
        self.session.event(
            "rule_searched",
            query=query.strip(),
            mode=mode,
            faction=faction,
            action=action,
            ruleset_id=ruleset_id,
            result_count=len(filtered),
            rule_ids=[item["id"] for item in filtered],
        )
        return _json(
            {
                "mode": mode,
                "ruleset_id": ruleset_id,
                "results": filtered,
            }
        )


class InspectRuleTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "inspect_rule"

    @property
    def description(self) -> str:
        return "Read complete public rule text and public relations for selected rule IDs."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rule_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                }
            },
            "required": ["rule_ids"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, rule_ids: list[str], **kwargs: Any) -> str | ToolResult:
        package = self.session.enabled_package()
        if package is None:
            return _error("No enabled verified rule package is available.")
        unique_ids = list(dict.fromkeys(rule_ids))
        rules: list[dict[str, Any]] = []
        missing: list[str] = []
        for rule_id in unique_ids:
            rule = self.session.rule_store.get_public_rule(rule_id, package_id=package["id"])
            if rule is None:
                missing.append(rule_id)
            else:
                rules.append(rule)
        if missing:
            return _error(_json({"error": "RULE_NOT_FOUND", "rule_ids": missing}))
        self.session.inspected_rule_ids.update(unique_ids)
        self.session.challenge_inspected_rule_ids.update(
            set(unique_ids) & self.session.challenge_rule_ids
        )
        self.session.event("rule_inspected", rule_ids=unique_ids, ruleset_id=M0_RULESET_VERSION)
        return _json({"ruleset_id": M0_RULESET_VERSION, "rules": rules})


class UpdateCaseStateTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "update_case_state"

    @property
    def description(self) -> str:
        return "Submit a proposed state patch backed by source messages in this Case."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "proposed_patch": {"type": "object"},
            },
            "required": ["proposed_patch"],
            "additionalProperties": False,
        }

    async def execute(self, proposed_patch: dict[str, Any], **kwargs: Any) -> str | ToolResult:
        try:
            patch = ProposedStatePatch.model_validate(proposed_patch)
        except Exception as exc:  # noqa: BLE001 - tool input becomes a public error
            return _error(f"Invalid proposed patch: {exc}")
        case = self.session.case_view()
        if case is None:
            return _error("Case not found.")
        result = self.session.state_store.apply_patch(self.session.case_id, patch)
        self.session.event(
            "state_update_requested",
            expected_state_revision=patch.expected_state_revision,
            state_revision=result.get("state_revision"),
            accepted=result.get("accepted", False),
            issues=result.get("issues", []),
        )
        self.session.latest_state_update = result
        if result.get("accepted"):
            self.session.inspected_rule_ids.clear()
            self.session.conflict_rule_ids.clear()
            self.session.conflict_resolution = None
            self.session.challenge_search_performed = False
            self.session.challenge_rule_ids.clear()
            self.session.challenge_inspected_rule_ids.clear()
            self.session.challenge_applicability = None
            self.session.challenge_unresolved_rule_ids.clear()
            self.session.last_simulation = None
            self.session.last_simulation_revision = None
            self.session.event("state_accepted", state_revision=result["state_revision"])
        else:
            self.session.event("state_rejected", state_revision=result.get("state_revision"))
        return _json({"case_id": self.session.case_id, **result})


class ResolveRuleConflictsTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "resolve_rule_conflicts"

    @property
    def description(self) -> str:
        return "Resolve precedence among public rule relations using the Root adapter."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "rule_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "maxItems": 50,
                },
                "case_id": {"type": "string"},
            },
            "required": ["rule_ids", "case_id"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, rule_ids: list[str], case_id: str, **kwargs: Any) -> str | ToolResult:
        if case_id != self.session.case_id:
            return _error("The requested Case is not this investigation.")
        package = self.session.enabled_package()
        if package is None:
            return _error("No enabled verified rule package is available.")
        unique_ids = list(dict.fromkeys(rule_ids))
        for rule_id in unique_ids:
            if self.session.rule_store.get_public_rule(rule_id, package_id=package["id"]) is None:
                return _error(_json({"error": "RULE_NOT_FOUND", "rule_id": rule_id}))
        resolution = self.session.adapter.resolve_rule_conflicts(package, unique_ids)
        self.session.conflict_resolution = resolution
        if resolution.get("status") != "resolved":
            self.session.event(
                "rule_conflicts_unresolved",
                case_id=case_id,
                rule_ids=unique_ids,
                unresolved_rule_ids=resolution.get("unresolved_rule_ids", []),
            )
            return _json(
                {
                    "accepted": False,
                    "code": "CONFLICT_UNRESOLVED",
                    "case_id": case_id,
                    **resolution,
                }
            )
        self.session.conflict_rule_ids.update(unique_ids)
        self.session.event(
            "rule_conflicts_resolved",
            case_id=case_id,
            rule_ids=unique_ids,
            relation_count=len(resolution.get("relations", [])),
        )
        return _json({"case_id": case_id, **resolution})


class SimulateActionTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "simulate_action"

    @property
    def description(self) -> str:
        return "Run the deterministic Root domain predicate for the current Case state."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"case_id": {"type": "string"}},
            "required": ["case_id"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(self, case_id: str, **kwargs: Any) -> str | ToolResult:
        if case_id != self.session.case_id:
            return _error("The requested Case is not this investigation.")
        case = self.session.case_view()
        if case is None:
            return _error("Case not found.")
        if self.session.workflow is None:
            return _error("No domain workflow is available.")
        result = self.session.workflow.run(case)
        public = self.session.public_simulation(result)
        counter_evidence = self.session.evaluate_counter_evidence(result)
        if counter_evidence is not None:
            public["counter_evidence"] = counter_evidence
        self.session.last_simulation = public
        self.session.last_simulation_revision = case["revision"]
        decision = result.get("decision", {})
        self.session.event(
            "action_simulated",
            case_id=case_id,
            state_revision=case["revision"],
            status=result.get("status"),
            reason=result.get("reason"),
            decision_status=decision.get("status"),
            reason_codes=decision.get("reason_codes", []),
            rule_ids=decision.get("rule_ids", []),
            missing_fields=decision.get("missing_fields", []),
        )
        if counter_evidence is not None:
            self.session.event("counter_evidence_checked", **counter_evidence)
        return _json(public)


class SubmitVerdictTool(Tool):
    def __init__(self, session: DynamicAgentSession):
        self.session = session

    @property
    def name(self) -> str:
        return "submit_verdict"

    @property
    def description(self) -> str:
        return "Ask the Controller to finalize the current deterministic result for this Case."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "expected_state_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["case_id", "expected_state_revision"],
            "additionalProperties": False,
        }

    @property
    def read_only(self) -> bool:
        return False

    async def execute(
        self,
        case_id: str,
        expected_state_revision: int,
        **kwargs: Any,
    ) -> str | ToolResult:
        if case_id != self.session.case_id:
            return _error("The requested Case is not this investigation.")
        case = self.session.case_view()
        if case is None:
            return _error("Case not found.")
        if expected_state_revision != case["revision"]:
            self.session.event(
                "verdict_submission_rejected",
                code="STATE_REVISION_CONFLICT",
                expected_state_revision=expected_state_revision,
                actual_state_revision=case["revision"],
            )
            return _json(
                {
                    "accepted": False,
                    "code": "STATE_REVISION_CONFLICT",
                    "state_revision": case["revision"],
                }
            )
        if self.session.workflow is None:
            return _json({"accepted": False, "code": "VERIFICATION_NOT_SATISFIED"})

        result = self.session.workflow.run(case)
        simulation_is_current = (
            self.session.last_simulation is not None
            and self.session.last_simulation_revision == case["revision"]
        )
        if result.get("status") in {"LEGAL", "ILLEGAL"} and not simulation_is_current:
            self.session.event(
                "verdict_submission_rejected",
                code="VERIFICATION_NOT_SATISFIED",
                state_revision=case["revision"],
            )
            return _json({"accepted": False, "code": "VERIFICATION_NOT_SATISFIED"})
        if not self.session.gate_ready(result):
            self.session.event(
                "verdict_submission_rejected",
                code="VERIFICATION_NOT_SATISFIED",
                state_revision=case["revision"],
            )
            return _json({"accepted": False, "code": "VERIFICATION_NOT_SATISFIED"})

        response = self.session.on_submit(
            result,
            self.session.latest_state_update,
            case["revision"],
        )
        if response is None:
            self.session.event(
                "verdict_submission_rejected",
                code="STATE_REVISION_CONFLICT",
                state_revision=case["revision"],
            )
            return _json({"accepted": False, "code": "STATE_REVISION_CONFLICT"})
        self.session.finalized_response = response
        self.session.event(
            "verdict_submitted",
            case_id=case_id,
            state_revision=case["revision"],
            status=response.get("status"),
            reason=response.get("reason"),
        )
        return _json(
            {
                "accepted": True,
                "case_id": case_id,
                "state_revision": response.get("state_revision"),
                "status": response.get("status"),
                "reason": response.get("reason"),
                "evidence": response.get("evidence", []),
                "missing_fields": response.get("missing_fields", []),
                "clarification_questions": response.get("clarification_questions", []),
            }
        )
