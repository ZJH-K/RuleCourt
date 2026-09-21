"""Public Case API and minimal browser view."""

import os
import secrets
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .rules import (
    AmbiguousPublicRuleError,
    DuplicateRulePackageError,
    RulePackageInput,
    RulePackageValidationError,
    RuleReviewInput,
    RuleStore,
)
from .runtime import RuleCourtController
from .state_store import StateStore
from .store import CaseStore


class CreateCase(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(max_length=10000)

    @field_validator("text")
    @classmethod
    def nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message must not be empty")
        return value


def create_app(
    db_path: str | Path,
    provider=None,
    model: str | None = None,
    maintenance_token: str | None = None,
) -> FastAPI:
    if provider is None:
        from nanobot.providers.openai_compat_provider import OpenAICompatProvider

        model = model or os.getenv("RULECOURT_MODEL", "gpt-4o-mini")
        provider = OpenAICompatProvider(
            api_key=os.getenv("OPENAI_API_KEY"),
            api_base=os.getenv("OPENAI_BASE_URL"),
            default_model=model,
        )
    else:
        model = model or provider.get_default_model()

    store = CaseStore(Path(db_path))
    rule_store = RuleStore(Path(db_path))
    state_store = StateStore(Path(db_path))
    controller = RuleCourtController(store, provider, model, state_store)
    app = FastAPI(title="RuleCourt")
    maintenance_token = maintenance_token or os.getenv("RULECOURT_MAINTENANCE_TOKEN")

    def require_maintainer(
        x_rulecourt_maintenance_token: str | None = Header(default=None),
    ) -> None:
        if not maintenance_token:
            raise HTTPException(503, "Rule package maintenance is not configured")
        if not x_rulecourt_maintenance_token or not secrets.compare_digest(
            x_rulecourt_maintenance_token, maintenance_token
        ):
            raise HTTPException(403, "Maintainer token required")

    def require_case(case_id: str) -> dict[str, Any]:
        case = store.get(case_id)
        if case is None:
            raise HTTPException(404, "Case not found")
        state_view = state_store.view(case_id)
        assert state_view is not None
        return {**case, **state_view}

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).with_name("index.html"))

    @app.get("/rules")
    def rule_review_page():
        return FileResponse(Path(__file__).with_name("rules.html"))

    @app.post("/api/cases", status_code=201)
    def create_case(_request: CreateCase):
        return store.create()

    @app.get("/api/cases/{case_id}")
    def get_case(case_id: str):
        return require_case(case_id)

    @app.post("/api/cases/{case_id}/messages")
    async def submit_message(case_id: str, request: UserMessage):
        require_case(case_id)
        return await controller.investigate(case_id, request.text)

    @app.get("/api/cases/{case_id}/events")
    def get_events(case_id: str):
        require_case(case_id)
        return store.events(case_id)

    @app.post("/api/rule-packages", status_code=201, dependencies=[Depends(require_maintainer)])
    def import_rule_package(request: RulePackageInput):
        try:
            return rule_store.import_package(request)
        except DuplicateRulePackageError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RulePackageValidationError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/rule-packages", dependencies=[Depends(require_maintainer)])
    def list_rule_packages():
        return rule_store.list_packages()

    @app.get("/api/rule-packages/{package_id}", dependencies=[Depends(require_maintainer)])
    def get_rule_package(package_id: str):
        package = rule_store.get_package(package_id)
        if package is None:
            raise HTTPException(404, "Rule package not found")
        return package

    @app.post("/api/rule-packages/{package_id}/reviews", dependencies=[Depends(require_maintainer)])
    def review_rule_package(package_id: str, request: RuleReviewInput):
        try:
            return rule_store.review(package_id, request)
        except KeyError as exc:
            raise HTTPException(404, "Rule package not found") from exc

    @app.post("/api/rule-packages/{package_id}/enable", dependencies=[Depends(require_maintainer)])
    def enable_rule_package(package_id: str):
        try:
            return rule_store.enable(package_id)
        except KeyError as exc:
            raise HTTPException(404, "Rule package not found") from exc
        except RulePackageValidationError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/rules")
    def search_rules(q: str = "", limit: int = 50):
        if limit < 1 or limit > 100:
            raise HTTPException(422, "limit must be between 1 and 100")
        return rule_store.search_public(q, limit)

    @app.get("/api/rules/{rule_id}")
    def inspect_rule(rule_id: str, package_id: str | None = None):
        try:
            rule = rule_store.get_public_rule(rule_id, package_id)
        except AmbiguousPublicRuleError as exc:
            raise HTTPException(409, str(exc)) from exc
        if rule is None:
            raise HTTPException(404, "Rule not found")
        return rule

    return app


app = create_app(os.getenv("RULECOURT_DB", "rulecourt.sqlite3"))
