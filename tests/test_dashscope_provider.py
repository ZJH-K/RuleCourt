"""The Qwen adapters must send the right native request and keep keys separate."""

import asyncio
import json

import httpx

from rulecourt.dashscope import dashscope_origin
from rulecourt.embeddings import HTTPEmbeddingProvider
from rulecourt.hybrid_baselines import DashScopeRerankerProvider


def test_dashscope_origin_accepts_console_host():
    assert (
        dashscope_origin("https://llm-test.cn-beijing.maas.aliyuncs.com")
        == "https://llm-test.cn-beijing.maas.aliyuncs.com"
    )


def test_qwen_embedding_uses_dashscope_key(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [{"index": 0, "embedding": [0.2, 0.8]}],
                "usage": {"prompt_tokens": 3},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("rulecourt.embeddings.httpx.AsyncClient", lambda: client)
    provider = HTTPEmbeddingProvider(
        model="qwen3.7-text-embedding-flash",
        base_url="https://llm-test.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        api_key_env="DASHSCOPE_API_KEY",
    )
    batch = asyncio.run(provider.embed(["rule"])).model_dump()
    assert batch == {"vectors": [[0.2, 0.8]], "input_tokens": 3}
    assert seen[0].url.path == "/compatible-mode/v1/embeddings"
    assert seen[0].headers["authorization"] == "Bearer qwen-test-key"


def test_qwen_reranker_uses_native_payload_and_aligns_scores(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "qwen-test-key")
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "output": {
                    "results": [
                        {"index": 1, "relevance_score": 0.9},
                        {"index": 0, "relevance_score": 0.1},
                    ]
                },
                "usage": {"prompt_tokens": 5, "total_tokens": 5},
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr("rulecourt.hybrid_baselines.httpx.AsyncClient", lambda: client)
    provider = DashScopeRerankerProvider(
        model="qwen3.7-text-rerank",
        api_host="llm-test.cn-beijing.maas.aliyuncs.com",
    )
    batch = asyncio.run(provider.rerank("move", ["rule A", "rule B"]))
    assert batch.scores == [0.1, 0.9]
    assert batch.total_tokens == 5
    assert seen[0].url.path == "/api/v1/services/rerank/text-rerank/text-rerank"
    assert seen[0].headers["authorization"] == "Bearer qwen-test-key"
    assert json.loads(seen[0].content) == {
        "model": "qwen3.7-text-rerank",
        "input": {"query": "move", "documents": ["rule A", "rule B"]},
        "parameters": {"top_n": 2},
    }
