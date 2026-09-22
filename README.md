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
provider endpoint. Case URLs can be reloaded. The provider is asked to use the
only registered tool, `inspect_case`; its text cannot authorize a ruling.

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

