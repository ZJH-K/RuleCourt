"""Root-specific adapter for the deterministic M0 move court."""

from __future__ import annotations

from typing import Any, ClassVar

from .adjudication import Decision, validate_eyrie_decree_move, validate_move
from .state import M0_RULESET_VERSION


class RootPrecedencePolicy:
    """Resolve the public Root relation graph deterministically."""

    @staticmethod
    def resolve(package: dict[str, Any], rule_ids: list[str]) -> dict[str, Any]:
        requested = list(dict.fromkeys(rule_ids))
        nodes = set(requested)
        relations = [
            {
                "source_rule_id": relation["source_rule_id"],
                "target_rule_id": relation["target_rule_id"],
                "relation": relation["relation"],
            }
            for relation in package.get("relations", [])
            if relation.get("source_rule_id") in nodes and relation.get("target_rule_id") in nodes
        ]
        relations.sort(
            key=lambda relation: (
                relation["source_rule_id"],
                relation["target_rule_id"],
                relation["relation"],
            )
        )

        outgoing = {rule_id: set[str]() for rule_id in nodes}
        incoming = {rule_id: 0 for rule_id in nodes}
        for relation in relations:
            if relation["relation"] == "depends_on":
                before = relation["target_rule_id"]
                after = relation["source_rule_id"]
            else:
                before = relation["source_rule_id"]
                after = relation["target_rule_id"]
            if after in outgoing[before]:
                continue
            outgoing[before].add(after)
            incoming[after] += 1

        ready = sorted(rule_id for rule_id, count in incoming.items() if count == 0)
        precedence: list[str] = []
        while ready:
            current = ready.pop(0)
            precedence.append(current)
            for target in sorted(outgoing[current]):
                incoming[target] -= 1
                if incoming[target] == 0:
                    ready.append(target)
            ready.sort()

        unresolved_rule_ids = sorted(nodes - set(precedence))
        return {
            "status": "resolved" if not unresolved_rule_ids else "unresolved",
            "rule_ids": requested,
            "relations": relations,
            "precedence": precedence,
            "unresolved_rule_ids": unresolved_rule_ids,
        }


class RootAdapter:
    """Register Root's rule coverage and dispatch supported move predicates."""

    game_id = "root"
    ruleset_id = M0_RULESET_VERSION
    scope = "local_move_conditions"
    not_checked: ClassVar[list[str]] = [
        "full_turn_action_availability",
        "full_decree_progress",
    ]

    base_sections = ("2.2", "2.5", "4.2", "4.2.1")
    eyrie_sections = ("7.2.2",)
    decree_sections = ("2.1", "7.5.2")

    def required_sections(self, state: dict[str, Any]) -> tuple[str, ...]:
        """Return the rules needed for the action visible in ``state``."""

        sections: list[str] = list(self.base_sections)
        action = state.get("action")
        actor = action.get("actor") if isinstance(action, dict) else None
        if actor == "eyrie" and isinstance(state.get("decree"), dict):
            sections.extend(self.eyrie_sections)
            sections.extend(self.decree_sections)
        return tuple(dict.fromkeys(sections))

    def rule_index(self, package: dict[str, Any], state: dict[str, Any]) -> dict[str, str] | None:
        """Resolve package-local rule IDs only when verified coverage is complete."""

        by_section = {rule["section"]: rule["id"] for rule in package.get("rules", [])}
        required = self.required_sections(state)
        if any(section not in by_section for section in required):
            return None
        required_ids = {by_section[section] for section in required}
        covered = {
            rule_id
            for obligation in package.get("coverage_obligations", [])
            for rule_id in obligation.get("rule_ids", [])
        }
        if not required_ids.issubset(covered):
            return None
        action = state.get("action")
        actor = action.get("actor") if isinstance(action, dict) else None
        if actor == "eyrie" and "7.2.2" in by_section and by_section["7.2.2"] not in covered:
            return None
        result = {
            "path": by_section["2.2"],
            "rule": by_section["2.5"],
            "move": by_section["4.2"],
            "move_restriction": by_section["4.2.1"],
        }
        if actor == "eyrie" and "7.2.2" in by_section:
            result["eyrie_rule"] = by_section["7.2.2"]
        if self._is_decree_move(state):
            result["suit"] = by_section["2.1"]
            result["decree"] = by_section["7.5.2"]
        return result

    @staticmethod
    def resolve_rule_conflicts(package: dict[str, Any], rule_ids: list[str]) -> dict[str, Any]:
        """Resolve public Root relations without exposing coverage obligations."""

        return RootPrecedencePolicy.resolve(package, rule_ids)

    @staticmethod
    def _is_decree_move(state: dict[str, Any]) -> bool:
        action = state.get("action")
        return (
            isinstance(action, dict)
            and action.get("actor") == "eyrie"
            and isinstance(state.get("decree"), dict)
        )

    def validate_action(self, state: dict[str, Any], rule_ids: dict[str, str]) -> Decision:
        """Run the Root predicate selected by the confirmed action context."""

        action = state.get("action")
        if isinstance(action, dict) and action.get("actor") == "marquise":
            if isinstance(state.get("decree"), dict):
                return Decision(
                    status="unsupported",
                    reason_codes=["UNSUPPORTED_INTERACTION"],
                    rule_ids=list(rule_ids.values()),
                )
            return validate_move(state, rule_ids=rule_ids)
        if self._is_decree_move(state):
            return validate_eyrie_decree_move(state, rule_ids=rule_ids)
        if isinstance(action, dict) and action.get("actor") == "eyrie":
            if "eyrie_rule" not in rule_ids:
                return Decision(
                    status="unsupported",
                    reason_codes=["UNSUPPORTED_FACTION"],
                    rule_ids=list(rule_ids.values()),
                )
            return validate_move(state, rule_ids=rule_ids)
        return validate_move(state, rule_ids=rule_ids)

    @staticmethod
    def explanation(status: str, reason: str, state: dict[str, Any]) -> str:
        if status == "LEGAL":
            action = state.get("action")
            actor = action.get("actor") if isinstance(action, dict) else None
            if RootAdapter._is_decree_move(state):
                return (
                    "The Eyrie satisfies the checked local Move and Decree conditions; "
                    "complete Decree progress and action availability were not checked."
                )
            if actor == "eyrie":
                return "The Eyrie rules at least one endpoint and the checked local move conditions hold."
            return (
                "Marquise rules at least one endpoint and the checked local move conditions hold."
            )
        if status == "ILLEGAL":
            return "The proposed move violates a checked local move or Decree condition."
        if status == "INSUFFICIENT_INFORMATION":
            return "More confirmed facts are needed before this local move can be decided."
        if reason == "VERIFICATION_NOT_SATISFIED":
            return "The reviewed rule evidence is not available for this adjudication."
        return "This action is outside the supported M0 Root move workflow."
