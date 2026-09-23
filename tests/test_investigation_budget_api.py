import asyncio

from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse, LLMUsage, ToolCallRequest

from rulecourt.api import create_app


class RepeatingToolProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="controlled")
        self.calls = 0

    def get_default_model(self):
        return "controlled-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(f"read-{self.calls}", "inspect_case", "{}")],
            finish_reason="tool_calls",
            usage=LLMUsage.reported(input_tokens=2, output_tokens=3),
        )


class InterruptingProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="interrupted")
        self.calls = 0

    def get_default_model(self):
        return "interrupted-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        raise RuntimeError("provider connection interrupted")

    async def chat_stream_with_retry(self, **kwargs):
        self.calls += 1
        raise RuntimeError("provider connection interrupted")


class SlowProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="slow")

    def get_default_model(self):
        return "slow-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        await asyncio.sleep(0.05)
        return LLMResponse(content="not a ruling")


class ToolFailureProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="tool-failure")
        self.calls = 0

    def get_default_model(self):
        return "tool-failure-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls += 1
        if self.calls > 1:
            return LLMResponse(content="not a ruling")
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest("bad-1", "inspect_case", '{"unexpected": 1}')],
            finish_reason="tool_calls",
        )


def submit(client, case_id, text="Investigate this Case"):
    response = client.post(
        f"/api/cases/{case_id}/messages",
        json={"text": text},
    )
    assert response.status_code == 200
    return response.json()


def new_case(client):
    return client.post("/api/cases", json={}).json()["id"]


def test_repeating_tools_stop_at_controller_budget_and_record_usage(tmp_path):
    provider = RepeatingToolProvider()
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=provider,
            budget={
                "max_duration_seconds": 10,
                "max_iterations": 10,
                "max_tool_calls": 1,
                "max_total_tokens": 100000,
            },
        )
    ) as client:
        budget_response = client.get("/api/investigation-budget")
        assert budget_response.status_code == 200
        assert budget_response.json()["max_tool_calls"] == 1
        case_id = new_case(client)
        result = submit(client, case_id)

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "BUDGET_EXHAUSTED"
        assert result["failure_reason"] == "BUDGET_EXHAUSTED"
        assert result["budget_dimension"] == "tool_calls"
        assert provider.calls == 1
        investigation = result["investigation"]
        assert investigation["budget"]["configuration_status"] == "development_trial"
        assert investigation["usage"]["tool_calls"] == 1
        assert investigation["usage"]["total_tokens"] == 5
        assert investigation["remaining"]["tool_calls"] == 0
        assert investigation["stop_reason"] == "budget_exhausted"

        state = client.get(f"/api/cases/{case_id}").json()
        assert len(state["investigation_runs"]) == 1
        assert state["investigation_runs"][0]["provider"] == "controlled"


def test_provider_failure_is_not_reported_as_missing_user_information(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=InterruptingProvider())
    ) as client:
        case_id = new_case(client)
        result = submit(client, case_id)

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "INVESTIGATION_FAILED"
        assert result["failure_reason"] == "PROVIDER_ERROR"
        assert result["stop_reason"] == "provider_error"
        assert result["status"] != "INSUFFICIENT_INFORMATION"
        events = client.get(f"/api/cases/{case_id}/events").json()
        assert any(event["type"] == "provider_failed" for event in events)


def test_provider_timeout_is_a_separate_unresolved_reason(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=SlowProvider(),
            budget={
                "max_duration_seconds": 0.001,
                "max_iterations": 3,
                "max_tool_calls": 3,
                "max_total_tokens": 1000,
            },
        )
    ) as client:
        case_id = new_case(client)
        result = submit(client, case_id)

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "INVESTIGATION_TIMEOUT"
        assert result["failure_reason"] == "TIME_LIMIT_EXCEEDED"
        assert result["stop_reason"] == "time_limit"


