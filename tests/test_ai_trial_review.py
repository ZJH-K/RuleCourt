import hashlib
import json
from copy import deepcopy
from pathlib import Path

import pytest

from rulecourt.evaluation import (
    CaseDataset,
    ReplayResult,
    main,
    score_results,
    score_results_ai_trial,
    validate_ai_trial_attestation,
)

ROOT = Path(__file__).parents[1]
DATASET = ROOT / "examples" / "m0-candidate-cases.json"
RULES = ROOT / "examples" / "root-m0-candidate-package.json"
ATTESTATION = ROOT / "docs" / "research" / "root-m0-ai-attestation.json"


def _materials():
    return (
        CaseDataset.from_json(DATASET),
        json.loads(RULES.read_text(encoding="utf-8")),
        json.loads(ATTESTATION.read_text(encoding="utf-8")),
    )


def _digest_attestation(value):
    payload = {key: item for key, item in value.items() if key != "attestation_payload_sha256"}
    value["attestation_payload_sha256"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def test_ai_attestation_scores_candidate_only_as_explicit_trial(capsys):
    dataset, rules, attestation = _materials()
    assert len(validate_ai_trial_attestation(dataset, rules, attestation)) == 13
    assert (
        main(
            ["validate", str(DATASET), "--ai-attestation", str(ATTESTATION), "--rules", str(RULES)]
        )
        == 0
    )
    assert '"ai_trial_count": 13' in capsys.readouterr().out
    with pytest.raises(ValueError, match="cannot be used as a human formal signoff"):
        main(
            [
                "validate",
                str(DATASET),
                "--formal",
                "--ai-attestation",
                str(ATTESTATION),
                "--rules",
                str(RULES),
            ]
        )

    outcomes = [
        ReplayResult(
            case_id=case.id,
            family_id=case.family_id,
            category=case.category,
            initial_result={"status": case.initial_label},
            complete_result={"status": case.complete_label},
        )
        for case in dataset.cases
    ]
    trial = score_results_ai_trial(dataset, outcomes, rule_package=rules, attestation=attestation)
    assert trial.review_basis == "ai_trial"
    assert trial.scored_case_count == 13
    assert trial.excluded_counts == {"draft": 0, "disputed": 0}
    assert "Review basis: ai_trial" in trial.to_markdown()

    with pytest.raises(ValueError, match="family split"):
        score_results(dataset, outcomes, formal=True)


def test_ai_attestation_rejects_tampered_case_and_rule_materials():
    dataset, rules, attestation = _materials()
    tampered = deepcopy(attestation)
    tampered["case_reviews"][0]["complete_label"] = "ILLEGAL"
    with pytest.raises(ValueError, match="payload digest"):
        validate_ai_trial_attestation(dataset, rules, tampered)
    _digest_attestation(tampered)
    with pytest.raises(ValueError, match="does not approve Case"):
        validate_ai_trial_attestation(dataset, rules, tampered)

    changed_rules = deepcopy(rules)
    changed_rules["rules"][0]["text"] = "changed without review"
    with pytest.raises(ValueError, match="rule package"):
        validate_ai_trial_attestation(dataset, changed_rules, attestation)

    masquerade = deepcopy(attestation)
    masquerade["human_verified"] = True
    _digest_attestation(masquerade)
    with pytest.raises(ValueError, match="non-human and non-formal"):
        validate_ai_trial_attestation(dataset, rules, masquerade)
