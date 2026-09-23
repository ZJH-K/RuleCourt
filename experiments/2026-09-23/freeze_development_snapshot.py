"""Bind all five real development runs to an AI-trial snapshot, not a formal release."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rulecourt.baselines import BaselineReport
from rulecourt.comparison import TreatmentComparisonReport
from rulecourt.evaluation import CaseDataset, ReplayResult, score_results_ai_trial

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
BASELINE = HERE / "t13-development-results.json"
TREATMENT = HERE / "t14-development-results.json"
OUTPUT = HERE / "five-group-development-snapshot.json"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    if OUTPUT.exists():
        raise FileExistsError(f"refusing to overwrite snapshot: {OUTPUT}")
    dataset_path = ROOT / "examples/m0-candidate-cases.json"
    rule_path = ROOT / "examples/root-m0-candidate-package.json"
    attestation_path = ROOT / "docs/research/root-m0-ai-attestation.json"
    dataset = CaseDataset.from_json(dataset_path)
    rule_package = json.loads(rule_path.read_text(encoding="utf-8"))
    attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
    baseline = BaselineReport.from_json(BASELINE)
    treatment = TreatmentComparisonReport.from_json(TREATMENT)
    groups = {}
    for name, run in {**baseline.runs, **treatment.runs}.items():
        results = [ReplayResult.model_validate(row) for row in run.results]
        report = score_results_ai_trial(
            dataset, results, rule_package=rule_package, attestation=attestation
        )
        groups[name] = {
            "case_count": len(results),
            "run_kind": "development_trial",
            "review_basis": "ai_trial",
            "initial": report.initial.model_dump(mode="json"),
            "complete": report.complete.model_dump(mode="json"),
            "usage": run.usage,
            "config_hash": run.config.get("config_hash"),
            "failure_case_ids": sorted(
                result.case_id for result in results if result.system_failure
            ),
        }
    expected = {"llm_only", "vanilla_rag", "hybrid_rag", "dynamic_agent", "fixed_workflow"}
    if set(groups) != expected:
        raise ValueError("snapshot requires all five group outputs")
    baseline_budget = baseline.runs["hybrid_rag"].config
    treatment_budget = treatment.config["budget"]
    if (
        baseline_budget["max_total_tokens"] != treatment_budget["max_total_tokens"]
        or baseline_budget["timeout_seconds"] != treatment_budget["max_duration_seconds"]
    ):
        raise ValueError("five-group token and time ceilings are not aligned")
    referenced = {
        "dataset": dataset_path,
        "candidate_rules": rule_path,
        "ai_attestation": attestation_path,
        "baseline_config": HERE / "t13-development-plan.json",
        "baseline_report": BASELINE,
        "treatment_config": HERE / "t14-development-config.json",
        "treatment_report": TREATMENT,
    }
    code = [
        ROOT / "rulecourt/baselines.py",
        ROOT / "rulecourt/hybrid_baselines.py",
        ROOT / "rulecourt/embeddings.py",
        ROOT / "rulecourt/dashscope.py",
        ROOT / "rulecourt/runtime.py",
        ROOT / "rulecourt/workflow.py",
        ROOT / "rulecourt/comparison.py",
        ROOT / "rulecourt/evaluation.py",
        HERE / "run_t14_trial.py",
        Path(__file__),
    ]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    payload = {
        "schema_version": "rulecourt-five-group-development-snapshot-v1",
        "status": "frozen_development_snapshot",
        "formal_release": False,
        "review_basis": "ai_trial",
        "frozen_at_utc": datetime.now(UTC).isoformat(),
        "git_commit": commit,
        "dataset_version": dataset.dataset_version,
        "dataset_approval_digest": dataset.approval_digest(),
        "case_count": len(dataset.cases),
        "repeat_count": 1,
        "statistical_method": "descriptive_only",
        "reproduction_steps": [
            "uv run --offline --no-sync --env-file .env rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config examples/t13-hybrid-baseline-config.json --output <new-baseline-report.json>",
            "uv run --offline --no-sync --env-file .env python experiments/2026-09-23/run_t14_trial.py --tag <new-tag> --iterations 3 --tool-calls 8 --total-tokens 32768 --duration 60",
        ],
        "shared_limits": {
            "max_total_tokens": baseline_budget["max_total_tokens"],
            "max_duration_seconds": treatment_budget["max_duration_seconds"],
        },
        "artifacts_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): digest(path)
            for path in referenced.values()
        },
        "code_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): digest(path) for path in code
        },
        "groups": groups,
        "treatment_paired_case_ids": treatment.paired_case_ids,
        "treatment_invalid_audit_case_ids": treatment.excluded_case_ids.get("invalid_audit", []),
        "limitations": [
            "All 13 Cases are draft; no independent human Golden Case signoff exists.",
            "The candidate rule package is unverified and was injected only into isolated development apps.",
            "No holdout partition or two-model determinism run exists.",
            "deepseek-flash is a provider alias rather than a pinned model snapshot.",
            "Provider prices and complete billed usage are not established; costs may be N/A.",
            "This snapshot cannot be used as a T16 formal release or formal experiment freeze.",
        ],
    }
    OUTPUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("saved", OUTPUT)
    for name, group in groups.items():
        metric = group["complete"]["correct_ruling_rate"]
        print(
            name,
            f"{metric['numerator']}/{metric['denominator']}",
            "failures",
            len(group["failure_case_ids"]),
        )
    print("treatment_audit_valid", len(treatment.paired_case_ids), "/", len(dataset.cases))


if __name__ == "__main__":
    main()
