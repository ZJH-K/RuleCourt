"""Embedding service boundary used by the vanilla vector baseline."""

from __future__ import annotations

import os
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field


class EmbeddingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    vectors: list[list[float]]
    input_tokens: int | None = Field(default=None, ge=0)


class EmbeddingProvider(Protocol):
    model: str

    async def embed(self, texts: list[str]) -> EmbeddingBatch: ...


class HTTPEmbeddingProvider:
    """Use an explicitly configured OpenAI-compatible embeddings endpoint."""

    def __init__(self, *, model: str, base_url: str, api_key_env: str = "OPENAI_API_KEY"):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env

    async def embed(self, texts: list[str]) -> EmbeddingBatch:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                self.base_url + "/embeddings",
                headers={"Authorization": f"Bearer {os.environ[self.api_key_env]}"},
                json={"model": self.model, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()
        rows = sorted(payload["data"], key=lambda row: row["index"])
        if [row["index"] for row in rows] != list(range(len(texts))):
            raise ValueError("embedding response indices do not match inputs")
        return EmbeddingBatch(
            vectors=[row["embedding"] for row in rows],
            input_tokens=payload.get("usage", {}).get("prompt_tokens"),
        )
