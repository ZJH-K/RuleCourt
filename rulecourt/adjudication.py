"""Deterministic Root move predicates used by the fixed adjudication workflow."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DerivedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["resolved", "unknown", "unsupported"]
    value: Any | None = None
    reason_codes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["allow", "deny", "unknown", "unsupported"]
    reason_codes: list[str] = Field(default_factory=list)
    rule_ids: list[str] = Field(default_factory=list)
    derived_facts: dict[str, Any] = Field(default_factory=dict)
    missing_fields: list[str] = Field(default_factory=list)


DEFAULT_RULE_IDS = {
    "path": "root-2.2",
    "rule": "root-2.5",
    "move": "root-4.2",
    "move_restriction": "root-4.2.1",
    "eyrie_rule": "root-7.2.2",
}


def _rule_ids(overrides: dict[str, str] | None) -> dict[str, str]:
    result = dict(DEFAULT_RULE_IDS)
    if overrides:
        result.update(overrides)
    return result


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _clearing(state: dict[str, Any], clearing_id: str) -> dict[str, Any] | None:
    clearings = state.get("clearings")
    if not isinstance(clearings, dict):
        return None
    value = clearings.get(clearing_id)
    return value if isinstance(value, dict) else None


def _assertions(state: dict[str, Any]) -> list[dict[str, Any]]:
    values = state.get("completeness_assertions", [])
    return [item for item in values if isinstance(item, dict)]


def _is_complete(
    state: dict[str, Any], collection_target: str, *, clearing_id: str | None = None
) -> bool:
    for assertion in _assertions(state):
        if assertion.get("status") != "confirmed":
            continue
        if assertion.get("completeness") != "complete":
            continue
        if assertion.get("collection_target") != collection_target:
            continue
        scope = assertion.get("covered_scope")
        if not isinstance(scope, dict):
            continue
        if clearing_id is not None and scope.get("clearing_id") != clearing_id:
            continue
        return True
    return False


def _known_count(state: dict[str, Any], clearing_id: str, faction: str, piece: str) -> int | None:
    clearing = _clearing(state, clearing_id)
    if clearing is not None:
        presence = clearing.get("presence")
        if isinstance(presence, dict):
            faction_presence = presence.get(faction)
            if isinstance(faction_presence, dict) and piece in faction_presence:
                value = faction_presence[piece]
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    return value

    target = f"clearings.{clearing_id}.presence"
    for assertion in _assertions(state):
        if assertion.get("status") != "confirmed":
            continue
        if assertion.get("completeness") != "complete":
            continue
        if assertion.get("collection_target") != target:
            continue
        scope = assertion.get("covered_scope")
        if not isinstance(scope, dict):
            continue
        if scope.get("clearing_id") != clearing_id:
            continue
        if scope.get("faction") not in (None, faction):
            continue
        if scope.get("piece_type") not in (None, piece):
            continue
        return 0
    return None


def compute_ruler(
    clearing: dict[str, Any],
    *,
    clearing_id: str | None = None,
    completeness_assertions: list[dict[str, Any]] | None = None,
    rule_ids: dict[str, str] | None = None,
) -> DerivedFact:
    """Compute a clearing ruler without treating omitted counts as zero."""

    ids = _rule_ids(rule_ids)
    context: dict[str, Any] = {"clearings": {}}
    if clearing_id is not None:
        context["clearings"][clearing_id] = clearing
        context["completeness_assertions"] = completeness_assertions or []
    else:
        context["clearings"]["_clearing"] = clearing
        context["completeness_assertions"] = completeness_assertions or []
        clearing_id = "_clearing"

    counts: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    for faction in ("eyrie", "marquise"):
        counts[faction] = {}
        for piece in ("warriors", "buildings"):
            count = _known_count(context, clearing_id, faction, piece)
            if count is None:
                missing.append(f"clearings.{clearing_id}.presence.{faction}.{piece}")
            else:
                counts[faction][piece] = count
    if missing:
        return DerivedFact(
            status="unknown",
            reason_codes=["RULER_STATE_MISSING"],
            rule_ids=[ids["rule"]],
            missing_fields=missing,
        )

    scores = {
        faction: values["warriors"] + values["buildings"] for faction, values in counts.items()
    }
    highest = max(scores.values())
    leaders = [faction for faction, score in scores.items() if score == highest]
    if len(leaders) == 1:
        return DerivedFact(
            status="resolved",
            value=leaders[0],
            reason_codes=["RULER_UNIQUE_HIGHEST"],
            rule_ids=[ids["rule"]],
        )
    if "eyrie" in leaders and scores["eyrie"] > 0:
        return DerivedFact(
            status="resolved",
            value="eyrie",
            reason_codes=["EYRIE_RULE_TIE_OVERRIDE"],
            rule_ids=[ids["rule"], ids["eyrie_rule"]],
        )
    return DerivedFact(
        status="resolved",
        value=None,
        reason_codes=["RULER_TIE"],
        rule_ids=[ids["rule"]],
    )


def _adjacency(
    state: dict[str, Any], origin: str, destination: str, ids: dict[str, str]
) -> DerivedFact:
    clearing = _clearing(state, origin)
    if clearing is None:
        return DerivedFact(
            status="unknown",
            reason_codes=["MOVE_ORIGIN_STATE_MISSING"],
            rule_ids=[ids["path"], ids["move"]],
            missing_fields=[f"clearings.{origin}.adjacent_to"],
        )

    adjacent = clearing.get("adjacent_to")
    if isinstance(adjacent, (list, set, tuple)) and destination in adjacent:
        return DerivedFact(
            status="resolved",
            value=True,
            reason_codes=["MOVE_ADJACENT"],
            rule_ids=[ids["path"], ids["move"]],
        )
    known_non_adjacent = clearing.get("known_non_adjacent_to", [])
    if isinstance(known_non_adjacent, (list, set, tuple)) and destination in known_non_adjacent:
        return DerivedFact(
            status="resolved",
            value=False,
            reason_codes=["MOVE_NOT_ADJACENT"],
            rule_ids=[ids["path"], ids["move"]],
        )
    if _is_complete(state, f"clearings.{origin}.adjacent_to", clearing_id=origin):
        return DerivedFact(
            status="resolved",
            value=False,
            reason_codes=["MOVE_NOT_ADJACENT"],
            rule_ids=[ids["path"], ids["move"]],
        )
    return DerivedFact(
        status="unknown",
        reason_codes=["MOVE_ADJACENCY_UNKNOWN"],
        rule_ids=[ids["path"], ids["move"]],
        missing_fields=[f"clearings.{origin}.adjacent_to"],
    )


def _decision(
    status: Literal["allow", "deny", "unknown", "unsupported"],
    reason_codes: list[str],
    rule_ids: list[str],
    *,
    derived_facts: dict[str, Any] | None = None,
    missing_fields: list[str] | None = None,
) -> Decision:
    return Decision(
        status=status,
        reason_codes=_unique(reason_codes),
        rule_ids=_unique(rule_ids),
        derived_facts=derived_facts or {},
        missing_fields=_unique(missing_fields or []),
    )


def validate_move(state: dict[str, Any], *, rule_ids: dict[str, str] | None = None) -> Decision:
    """Validate ordinary warrior movement using three-valued domain logic."""

    ids = _rule_ids(rule_ids)
    action = state.get("action")
    if not isinstance(action, dict):
        return _decision(
            "unknown", ["MOVE_ACTION_MISSING"], [ids["move"]], missing_fields=["action"]
        )
    if action.get("type") != "move":
        return _decision("unsupported", ["UNSUPPORTED_ACTION"], [ids["move"]])

    actor = action.get("actor")
    if actor not in {"eyrie", "marquise"}:
        return _decision(
            "unknown",
            ["MOVE_ACTOR_MISSING"],
            [ids["move"]],
            missing_fields=["action.actor"],
        )
    origin = action.get("origin")
    destination = action.get("destination")
    count = action.get("warrior_count")
    missing_action = [
        path
        for path, value in (
            ("action.origin", origin),
            ("action.destination", destination),
            ("action.warrior_count", count),
        )
        if value is None
    ]
    if missing_action:
        return _decision(
            "unknown",
            ["MOVE_ACTION_STATE_MISSING"],
            [ids["move"]],
            missing_fields=missing_action,
        )
    if not isinstance(origin, str) or not isinstance(destination, str):
        return _decision("unsupported", ["INVALID_CLEARING_ID"], [ids["move"]])
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return _decision("deny", ["MOVE_INVALID_COUNT"], [ids["move"]])
    if count == 0:
        return _decision("deny", ["MOVE_ZERO_PIECES"], [ids["move"]])

    adjacent = _adjacency(state, origin, destination, ids)
    if adjacent.status == "resolved" and adjacent.value is False:
        return _decision("deny", adjacent.reason_codes, adjacent.rule_ids)
    if adjacent.status == "unknown":
        return _decision(
            "unknown",
            adjacent.reason_codes,
            adjacent.rule_ids,
            missing_fields=adjacent.missing_fields,
        )

    available = _known_count(state, origin, actor, "warriors")
    if available is None:
        return _decision(
            "unknown",
            ["MOVE_ORIGIN_STATE_MISSING"],
            [ids["move"]],
            missing_fields=[f"clearings.{origin}.presence.{actor}.warriors"],
        )
    if available < count:
        return _decision("deny", ["MOVE_INSUFFICIENT_WARRIORS"], [ids["move"]])

    origin_data = _clearing(state, origin)
    destination_data = _clearing(state, destination)
    origin_ruler = (
        compute_ruler(
            origin_data or {},
            clearing_id=origin,
            completeness_assertions=_assertions(state),
            rule_ids=ids,
        )
        if origin_data is not None
        else DerivedFact(
            status="unknown",
            reason_codes=["MOVE_ORIGIN_STATE_MISSING"],
            rule_ids=[ids["rule"]],
            missing_fields=[f"clearings.{origin}.presence"],
        )
    )
    destination_ruler = (
        compute_ruler(
            destination_data or {},
            clearing_id=destination,
            completeness_assertions=_assertions(state),
            rule_ids=ids,
        )
        if destination_data is not None
        else DerivedFact(
            status="unknown",
            reason_codes=["MOVE_DEST_STATE_MISSING"],
            rule_ids=[ids["rule"]],
            missing_fields=[f"clearings.{destination}.presence"],
        )
    )
    derived = {
        "origin_ruler": origin_ruler.model_dump(),
        "destination_ruler": destination_ruler.model_dump(),
        "origin_warriors_available": available,
    }
    ruler_rule_ids = origin_ruler.rule_ids + destination_ruler.rule_ids
    if origin_ruler.status == "resolved" and origin_ruler.value == actor:
        return _decision(
            "allow",
            ["MOVE_RULES_ORIGIN"],
            [ids["path"], ids["move"], ids["move_restriction"], *ruler_rule_ids],
            derived_facts=derived,
        )
    if destination_ruler.status == "resolved" and destination_ruler.value == actor:
        return _decision(
            "allow",
            ["MOVE_RULES_DESTINATION"],
            [ids["path"], ids["move"], ids["move_restriction"], *ruler_rule_ids],
            derived_facts=derived,
        )
    if origin_ruler.status == "resolved" and destination_ruler.status == "resolved":
        return _decision(
            "deny",
            ["MOVE_RULES_NEITHER_CLEARING"],
            [ids["path"], ids["move"], ids["move_restriction"], *ruler_rule_ids],
            derived_facts=derived,
        )
    missing = origin_ruler.missing_fields + destination_ruler.missing_fields
    return _decision(
        "unknown",
        ["MOVE_RULE_STATE_MISSING"],
        [ids["path"], ids["move"], ids["move_restriction"], *ruler_rule_ids],
        derived_facts=derived,
        missing_fields=missing,
    )
