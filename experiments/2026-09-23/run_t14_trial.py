"""Reproduce the two live treatment arms on the AI-attested draft dataset.

This runner provides the candidate rule package only inside isolated local apps.
It never records a human review or enables a package in the production RuleStore.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from rulecourt.api import create_app
from rulecourt.baselines import _cli_provider
from rulecourt.comparison import TreatmentComparisonRunner, TreatmentConfig
from rulecourt.evaluation import CaseDataset, FastAPICaseAdapter, validate_ai_trial_attestation
from rulecourt.rules import RulePackageInput

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


class CandidateTrialRuleStore:
    """Expose the exact candidate package in an isolated development replay."""

    def __init__(self, raw: dict[str, Any]):
        package = RulePackageInput.model_validate(raw).model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(package, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.package = {**package, "id": f"candidate-{digest[:16]}", "status": "draft"}
        self.rules = {rule["id"]: rule for rule in package["rules"]}
        self.source = {
            key: self.package[key]
            for key in (
                "id",
                "game_id",
                "title",
                "source_type",
                "revision",
                "published_at",
                "authority",
                "locator",
                "scope_strategy",
                "checksum",
            )
        }

    def get_enabled_package(self, game_id: str, revision: str) -> dict[str, Any] | None:
        # Only this development adapter supplies the draft as a provisional source.
        if game_id == self.package["game_id"] and revision == self.package["revision"]:
            return self.package
        return None

    def search_public(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        needle = query.casefold().strip()
        rows = []
        for rule in sorted(self.rules.values(), key=lambda row: (row["section"], row["id"])):
            haystack = " ".join(
                str(value)
                for value in (
                    rule["id"],
                    rule["section"],
                    rule.get("title") or "",
                    rule["text"],
                    *rule.get("keywords", []),
                )
            ).casefold()
            if not needle or needle in haystack:
                rows.append({**rule, "source": self.source})
        return rows[:limit]

    def get_public_rule(self, rule_id: str, package_id: str | None = None) -> dict[str, Any] | None:
        if package_id is not None and package_id != self.package["id"]:
            return None
        rule = self.rules.get(rule_id)
        if rule is None:
            return None
        relations = [
            relation
            for relation in self.package["relations"]
            if rule_id in (relation["source_rule_id"], relation["target_rule_id"])
        ]
        return {**rule, "source": self.source, "relations": relations}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int)
    parser.add_argument("--tag")
    parser.add_argument("--total-tokens", type=int, default=32768)
    parser.add_argument("--duration", type=float, default=60.0)
    parser.add_argument("--iterations", type=int, default=3)
    parser.add_argument("--tool-calls", type=int, default=8)
    args = parser.parse_args()
    dataset = CaseDataset.from_json(ROOT / "examples/m0-candidate-cases.json")
    raw_package = json.loads(
        (ROOT / "examples/root-m0-candidate-package.json").read_text(encoding="utf-8")
    )
    attestation = json.loads(
        (ROOT / "docs/research/root-m0-ai-attestation.json").read_text(encoding="utf-8")
    )
    validate_ai_trial_attestation(dataset, raw_package, attestation)
    trial_rules = CandidateTrialRuleStore(raw_package)
    sample_provider = _cli_provider(os.environ["RULECOURT_MODEL"])
    provider_name = str(getattr(sample_provider, "provider_name", type(sample_provider).__name__))
    config_values = json.loads(
        (ROOT / "examples/t14-treatment-config.json").read_text(encoding="utf-8")
    )
    config_values["model"] = os.environ["RULECOURT_MODEL"]
    config_values["provider"] = provider_name
    config_values["budget"].update(
        max_duration_seconds=args.duration,
        max_iterations=args.iterations,
        max_tool_calls=args.tool_calls,
        max_total_tokens=args.total_tokens,
    )
    config_values["cache_condition"] = "no-cache"
    config = TreatmentConfig.model_validate(config_values)
    tag = args.tag or ("smoke" if args.limit else "development")
    config_path = HERE / f"t14-{tag}-config.json"
    config_path.write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    adapters = {}
    clients = []
    for strategy in ("dynamic_agent", "fixed_workflow"):
        db_path = HERE / f"t14-{tag}-{strategy}.sqlite3"
        if db_path.exists():
            raise FileExistsError(f"refusing to reuse case database: {db_path}")
        app = create_app(db_path, provider=_cli_provider(config.model), model=config.model)
        app.state.rulecourt_controller.workflow.rule_store = trial_rules
        client = TestClient(app)
        clients.append(client)
        metadata = {
            **config.shared_contract,
            "strategy_version": (
                config.dynamic_strategy_version
                if strategy == "dynamic_agent"
                else config.fixed_workflow_version
            ),
        }
        if strategy == "fixed_workflow":
            metadata.update(
                fixed_route_source=config.fixed_route_source,
                fixed_template_version=config.fixed_template_version,
                fixed_workflow_llm_routing=False,
            )
        adapters[strategy] = FastAPICaseAdapter(client, strategy=strategy, metadata=metadata)
    runner = TreatmentComparisonRunner(adapters, config=config)
    cases = dataset.cases[: args.limit] if args.limit else dataset.cases
    pairs = []
    for index, case in enumerate(cases, 1):
        pair = runner.run_case(case)
        pairs.append(pair)
        print(
            f"paired {index}/{len(cases)} {case.id}: audit={pair.valid_for_comparison}", flush=True
        )
    score_dataset = CaseDataset(dataset_version=dataset.dataset_version, cases=cases)
    report = runner.build_report(score_dataset, cases, pairs)
    output_path = HERE / f"t14-{tag}-results.json"
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite result: {output_path}")
    report.save_json(output_path)
    print(f"saved {output_path}", flush=True)
    for client in clients:
        client.close()


if __name__ == "__main__":
    main()
