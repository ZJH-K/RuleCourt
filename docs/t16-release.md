# T16 formal M0 release

`rulecourt.release` is the final evidence gate over the T11--T15 artifacts. It does not change the Case product or add a second adjudication path.

## Freeze from development

Create a family split and complete the development trial first. Freeze the budget, clarification and fact-request limits, repeat count, statistical method, minimum quality gain, wrong-allow constraint, model/strategy versions, rule material, private coverage-table version, and cache condition with `FrozenExperimentConfig.freeze_from_development(...)`. The resulting config is `run_kind = formal`, has a frozen `InvestigationBudget`, and is bound to the exact dataset digest. A holdout-only or incomplete development partition is rejected.

`examples/t16-freeze-config.json` is deliberately incomplete; it is not a formal configuration.

## Formal replay and report

Run formal T12/T13 baseline and T14 treatment reports with the same detached `HumanSignoff` and frozen configuration. T12/T13 formal runners accept holdout Cases only with `configuration_status = frozen`; their formal default selects the holdout partition. T14 exposes the same `holdout_only` option and keeps its leakage and determinism gates.

Build and sign the release from those reports:

```python
from rulecourt.release import ReleaseBuilder

release = ReleaseBuilder(frozen_config).build(
    dataset,
    baseline_reports=[t13_report],
    treatment_reports=[t14_report],
    signoff=human_signoff,
    signing_key=release_key,
)
release.save_json("t16-release.json")
```

The report contains all five groups (`llm_only`, `vanilla_rag`, `hybrid_rag`, `dynamic_agent`, `fixed_workflow`), initial and complete metrics, category metrics, paired Dynamic/Fixed results, resources, retained artifact digests, family and repeat counts, reproduction steps, limitations, and the human-review reference. Repeated runs use unique run IDs but a unique-Case denominator; canonical families are reported separately and are never treated as independent samples.

Use the CLI without rerunning providers:

```powershell
$env:RULECOURT_T16_RELEASE_KEY = "<protected release key>"
uv run rulecourt-release report t16-release.json --format markdown
```

The conclusion is computed from the frozen policy and is one of `quality_gain`, `efficiency_gain`, `insufficient_evidence`, or `unproven_gain`. Changing a threshold after replay invalidates the config hash and signature. A completed experiment remains deliverable when it does not prove an Agent increment.

## Public regression and limits

The existing public FastAPI entry points and browser view remain the regression seam: Case creation, state, events/investigation, and current/historical verdicts are exercised by the public API tests. T16 adds no game capability or visual redesign. The example rules and candidate Cases remain unverified; rule review, Golden Case signoff, provider billing completeness, and observed model limitations remain explicit prerequisites or limitations.
