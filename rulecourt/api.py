"""Public Case API and minimal browser view."""

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime import RuleCourtController
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


def create_app(db_path: str | Path, provider=None, model: str | None = None) -> FastAPI:
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
    controller = RuleCourtController(store, provider, model)
    app = FastAPI(title="RuleCourt")

    def require_case(case_id: str) -> dict[str, Any]:
        case = store.get(case_id)
        if case is None:
            raise HTTPException(404, "Case not found")
        return case

    @app.get("/")
    def index():
        return FileResponse(Path(__file__).with_name("index.html"))

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

    return app


app = create_app(os.getenv("RULECOURT_DB", "rulecourt.sqlite3"))
