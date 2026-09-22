"""Versioned, deterministic workflow for the M0 Marquise ordinary move."""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from .adjudication import Decision, validate_move
from .rules import AmbiguousPublicRuleError, RuleStore
from .state import M0_RULESET_VERSION


class VerificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    status: Literal["passed", "failed"]
    ruleset_id: str
    ruleset_version_ref: str | None = None
    rule_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)


class FixedWorkflow:
    """Run a frozen sequence of domain checks without LLM routing."""

    scope: ClassVar[str] = "local_move_conditions"
    not_checked: ClassVar[list[str]] = [
        "full_turn_action_availability",
        "full_decree_progress",
    ]
    required_sections: ClassVar[tuple[str, ...]] = ("2.2", "2.5", "4.2", "4.2.1")

    def __init__(self, rule_store: RuleStore):
        self.rule_store = rule_store

    @staticmethod
    def _scope_confirmation(case: dict[str, Any]) -> dict[str, Any] | None:
        assumptions = case.get("active_scope_assumptions") or case.get("scope_assumptions", [])
        for assumption in assumptions:
            if (
                assumption.get("status") == "confirmed"
                and assumption.get("asserted_value") is True
                and assumption.get("scope_id") == "root-local-move"
                and assumption.get("predicate_id") == "m0.local_warrior_move_scope"
            ):
                return assumption
        return None

    @classmethod
    def _rule_index(cls, package: dict[str, Any]) -> dict[str, str] | None:
        by_section = {rule["section"]: rule["id"] for rule in package["rules"]}
        if any(section not in by_section for section in cls.required_sections):
            return None
        rule_ids = {by_section[section] for section in cls.required_sections}
        covered = {
            rule_id
            for obligation in package.get("coverage_obligations", [])
            for rule_id in obligation.get("rule_ids", [])
        }
        if not rule_ids.issubset(covered):
            return None
        return {
            "path": by_section["2.2"],
            "rule": by_section["2.5"],
            "move": by_section["4.2"],
            "move_restriction": by_section["4.2.1"],
        }

    @staticmethod
    def _public_rules(package: dict[str, Any], rule_ids: list[str]) -> list[dict[str, str]]:
        titles = {rule["id"]: rule["title"] or rule["section"] for rule in package["rules"]}
        return [
            {"id": rule_id, "title": titles[rule_id]} for rule_id in rule_ids if rule_id in titles
        ]

    @staticmethod
    def _clarification_questions(missing_fields: list[str]) -> list[dict[str, str]]:
        questions: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw_field in missing_fields:
            field = (
                raw_field.removesuffix(".complete")
                if raw_field.endswith(".adjacent_to.complete")
                else raw_field
            )
            if field in seen:
                continue
            seen.add(field)
            parts = field.split(".")
            if field == "scope.root-local-move":
                question = (
                    "Please confirm that this Case is limited to Marquise ordinary "
                    "local-move conditions."
                )
            elif len(parts) == 3 and parts[0] == "clearings" and parts[2] == "adjacent_to":
                question = (
                    f"Which clearings are adjacent to {parts[1]}? "
                    "State whether the list is complete if you know it."
                )
            elif len(parts) == 5 and parts[0] == "clearings" and parts[2] == "presence":
                question = (
                    f"How many {parts[3]} {parts[4]} are in clearing {parts[1]}? "
                    "State whether this is a complete list if relevant."
                )
            else:
                question = f"Please provide the confirmed value for {field}."
            questions.append({"field": field, "question": question})
        return questions

    def _base(
        self,
        case: dict[str, Any],
        package: dict[str, Any] | None,
        decision: Decision,
        verification: VerificationRecord,
        *,
        status: str,
        reason: str,
        scope_confirmation: dict[str, Any] | None = None,
        missing_fields: list[str] | None = None,
    ) -> dict[str, Any]:
        evidence = sorted(set(decision.rule_ids if verification.status == "passed" else []))
        ruleset_ref = package["id"] if package is not None else None
        resolved_missing_fields = list(
            decision.missing_fields if missing_fields is None else missing_fields
        )
        return {
            "case_id": case["id"],
            "state_revision": case["revision"],
            "status": status,
            "verdict": status.lower(),
            "reason": reason,
            "ruleset_id": M0_RULESET_VERSION,
            "ruleset_version_ref": ruleset_ref,
            "scope": self.scope,
            "scope_confirmation_ref": (
                scope_confirmation["id"] if scope_confirmation is not None else None
            ),
            "not_checked": list(self.not_checked),
            "decision": decision.model_dump(),
            "verification": verification.model_dump(),
            "decision_ref": None,
            "verification_refs": [verification.id],
            "evidence": evidence,
            "applicable_rules": (
                self._public_rules(package, evidence) if package is not None else []
            ),
            "derived_facts": {
                key: value
                for key, value in decision.derived_facts.items()
                if isinstance(value, dict)
            },
            "checks": {
                "state_sufficient": decision.status in {"allow", "deny"},
                "evidence_verified": verification.status == "passed",
            },
            "missing_fields": resolved_missing_fields,
            "clarification_questions": self._clarification_questions(resolved_missing_fields),
            "explanation": self._explanation(status, reason),
        }

    @staticmethod
    def _explanation(status: str, reason: str) -> str:
        if status == "LEGAL":
            return (
                "Marquise rules at least one endpoint and the checked local move conditions hold."
            )
        if status == "ILLEGAL":
            return "The proposed move violates a checked local move condition."
        if status == "INSUFFICIENT_INFORMATION":
            return "More confirmed facts are needed before this local move can be decided."
        if reason == "VERIFICATION_NOT_SATISFIED":
            return "The reviewed rule evidence is not available for this adjudication."
        return "This action is outside the supported M0 ordinary-move workflow."

    def run(self, case: dict[str, Any]) -> dict[str, Any]:
        try:
            package = self.rule_store.get_enabled_package(
                "root", M0_RULESET_VERSION.removeprefix("root-law-")
            )
            if package is None:
                package = self.rule_store.get_enabled_package("root", M0_RULESET_VERSION)
        except AmbiguousPublicRuleError:
            package = None
        rule_ids = self._rule_index(package) if package is not None else None
        if package is None or rule_ids is None:
            decision = Decision(status="unsupported", reason_codes=["VERIFICATION_NOT_SATISFIED"])
            verification = VerificationRecord(
                status="failed",
                ruleset_id=M0_RULESET_VERSION,
                checks={"state_sufficient": False, "evidence_verified": False},
            )
            return self._base(
                case,
                package,
                decision,
                verification,
                status="UNRESOLVED",
                reason="VERIFICATION_NOT_SATISFIED",
            )

        scope_confirmation = self._scope_confirmation(case)
        if scope_confirmation is None:
            decision = Decision(
                status="unknown",
                reason_codes=["SCOPE_NOT_CONFIRMED"],
                rule_ids=list(rule_ids.values()),
                missing_fields=["scope.root-local-move"],
            )
            verification = VerificationRecord(
                status="passed",
                ruleset_id=M0_RULESET_VERSION,
                ruleset_version_ref=package["id"],
                rule_ids=sorted(set(rule_ids.values())),
                evidence=sorted(set(rule_ids.values())),
                checks={"state_sufficient": False, "evidence_verified": True},
            )
            return self._base(
                case,
                package,
                decision,
                verification,
                status="INSUFFICIENT_INFORMATION",
                reason="SCOPE_NOT_CONFIRMED",
                missing_fields=decision.missing_fields,
            )

        state = dict(case["confirmed_state"])
        state["completeness_assertions"] = case.get(
            "active_completeness_assertions", case.get("completeness_assertions", [])
        )
        action = state.get("action")
        if isinstance(action, dict) and action.get("actor") != "marquise":
            decision = Decision(
                status="unsupported",
                reason_codes=["UNSUPPORTED_FACTION"],
                rule_ids=list(rule_ids.values()),
            )
            verification = VerificationRecord(
                status="passed",
                ruleset_id=M0_RULESET_VERSION,
                ruleset_version_ref=package["id"],
                rule_ids=sorted(set(rule_ids.values())),
                evidence=sorted(set(rule_ids.values())),
                checks={"state_sufficient": False, "evidence_verified": True},
            )
            return self._base(
                case,
                package,
                decision,
                verification,
                status="UNRESOLVED",
                reason="UNSUPPORTED_FACTION",
                scope_confirmation=scope_confirmation,
            )

        decision = validate_move(state, rule_ids=rule_ids)
        verification = VerificationRecord(
            status="passed",
            ruleset_id=M0_RULESET_VERSION,
            ruleset_version_ref=package["id"],
            rule_ids=sorted(set(decision.rule_ids)),
            evidence=sorted(set(decision.rule_ids)),
            checks={
                "state_sufficient": decision.status in {"allow", "deny"},
                "evidence_verified": True,
            },
        )
        if decision.status == "allow":
            status = "LEGAL"
            reason = "MOVE_LEGAL"
        elif decision.status == "deny":
            status = "ILLEGAL"
            reason = decision.reason_codes[0]
        elif decision.status == "unknown":
            status = "INSUFFICIENT_INFORMATION"
            reason = "INSUFFICIENT_INFORMATION"
        else:
            status = "UNRESOLVED"
            reason = decision.reason_codes[0] if decision.reason_codes else "UNSUPPORTED_ACTION"
        return self._base(
            case,
            package,
            decision,
            verification,
            status=status,
            reason=reason,
            scope_confirmation=scope_confirmation,
        )
