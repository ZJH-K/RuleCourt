# RuleCourt M0

This M0 slice stores Cases and public investigation events. With an enabled,
human-verified Root rule package, the fixed workflow adjudicates Marquise
ordinary moves as `LEGAL`, `ILLEGAL`, or `INSUFFICIENT_INFORMATION`; unsupported
actions and missing rule evidence remain `UNRESOLVED`.

## Start

Requires [uv](https://docs.astral.sh/uv/). The nanobot dependency is pinned to
`HKUDS/nanobot@12a9a8a692c7e8fc6d3d293afdddcdaa390436df` in `pyproject.toml`
and resolved in `uv.lock`.

```powershell
uv sync --extra dev
uv run uvicorn rulecourt.api:app --reload --env-file .env
```

Open <http://127.0.0.1:8000/>. The default database is `rulecourt.sqlite3`.
Set `RULECOURT_DB` to choose another path and `OPENAI_BASE_URL` for a compatible
provider endpoint. Case URLs can be reloaded. The provider receives only the
constrained public investigation tools; its text cannot authorize a ruling.

## Checks

```powershell
uv run pytest tests/test_case_api.py -q
uv run basedpyright rulecourt
uv run ruff check rulecourt tests
uv run ruff format --check rulecourt tests
uv run pytest -q
```

## T02 rule review

Open <http://127.0.0.1:8000/rules> for the local maintenance view. It can import
versioned rule packages as `draft`, record a `verified` or `disputed` review with
a reviewer and evidence basis, and enable only a reviewed verified package.
Public search is available at `/api/rules`; private coverage obligations are
returned only from the package review view.

Set `RULECOURT_MAINTENANCE_TOKEN` in the ignored local `.env` before starting the
server. Enter that value in the review page to open maintenance operations. The
secret stays in page memory and is sent in the `X-RuleCourt-Maintenance-Token`
header. Without a configured token, maintenance APIs return 503; without the
correct token, they return 403. Public rule search remains readable. This local
review record is a T02 demo; independent source verification and formal human
signoff belong to T03.

The unverified demo package is
[`examples/root-m0-candidate-package.json`](examples/root-m0-candidate-package.json).
It points to the official [Leder Games Root rules library](https://rules.ledergames.com/?locale=en-US&printing=p1&product=root),
but its excerpts and package-local IDs still require T03 human review.

## T05 fixed ordinary-move workflow

After enabling a verified package that covers Root sections 2.2, 2.5, 4.2,
and 4.2.1, submit a complete Marquise ordinary-move description in a Case.
The result records the deterministic Decision, Verification, rule evidence,
scope, and conditions that were not checked. Eyrie moves and full-turn
legality remain outside this ticket's scope.

## T06 clarification and resumption

When current facts cannot determine the local move result, the Case returns
INSUFFICIENT_INFORMATION with missing_fields and deterministic
clarification_questions. Submit the requested facts to the same Case to
re-run the workflow. Messages, state evidence, revisions, verdict history, and
clarification_requested/clarification_resumed events remain queryable.

## T09 Dynamic Agent strategy

Choose `Automatic`, `Fixed Workflow`, or `Dynamic Agent` when creating a Case.
The API accepts the same choice as `{"strategy":"dynamic_agent"}` (the other
values are `auto` and `fixed_workflow`). Dynamic Agent may search and inspect
verified public rules, propose source-backed state updates, resolve public rule
relations, simulate the action, and submit the deterministic result. Coverage
obligations and planning hints stay private, and the Controller remains the
only component that can record a `LEGAL` or `ILLEGAL` verdict.

## T10 investigation budgets

The Controller applies one shared trial budget to dynamic and fixed
investigations. Configure it with create_app(..., budget=...) or the
RULECOURT_MAX_DURATION_SECONDS, RULECOURT_MAX_ITERATIONS,
RULECOURT_MAX_TOOL_CALLS, and RULECOURT_MAX_TOTAL_TOKENS environment
variables. Defaults are marked development_trial; they are not frozen
evaluation thresholds.

Case responses expose the cumulative usage, remaining budget, provider/model,
tool failures, latency, and stop reason. Clarification and recoverable provider
failure runs reuse the same investigation ledger, so a new submission cannot
reset the budget.

## T11 evaluation replay

`rulecourt.evaluation` keeps candidate Case labels and facts outside the
application under evaluation. `EvaluationRunner` sends only the initial input
and explicitly requested facts through an injected adapter; `score_results`
reports first-result and complete-investigation metrics separately. Draft and
disputed Cases are retained in the report but excluded from formal scoring.

Validate the candidate fixture or view an exported report with:

```powershell
uv run rulecourt-eval validate examples/m0-candidate-cases.json
uv run rulecourt-eval report evaluation-report.json --format markdown
```

The checked-in candidate fixture is intentionally `draft`; it is a trial
dataset, not an independently verified Golden Case set.

## T15 human Golden Case signoff

Formal scoring is gated by an independent human review. A verified Case must
record the reviewer, fixed rule version, review time, evidence, and all required
checks for labels, scope, fact availability, evidence, allowed questions, and
an independent label source. Corrections and ambiguity decisions stay in the
Case history; disputed Cases record their reasons and remain excluded.

The formal gate also requires coverage of partial, unknown, withdrawal, Eyrie,
Bird, and unsupported-interaction Cases. A human may record an explicit waiver
with a reason, but the waiver belongs in a detached signoff artifact rather than
the candidate dataset. That artifact binds the exact dataset digest and is
HMAC-sealed with the protected `RULECOURT_GOLDEN_SIGNOFF_KEY`.

Create a deterministic family-level split before publishing the scoring list:

~~~python
dataset.split_by_family(holdout_fraction=0.2, seed=7)
dataset.save_json("golden-cases-v1.json")
~~~

After human review, write a detached `HumanSignoff` JSON with the approved Case
IDs, reviewer IDs, coverage attestations/waivers, and signature. The manifest
commands fail closed without that artifact, its matching digest/signature, or
the family split. They export only Case/family IDs and review metadata, never
gold labels or hidden facts:

~~~powershell
$env:RULECOURT_GOLDEN_SIGNOFF_KEY = "<protected key>"
uv run rulecourt-eval validate golden-cases-v1.json --formal --signoff golden-signoff.json
uv run rulecourt-eval manifest golden-cases-v1.json --signoff golden-signoff.json
~~~

The checked-in candidate fixture remains draft; it cannot be promoted by the
replay runner or scored as formal truth.

## T12 LLM-only and Vanilla Vector RAG baselines

Both baselines use the T11 replay runner, fact responder, clarification limits,
and offline scorer. LLM-only receives the fixed rule version and user facts,
without retrieved excerpts. Vanilla Vector RAG embeds each public rule excerpt
and the accumulated user facts, then supplies cosine top-k matches to the model.
It uses no lexical search, reranker, domain engine, or private coverage table.
The model's declared four-state result is retained; citation provenance errors
and required reference-ID coverage are reported separately so an unsupported
LEGAL answer still counts as a wrong allow. Reference coverage does not establish
semantic support.

Start with the reproducible development configuration:

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config examples/t12-baseline-config.json --no-hybrid --dry-run
~~~

Copy the config to a new experiment file, replace its generation/embedding model
placeholders with fixed provider model versions, and set the embedding endpoint.
Generation uses OPENAI_BASE_URL (or the provider default); both services use
OPENAI_API_KEY from the environment. Set per-1,000-token prices in the config
if monetary estimates are wanted. Then run:

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config my-t12-config.json --no-hybrid --output t12-trial-001.json
~~~

Use --format markdown for a readable report. Existing output files are refused
to preserve prior runs. BaselineReport.from_json(path).to_markdown() renders a
saved JSON report without rerunning providers. The runner classes also expose
run_case and run_dataset for running either strategy independently; use an
asynchronous provider and call close() when finished.

Reports retain first and complete results, every retrieval round, source text,
rule IDs, corpus/index hashes, model/config/code fingerprints, and cumulative
generation plus embedding usage. Index construction is charged to the first
Case that builds it; subsequent Cases reuse that index. User waiting time is
excluded. A timeout or failed request may have unreported billed usage; missing
usage or prices produces a null/N/A total cost, with known usage reported
separately. Token budgets are enforced against reported usage, so an input-token
overrun is detected after the response and retained as a budget failure.

These are development trials: holdout Cases are rejected and no labels enter
model prompts or retrieval. The bundled rules and Cases are unverified demo
material, and synthetic tests do not confer human signoff. The two external
baselines cannot by themselves establish the value of Dynamic Agent planning.

## T13 Hybrid RAG + Reranker baseline

The default runner now adds a third external baseline. Hybrid RAG retrieves
public rule excerpts with deterministic BM25-style lexical search and the T12
vector index, merges duplicate IDs with reciprocal-rank fusion, and sends only
the merged candidates to a configured reranker. Reranker scores order evidence;
they are never treated as authoritative rule meaning, domain calculations, or
coverage-table decisions. Empty and failed retrievals are returned as normal
`UNRESOLVED` observations rather than information requests.

Use the checked-in T13 configuration as a reproducible dry run, then replace
the generation, embedding, and reranker model placeholders with fixed versions:

~~~powershell
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config examples/t13-hybrid-baseline-config.json --dry-run
uv run rulecourt-baselines examples/m0-candidate-cases.json --rules examples/root-m0-candidate-package.json --config my-t13-config.json --output t13-trial-001.json
~~~

The same `EvaluationRunner` and deterministic fact responder drive the first
and complete investigation for all three strategies. T13 reports preserve
lexical/vector candidate IDs, fusion and reranker scores, index and model
versions, every retrieval round, reranker calls/tokens, and cumulative cost.
Set `reranker_cost_per_1k_tokens` (as well as the T12 prices) when monetary
estimates are required. Use `--no-hybrid` to run the original T12 pair.

## T14 Dynamic Agent versus Fixed Workflow treatment comparison

T14 runs the Dynamic Agent and Fixed Workflow arms through the same replay
contract, fact responder, ruleset, domain controller, visible projection, and
budget configuration. The fixed route and template are versioned hand-authored
configuration; coverage obligations and diagnostic route details are never
used to generate it. All Dynamic Agent provider usage remains in its cumulative
resource usage; planning-specific counters are reported only when the provider
supplies them explicitly.

Start with the checked-in development-trial plan:

~~~powershell
uv run rulecourt-compare examples/m0-candidate-cases.json --config examples/t14-treatment-config.json --dry-run
~~~

A live comparison uses two injected CaseAdapter instances. Each adapter must
declare the complete shared contract, expose configure_budget, and provide
get_case/get_events projections with authoritative strategy/version/budget
events. Fixed-workflow events must also identify the hand-authored template,
route source, and disabled LLM routing; missing audit observations invalidate
the pair. FastAPICaseAdapter supports this contract for local FastAPI TestClient
instances:

~~~python
import json

from rulecourt.comparison import TreatmentComparisonRunner, TreatmentConfig
from rulecourt.evaluation import FastAPICaseAdapter

config = TreatmentConfig.model_validate(json.load(open("my-t14-config.json")))
shared = config.shared_contract
runner = TreatmentComparisonRunner({
    "dynamic_agent": FastAPICaseAdapter(
        dynamic_client,
        strategy="dynamic_agent",
        metadata={
            **shared,
            "strategy_version": config.dynamic_strategy_version,
        },
    ),
    "fixed_workflow": FastAPICaseAdapter(
        fixed_client,
        strategy="fixed_workflow",
        metadata={
            **shared,
            "strategy_version": config.fixed_workflow_version,
            "fixed_route_source": config.fixed_route_source,
            "fixed_template_version": config.fixed_template_version,
            "fixed_workflow_llm_routing": False,
        },
    ),
}, config=config)
report = runner.run_dataset(
    dataset,
    determinism_adapters={
        "provider-a/model-a": {...},
        "provider-b/model-b": {...},
    },
)
report.save_json("t14-trial-001.json")
~~~

The runner applies the same InvestigationBudget to both arms. The report
pairs initial and complete results, records per-case resource deltas, reports
correct ruling rate, wrong-allow rate and coverage for each arm, and retains
the public context/tool/log projection used for the leakage audit. Explicit
planning usage is reported only when the provider supplies planning-specific
counters; cumulative Dynamic Agent usage and cost always remain included.
Invalid audit pairs are retained but excluded from the main comparison.
Development runs reject holdout Cases. Formal replay additionally requires a
frozen budget, split/coverage validation, detached human signoff, and at least
two model/provider adapter sets, complete resource dimensions, and usage
provenance for both arms. For exported formal replay, pass --signoff together
with --determinism-manifest; the manifest maps each provider or model identity
to its dynamic and fixed StrategyRunReport artifacts and their per-case public
observations. Formal StrategyRunReport artifacts include an HMAC over the exact
config, results, and observations; replay requires the protected
RULECOURT_GOLDEN_SIGNOFF_KEY to validate that signature.
