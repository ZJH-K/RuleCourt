"""State proposals and the small, deterministic extractor used by T04.

The extractor is deliberately a proposal builder.  It never writes a Case,
chooses an investigation route, or produces a verdict. ``StateStore``
validates and accepts proposals into the confirmed state.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

M0_RULESET_VERSION = "root-law-2025-10"
M0_SCOPE_POLICY_VERSION = "root-local-move-v1"
SUPPORTED_FACTIONS = {"eyrie", "marquise"}
SUPPORTED_PIECES = {"warriors", "buildings"}
SUPPORTED_CLEARING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")


class StateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field_path: str = Field(min_length=1, max_length=300)
    source_message_id: str = Field(min_length=1, max_length=100)
    source_span: str = Field(min_length=1, max_length=2000)


class ProposedFactChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["assert", "correct", "retract"]
    field_path: str = Field(min_length=1, max_length=300)
    value: Any | None = None
    evidence: list[StateEvidence] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_operation(self) -> ProposedFactChange:
        if self.operation == "retract" and self.value is not None:
            raise ValueError("retract changes must not carry a value")
        if any(item.field_path != self.field_path for item in self.evidence):
            raise ValueError("fact evidence must point to the changed field")
        return self


class ProposedScopeAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["assert", "correct", "retract"] = "assert"
    scope_id: str = Field(min_length=1, max_length=120)
    predicate_id: str = Field(min_length=1, max_length=160)
    asserted_value: bool | None = None
    ruleset_version: str = Field(min_length=1, max_length=120)
    scope_policy_version: str = Field(min_length=1, max_length=120)
    depends_on_fact_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[StateEvidence] = Field(min_length=1)
    supersedes_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_operation(self) -> ProposedScopeAssumption:
        if self.operation == "retract" and self.asserted_value is not None:
            raise ValueError("retracted scope assumptions must not carry a value")
        return self


class ProposedCompletenessAssertion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: Literal["assert", "correct", "retract"] = "assert"
    collection_target: str = Field(min_length=1, max_length=300)
    covered_scope: dict[str, Any]
    completeness: Literal["partial", "complete"]
    member_snapshot: dict[str, Any] = Field(default_factory=lambda: {"members": []})
    member_snapshot_ref: str | None = Field(default=None, max_length=160)
    evidence_refs: list[StateEvidence] = Field(min_length=1)
    supersedes_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_operation(self) -> ProposedCompletenessAssertion:
        if self.operation == "retract" and self.member_snapshot.get("members"):
            raise ValueError("retracted completeness assertions must have an empty snapshot")
        return self


class ProposedStatePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_state_revision: int = Field(ge=0)
    changes: list[ProposedFactChange] = Field(default_factory=list)
    scope_assumptions: list[ProposedScopeAssumption] = Field(default_factory=list)
    completeness_assertions: list[ProposedCompletenessAssertion] = Field(default_factory=list)
    unknown_fields: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class ExtractionResult:
    patch: ProposedStatePatch | None
    issues: list[dict[str, str]]
    unknown_fields: list[str]
    recognized: bool


def _issue(code: str, message: str, field_path: str | None = None) -> dict[str, str]:
    result = {"code": code, "message": message}
    if field_path:
        result["field_path"] = field_path
    return result


def _evidence(field_path: str, message_id: str, source_span: str) -> StateEvidence:
    return StateEvidence(
        field_path=field_path,
        source_message_id=message_id,
        source_span=source_span.strip(),
    )


def _clearing(value: str) -> str:
    return value.strip().strip("，。,.；;：:")


def _faction(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip().lower()
    if value in {"eyrie", "eyrie dynasties", "老鹰", "鹰", "鸟巢"}:
        return "eyrie"
    if value in {"marquise", "marquise de cat", "猫", "猫咪", "猫侯爵"}:
        return "marquise"
    return None


def _piece(value: str) -> str | None:
    value = value.strip().lower()
    if value in {"兵", "战士", "warrior", "warriors", "unit", "units"}:
        return "warriors"
    if value in {"建筑", "建筑物", "building", "buildings", "巢穴", "roost"}:
        return "buildings"
    return None


_CHINESE_NUMBERS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "一个": 1,
    "一只": 1,
    "二": 2,
    "两个": 2,
    "两": 2,
    "两只": 2,
    "三": 3,
    "三个": 3,
    "四": 4,
    "四个": 4,
    "五": 5,
    "五个": 5,
    "六": 6,
    "六个": 6,
    "七": 7,
    "七个": 7,
    "八": 8,
    "八个": 8,
    "九": 9,
    "九个": 9,
    "十": 10,
}


def _count(value: str) -> int | None:
    value = value.strip().lower()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    return _CHINESE_NUMBERS.get(value)


_FACTION = r"(?:Eyrie(?:\s+Dynasties)?|Marquise(?:\s+de\s+Cat)?|老鹰|鹰|鸟巢|猫咪|猫侯爵|猫)"
_PIECE = r"(?:warriors?|units?|buildings?|roost|兵|战士|建筑(?:物)?|巢穴)"
_COUNT = r"(?:-?\d+|零|〇|一(?:个|只)?|二(?:个)?|两(?:个|只)?|三(?:个)?|四(?:个)?|五(?:个)?|六(?:个)?|七(?:个)?|八(?:个)?|九(?:个)?|十)"
_CLEARING = r"[A-Za-z][A-Za-z0-9_-]{0,63}"


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _targets(value: str) -> list[str]:
    return _unique(
        _clearing(item)
        for item in re.split(r"\s*(?:、|，|,|和|及|and|&)\s*", value)
        if _clearing(item)
    )


def _has_path(state: dict[str, Any], path: str) -> bool:
    current: Any = state
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def _scope_assumption(
    text: str, message_id: str, *, asserted_value: bool | None = True
) -> ProposedScopeAssumption:
    return ProposedScopeAssumption(
        scope_id="root-local-move",
        predicate_id="m0.local_warrior_move_scope",
        asserted_value=asserted_value,
        ruleset_version=M0_RULESET_VERSION,
        scope_policy_version=M0_SCOPE_POLICY_VERSION,
        evidence_refs=[_evidence("scope.root-local-move", message_id, text)],
    )


class NaturalLanguageStateExtractor:
    """Turn a small, explicit vocabulary into a proposed state patch."""

    def extract(
        self,
        text: str,
        *,
        message_id: str,
        expected_revision: int,
        current_state: dict[str, Any] | None = None,
    ) -> ExtractionResult:
        text = text.strip()
        state = current_state or {}
        issues: list[dict[str, str]] = []
        changes: list[ProposedFactChange] = []
        scope_assumptions: list[ProposedScopeAssumption] = []
        completeness_assertions: list[ProposedCompletenessAssertion] = []
        unknown_fields: list[str] = []
        recognized = False
        seen_changes: set[tuple[str, str]] = set()
        seen_assertions: set[tuple[str, str]] = set()

        unsupported = re.search(
            r"\b(?:vagabond|riverfolk|lizard|lizard cult|hirelings)\b|侠客|河民|蜥蜴|雇佣兵",
            text,
            re.IGNORECASE,
        )
        explicitly_excluded = re.search(r"不涉及|不包括|排除|excluding|except", text, re.IGNORECASE)
        if unsupported and not explicitly_excluded:
            return ExtractionResult(
                patch=None,
                issues=[
                    _issue(
                        "SCOPE_OUT_OF_BOUNDS",
                        "This declaration names a faction or interaction outside the M0 local move scope.",
                    )
                ],
                unknown_fields=[],
                recognized=True,
            )

        actor = None
        actor_match = re.search(_FACTION, text, re.IGNORECASE)
        if actor_match:
            actor = _faction(actor_match.group(0))

        explicit_scope = re.search(
            r"普通移动|局部(?:移动|裁决)|范围|scope|only\s+includes|只按|仅按|root\s+move|m0",
            text,
            re.IGNORECASE,
        )
        if explicit_scope:
            recognized = True
            asserted_value = (
                None if re.search(r"不确定|尚未确认|unknown", text, re.IGNORECASE) else True
            )
            if re.search(r"不是|不属于|不适用", text, re.IGNORECASE):
                asserted_value = False
            scope_assumptions.append(
                _scope_assumption(text, message_id, asserted_value=asserted_value)
            )

        move_match = re.search(
            rf"(?:从|from)\s*(?P<origin>{_CLEARING})\s*(?:到|移到|移动到|to|→|->)\s*(?P<destination>{_CLEARING})",
            text,
            re.IGNORECASE,
        )
        if move_match or re.search(r"移动|\bmove\b", text, re.IGNORECASE):
            recognized = True
        if move_match:
            origin = _clearing(move_match.group("origin"))
            destination = _clearing(move_match.group("destination"))
            source_span = move_match.group(0)
            for field_path, value in (
                ("action.type", "move"),
                ("action.origin", origin),
                ("action.destination", destination),
            ):
                key = (field_path, repr(value))
                if key not in seen_changes:
                    changes.append(
                        ProposedFactChange(
                            operation="assert",
                            field_path=field_path,
                            value=value,
                            evidence=[_evidence(field_path, message_id, source_span)],
                        )
                    )
                    seen_changes.add(key)
            if actor:
                changes.append(
                    ProposedFactChange(
                        operation="assert",
                        field_path="action.actor",
                        value=actor,
                        evidence=[_evidence("action.actor", message_id, source_span)],
                    )
                )
            if not _has_path(state, f"clearings.{origin}.presence"):
                unknown_fields.append(f"clearings.{origin}.presence")
            if not _has_path(state, f"clearings.{destination}.presence"):
                unknown_fields.append(f"clearings.{destination}.presence")

        presence_patterns = [
            re.compile(
                rf"(?P<faction>{_FACTION})\s*(?:在|于|in|at)\s*(?P<clearing>{_CLEARING})\s*"
                rf"(?:(?P<only>只有|仅有|has\s+only)|有|拥有|has|have)\s*"
                rf"(?P<count>{_COUNT})\s*(?:个|只)?\s*(?P<piece>{_PIECE})",
                re.IGNORECASE,
            ),
            re.compile(
                rf"(?P<clearing>{_CLEARING})\s*(?:(?P<only>只有|仅有|has\s+only)|有|拥有|has|have)\s*"
                rf"(?P<count>{_COUNT})\s*(?:个|只)?\s*"
                rf"(?:(?P<faction>{_FACTION})\s*)?(?P<piece>{_PIECE})",
                re.IGNORECASE,
            ),
        ]
        for pattern in presence_patterns:
            for match in pattern.finditer(text):
                recognized = True
                clearing = _clearing(match.group("clearing"))
                faction = _faction(match.group("faction")) or actor
                piece = _piece(match.group("piece"))
                count = _count(match.group("count"))
                source_span = match.group(0)
                if faction is None:
                    issues.append(
                        _issue(
                            "AMBIGUOUS_FACTION",
                            "The piece count does not identify a supported faction.",
                            f"clearings.{clearing}.presence",
                        )
                    )
                    continue
                if piece is None or count is None:
                    issues.append(
                        _issue(
                            "INVALID_STATE_VALUE",
                            "The piece count or piece type could not be understood.",
                            f"clearings.{clearing}.presence",
                        )
                    )
                    continue
                field_path = f"clearings.{clearing}.presence.{faction}.{piece}"
                if count < 0:
                    issues.append(
                        _issue(
                            "INVALID_COUNT",
                            "Piece counts must be zero or a positive integer; no fact was accepted.",
                            field_path,
                        )
                    )
                    continue
                key = (field_path, repr(count))
                if key not in seen_changes:
                    changes.append(
                        ProposedFactChange(
                            operation="assert",
                            field_path=field_path,
                            value=count,
                            evidence=[_evidence(field_path, message_id, source_span)],
                        )
                    )
                    seen_changes.add(key)

                is_complete = bool(match.group("only")) or bool(
                    re.search(
                        r"完整|清单",
                        text[max(0, match.start() - 4) : min(len(text), match.end() + 12)],
                    )
                )
                if is_complete:
                    assertion_key = (clearing, f"presence:{faction}:{piece}")
                    if assertion_key not in seen_assertions:
                        members = (
                            []
                            if count == 0
                            else [{"faction": faction, "piece_type": piece, "count": count}]
                        )
                        covered_scope = {
                            "kind": "presence",
                            "clearing_id": clearing,
                            "faction": faction,
                            "piece_type": piece,
                        }
                        collection_target = f"clearings.{clearing}.presence"
                        completeness_assertions.append(
                            ProposedCompletenessAssertion(
                                collection_target=collection_target,
                                covered_scope=covered_scope,
                                completeness="complete",
                                member_snapshot={"members": members},
                                evidence_refs=[
                                    _evidence(collection_target, message_id, source_span)
                                ],
                            )
                        )
                        seen_assertions.add(assertion_key)

        empty_pattern = re.compile(
            rf"(?P<clearing>{_CLEARING})\s*(?:是|为|系)?\s*(?:空的|为空|没有任何(?:棋子|单位))",
            re.IGNORECASE,
        )
        for match in empty_pattern.finditer(text):
            recognized = True
            clearing = _clearing(match.group("clearing"))
            collection_target = f"clearings.{clearing}.presence"
            assertion_key = (clearing, "presence:all:all")
            if assertion_key not in seen_assertions:
                completeness_assertions.append(
                    ProposedCompletenessAssertion(
                        collection_target=collection_target,
                        covered_scope={
                            "kind": "presence",
                            "clearing_id": clearing,
                            "faction": None,
                            "piece_type": None,
                        },
                        completeness="complete",
                        member_snapshot={"members": []},
                        evidence_refs=[_evidence(collection_target, message_id, match.group(0))],
                    )
                )
                seen_assertions.add(assertion_key)

        adjacency_patterns = [
            re.compile(
                rf"(?P<origin>{_CLEARING})\s*(?P<only>只|仅)?\s*(?:与|和|跟)\s*"
                rf"(?P<targets>{_CLEARING}(?:\s*(?:、|，|,|和|及|and|&)\s*{_CLEARING})*)\s*相邻",
                re.IGNORECASE,
            ),
            re.compile(
                rf"(?P<origin>{_CLEARING})\s*(?:的)?\s*(?:相邻地点|邻接地点)\s*"
                rf"(?:是|有|包括|:|：)?\s*(?P<targets>{_CLEARING}(?:\s*(?:、|，|,|和|及|and|&)\s*{_CLEARING})*)",
                re.IGNORECASE,
            ),
            re.compile(
                rf"(?P<origin>{_CLEARING})\s+(?:is\s+)?adjacent\s+to\s*"
                rf"(?P<targets>{_CLEARING}(?:\s*(?:,|and)\s*{_CLEARING})*)",
                re.IGNORECASE,
            ),
        ]
        for pattern in adjacency_patterns:
            for match in pattern.finditer(text):
                recognized = True
                origin = _clearing(match.group("origin"))
                target_ids = [item for item in _targets(match.group("targets")) if item != origin]
                if not target_ids:
                    issues.append(
                        _issue(
                            "INVALID_ADJACENCY",
                            "An adjacency declaration must name a different clearing.",
                            f"clearings.{origin}.adjacent_to",
                        )
                    )
                    continue
                field_path = f"clearings.{origin}.adjacent_to"
                key = (field_path, repr(target_ids))
                if key not in seen_changes:
                    changes.append(
                        ProposedFactChange(
                            operation="assert",
                            field_path=field_path,
                            value=target_ids,
                            evidence=[_evidence(field_path, message_id, match.group(0))],
                        )
                    )
                    seen_changes.add(key)
                is_complete = bool(match.groupdict().get("only")) or bool(
                    re.search(
                        r"完整|所有|全部|only|all",
                        text[max(0, match.start() - 8) : match.end() + 8],
                        re.IGNORECASE,
                    )
                )
                completeness = "complete" if is_complete else "partial"
                assertion_key = (origin, f"adjacency:{','.join(target_ids)}:{completeness}")
                if assertion_key not in seen_assertions:
                    completeness_assertions.append(
                        ProposedCompletenessAssertion(
                            collection_target=field_path,
                            covered_scope={
                                "kind": "adjacency",
                                "clearing_id": origin,
                                "origin": origin,
                                "members": "all_adjacent_clearings",
                            },
                            completeness=completeness,
                            member_snapshot={"members": target_ids},
                            evidence_refs=[_evidence(field_path, message_id, match.group(0))],
                        )
                    )
                    seen_assertions.add(assertion_key)
                if not is_complete:
                    unknown_fields.append(f"{field_path}.complete")

        if issues:
            return ExtractionResult(
                patch=None,
                issues=issues,
                unknown_fields=_unique(unknown_fields),
                recognized=recognized,
            )
        if not recognized or not changes and not scope_assumptions and not completeness_assertions:
            return ExtractionResult(
                patch=None,
                issues=[],
                unknown_fields=_unique(unknown_fields),
                recognized=False,
            )

        try:
            patch = ProposedStatePatch(
                expected_state_revision=expected_revision,
                changes=changes,
                scope_assumptions=scope_assumptions,
                completeness_assertions=completeness_assertions,
                unknown_fields=_unique(unknown_fields),
            )
        except ValueError as exc:
            return ExtractionResult(
                patch=None,
                issues=[_issue("STATE_SCHEMA_INVALID", str(exc))],
                unknown_fields=_unique(unknown_fields),
                recognized=True,
            )
        return ExtractionResult(
            patch=patch,
            issues=[],
            unknown_fields=_unique(unknown_fields),
            recognized=True,
        )


def validate_fact_change(change: ProposedFactChange) -> dict[str, str] | None:
    """Validate a fact path/value without mutating state."""

    parts = change.field_path.split(".")
    if any(not item for item in parts):
        return _issue(
            "INVALID_FIELD_PATH", "Fact paths must not contain empty segments.", change.field_path
        )
    if parts[0] in {"confirmed_state", "revision", "verdict", "scope_assumptions"}:
        return _issue(
            "FORBIDDEN_FIELD",
            "Controller-owned fields cannot be changed by a state patch.",
            change.field_path,
        )
    valid = False
    expected: str | None = None
    if parts in [["actor"], ["phase"]]:
        valid = True
        expected = "string"
    elif (
        len(parts) == 2
        and parts[0] == "action"
        and parts[1]
        in {
            "type",
            "actor",
            "origin",
            "destination",
            "warrior_count",
        }
    ):
        valid = True
        expected = "integer" if parts[1] == "warrior_count" else "string"
    elif len(parts) == 2 and parts[0] == "decree" and parts[1] in {"column", "card_suit"}:
        valid = True
        expected = "string"
    elif len(parts) == 3 and parts[0] == "clearings" and parts[2] in {"suit", "adjacent_to"}:
        valid = bool(SUPPORTED_CLEARING_RE.fullmatch(parts[1]))
        expected = "list" if parts[2] == "adjacent_to" else "string"
    elif (
        len(parts) == 5
        and parts[0] == "clearings"
        and parts[2] == "presence"
        and parts[3] in SUPPORTED_FACTIONS
        and parts[4] in SUPPORTED_PIECES
        and SUPPORTED_CLEARING_RE.fullmatch(parts[1])
    ):
        valid = True
        expected = "integer"
    if not valid:
        return _issue(
            "FORBIDDEN_FIELD",
            "The field is outside the supported Root state schema.",
            change.field_path,
        )
    if change.operation == "retract":
        return None
    if expected == "integer" and (
        isinstance(change.value, bool) or not isinstance(change.value, int) or change.value < 0
    ):
        return _issue(
            "INVALID_COUNT", "Piece counts must be non-negative integers.", change.field_path
        )
    if expected == "list" and (
        not isinstance(change.value, list)
        or any(
            not isinstance(item, str) or not SUPPORTED_CLEARING_RE.fullmatch(item)
            for item in change.value
        )
    ):
        return _issue(
            "INVALID_ADJACENCY",
            "Adjacent clearing IDs must be a list of valid IDs.",
            change.field_path,
        )
    if parts[-1] == "type" and change.value != "move":
        return _issue(
            "UNSUPPORTED_ACTION", "M0 only accepts the warrior Move action.", change.field_path
        )
    if parts[-1] == "actor" and change.value not in SUPPORTED_FACTIONS:
        return _issue(
            "UNSUPPORTED_FACTION", "M0 supports only Eyrie and Marquise.", change.field_path
        )
    if parts[-1] == "phase" and change.value not in {"birdsong", "daylight", "evening"}:
        return _issue(
            "INVALID_PHASE", "The phase is not a supported Root phase.", change.field_path
        )
    return None


def validate_scope_assumption(assumption: ProposedScopeAssumption) -> dict[str, str] | None:
    if assumption.scope_id != "root-local-move":
        return _issue("SCOPE_OUT_OF_BOUNDS", "The scope is outside the M0 local move boundary.")
    if assumption.ruleset_version != M0_RULESET_VERSION:
        return _issue(
            "RULESET_OUT_OF_BOUNDS", "The declaration names an unsupported ruleset version."
        )
    if assumption.scope_policy_version != M0_SCOPE_POLICY_VERSION:
        return _issue(
            "SCOPE_POLICY_OUT_OF_BOUNDS", "The declaration names an unsupported scope policy."
        )
    return None


def validate_completeness_assertion(
    assertion: ProposedCompletenessAssertion,
) -> dict[str, str] | None:
    parts = assertion.collection_target.split(".")
    if len(parts) != 3 or parts[0] != "clearings" or not SUPPORTED_CLEARING_RE.fullmatch(parts[1]):
        return _issue(
            "DECLARATION_OUT_OF_BOUNDS",
            "Completeness must name a supported clearing collection.",
            assertion.collection_target,
        )
    kind = assertion.covered_scope.get("kind")
    clearing_id = assertion.covered_scope.get("clearing_id")
    if kind not in {"presence", "adjacency"} or clearing_id != parts[1]:
        return _issue(
            "DECLARATION_OUT_OF_BOUNDS",
            "The covered scope must be a structured presence or adjacency scope for the same clearing.",
            assertion.collection_target,
        )
    if kind == "presence":
        if parts[2] != "presence":
            return _issue(
                "DECLARATION_OUT_OF_BOUNDS",
                "Presence declarations target presence.",
                assertion.collection_target,
            )
        faction = assertion.covered_scope.get("faction")
        piece = assertion.covered_scope.get("piece_type")
        if faction is not None and faction not in SUPPORTED_FACTIONS:
            return _issue(
                "DECLARATION_OUT_OF_BOUNDS",
                "The faction is outside the M0 scope.",
                assertion.collection_target,
            )
        if piece is not None and piece not in SUPPORTED_PIECES:
            return _issue(
                "DECLARATION_OUT_OF_BOUNDS",
                "The piece type is outside the M0 scope.",
                assertion.collection_target,
            )
    elif parts[2] != "adjacent_to":
        return _issue(
            "DECLARATION_OUT_OF_BOUNDS",
            "Adjacency declarations target adjacent_to.",
            assertion.collection_target,
        )
    members = assertion.member_snapshot.get("members")
    if not isinstance(members, list):
        return _issue("INVALID_MEMBER_SNAPSHOT", "The member snapshot must contain a members list.")
    return None