def test_recovery_run_uses_remaining_budget_instead_of_resetting(tmp_path):
    provider = RepeatingToolProvider()

    async def first_then_final(messages, tools=None, model=None, **kwargs):
        provider.calls += 1
        if provider.calls == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("read-1", "inspect_case", "{}")],
                finish_reason="tool_calls",
                usage=LLMUsage.reported(input_tokens=2, output_tokens=3),
            )
        return LLMResponse(
            content="advisory response", usage=LLMUsage.reported(input_tokens=2, output_tokens=3)
        )

    provider.chat = first_then_final
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=provider,
            budget={
                "max_duration_seconds": 10,
                "max_iterations": 2,
                "max_tool_calls": 2,
                "max_total_tokens": 100000,
            },
        )
    ) as client:
        case_id = new_case(client)
        first = submit(client, case_id, "first investigation")
        second = submit(client, case_id, "clarifying information")

        assert first["investigation"]["run_count"] == 1
        assert second["investigation"]["run_count"] == 2
        assert second["investigation"]["usage"]["iterations"] == 2
        assert second["investigation"]["usage"]["total_tokens"] == 10
        assert second["investigation"]["remaining"]["iterations"] == 0
        assert provider.calls == 2


def test_tool_failure_is_recorded_separately_from_user_information(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=ToolFailureProvider())
    ) as client:
        case_id = new_case(client)
        result = submit(client, case_id)

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "INVESTIGATION_INCOMPLETE"
        assert result["failure_reason"] == "TOOL_FAILED"
        assert result["stop_reason"] == "tool_failure"


def test_repeated_public_submission_does_not_reset_exhausted_budget(tmp_path):
    provider = RepeatingToolProvider()
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=provider,
            budget={
                "max_duration_seconds": 10,
                "max_iterations": 1,
                "max_tool_calls": 5,
                "max_total_tokens": 100000,
            },
        )
    ) as client:
        case_id = new_case(client)
        first = submit(client, case_id, "repeatable failure")
        second = submit(client, case_id, "repeatable failure")

        assert first["reason"] == "BUDGET_EXHAUSTED"
        assert first["budget_dimension"] == "iterations"
        assert second["reason"] == "BUDGET_EXHAUSTED"
        assert second["investigation"]["id"] == first["investigation"]["id"]


def test_no_evidence_is_not_reported_as_user_information(tmp_path):
    with TestClient(create_app(tmp_path / "cases.sqlite3", provider=SlowProvider())) as client:
        case_id = new_case(client)
        result = submit(client, case_id)

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "INVESTIGATION_INCOMPLETE"
        assert result["failure_reason"] == "EVIDENCE_UNAVAILABLE"
        assert result["stop_reason"] == "evidence_unavailable"


def test_repeated_provider_failures_consume_budget(tmp_path):
    provider = InterruptingProvider()
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=provider,
            budget={
                "max_duration_seconds": 10,
                "max_iterations": 2,
                "max_tool_calls": 2,
                "max_total_tokens": 1000,
            },
        )
    ) as client:
        case_id = new_case(client)
        first = submit(client, case_id)
        second = submit(client, case_id)

        assert first["failure_reason"] == "PROVIDER_ERROR"
        assert second["failure_reason"] == "PROVIDER_ERROR"
        assert provider.calls == 2


def test_fixed_failed_investigation_uses_shared_iteration_budget(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=SlowProvider(),
            budget={
                "max_duration_seconds": 10,
                "max_iterations": 1,
                "max_tool_calls": 2,
                "max_total_tokens": 1000,
            },
        )
    ) as client:
        case_id = new_case(client)
        text = (
            "确认本次只裁决 Marquise 普通移动范围。"
            "Marquise 在 A 有 3 个 warriors。Marquise 在 A 有 0 个 buildings。"
            "Eyrie 在 A 有 0 个 warriors。Eyrie 在 A 有 0 个 buildings。"
            "Marquise 在 B 有 0 个 warriors。Marquise 在 B 有 0 个 buildings。"
            "Eyrie 在 B 有 0 个 warriors。Eyrie 在 B 有 0 个 buildings。"
            "A 只与 B 相邻。Marquise 从 A 移动 1 个 warriors 到 B。"
        )
        first = submit(client, case_id, text)
        second = submit(client, case_id, text)

        assert first["investigation"]["strategy"] == "fixed_workflow"
        assert first["investigation"]["usage"]["iterations"] == 1
        assert second["reason"] == "BUDGET_EXHAUSTED"
        assert second["budget_dimension"] == "iterations"
