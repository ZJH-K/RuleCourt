from rulecourt.state import ProposedFactChange, ProposedStatePatch, StateEvidence
from rulecourt.state_store import StateStore
from rulecourt.store import CaseStore

FIELD = "clearings.A.presence.marquise.warriors"


def fact_change(*, operation, value, message_id, supersedes_id=None):
    return ProposedFactChange(
        operation=operation,
        field_path=FIELD,
        value=value,
        supersedes_id=supersedes_id,
        evidence=[
            StateEvidence(
                field_path=FIELD,
                source_message_id=message_id,
                source_span="A has the stated number of warriors",
            )
        ],
    )


def apply(case_id, revision, change):
    return state_store.apply_patch(
        case_id,
        ProposedStatePatch(expected_state_revision=revision, changes=[change]),
    )


def test_cross_case_and_expired_replacement_objects_are_rejected_atomically(tmp_path):
    global state_store
    case_store = CaseStore(tmp_path / "cases.sqlite3")
    state_store = StateStore(tmp_path / "cases.sqlite3")
    first_case = case_store.create()["id"]
    second_case = case_store.create()["id"]
    first_message = case_store.add_message(first_case, "A has 2 Marquise warriors.")
    second_message = case_store.add_message(second_case, "A has 5 Marquise warriors.")
    first_correction_message = case_store.add_message(
        first_case, "Correction: A has 3 Marquise warriors."
    )
    second_correction_message = case_store.add_message(
        second_case, "Correction: A has 6 Marquise warriors."
    )

    assert apply(
        first_case,
        0,
        fact_change(operation="assert", value=2, message_id=first_message),
    )["accepted"]
    assert apply(
        second_case,
        0,
        fact_change(operation="assert", value=5, message_id=second_message),
    )["accepted"]
    first_fact_id = state_store.view(first_case)["state_facts"][0]["id"]

    cross_case = apply(
        second_case,
        1,
        fact_change(
            operation="correct",
            value=6,
            message_id=second_correction_message,
            supersedes_id=first_fact_id,
        ),
    )
    assert cross_case["accepted"] is False
    assert cross_case["issues"][0]["code"] == "CROSS_CASE_REFERENCE"
    assert state_store.view(second_case)["state_revision"] == 1
    assert (
        state_store.view(second_case)["confirmed_state"]["clearings"]["A"]["presence"]["marquise"][
            "warriors"
        ]
        == 5
    )

    assert apply(
        first_case,
        1,
        fact_change(
            operation="correct",
            value=3,
            message_id=first_correction_message,
            supersedes_id=first_fact_id,
        ),
    )["accepted"]
    stale = apply(
        first_case,
        2,
        fact_change(
            operation="correct",
            value=4,
            message_id=first_correction_message,
            supersedes_id=first_fact_id,
        ),
    )
    assert stale["accepted"] is False
    assert stale["issues"][0]["code"] == "STALE_OBJECT_REFERENCE"
    assert state_store.view(first_case)["state_revision"] == 2
    assert (
        state_store.view(first_case)["confirmed_state"]["clearings"]["A"]["presence"]["marquise"][
            "warriors"
        ]
        == 3
    )


def test_correction_requires_explicit_source_wording(tmp_path):
    case_store = CaseStore(tmp_path / "cases.sqlite3")
    state_store = StateStore(tmp_path / "cases.sqlite3")
    case_id = case_store.create()["id"]
    original_message = case_store.add_message(case_id, "A has 2 Marquise warriors.")

    assert state_store.apply_patch(
        case_id,
        ProposedStatePatch(
            expected_state_revision=0,
            changes=[fact_change(operation="assert", value=2, message_id=original_message)],
        ),
    )["accepted"]
    rejected = state_store.apply_patch(
        case_id,
        ProposedStatePatch(
            expected_state_revision=1,
            changes=[
                fact_change(
                    operation="correct",
                    value=3,
                    message_id=original_message,
                    supersedes_id=state_store.view(case_id)["state_facts"][0]["id"],
                )
            ],
        ),
    )

    assert rejected["accepted"] is False
    assert {issue["code"] for issue in rejected["issues"]} == {"OPERATION_NOT_EXPLICIT"}
