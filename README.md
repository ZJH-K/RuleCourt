# RuleCourt T01

This first slice stores Cases and public investigation events. Domain rules and
verified evidence are not installed, so every run ends in `UNRESOLVED`.

## Start

Requires [uv](https://docs.astral.sh/uv/). The nanobot dependency is pinned to
`HKUDS/nanobot@12a9a8a692c7e8fc6d3d293afdddcdaa390436df` in `pyproject.toml`
and resolved in `uv.lock`.

```powershell
uv sync --extra dev
$env:OPENAI_API_KEY = "your-key"
$env:RULECOURT_MODEL = "gpt-4o-mini"
uv run uvicorn rulecourt.api:app --reload
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

