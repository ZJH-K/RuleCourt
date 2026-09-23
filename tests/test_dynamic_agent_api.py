import json

from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from test_eyrie_decree_api import install_verified_package

from rulecourt.api import create_app

RULE_IDS = [
    "root-2.1.1",
    "root-2.2.1",
    "root-2.2.2",
    "root-2.5",
    "root-4.2",
    "root-4.2.1",
    "root-7.2.2",
    "root-7.5.2",
    "root-7.5.2.II",
]


class DynamicProvider(LLMProvider):
    def __init__(self, case_id: str | None = None):
        super().__init__(provider_name="dynamic-test")
        self.case_id = case_id
        self.calls: list[dict] = []

    def get_default_model(self):
        return "dynamic-test-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        step = len(self.calls)
        if step == 1:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("inspect", "inspect_case", "{}")],
                finish_reason="tool_calls",
            )
        if step == 2:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "search",
                        "search_rules",
                        json.dumps(
                            {
                                "query": "move",
                                "mode": "support",
                                "faction": "marquise",
                                "action": "move",
                                "ruleset_id": "root-law-2025-10",
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        if step == 3:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "challenge",
                        "search_rules",
                        json.dumps(
                            {
                                "query": "move",
                                "mode": "challenge",
                                "faction": "marquise",
                                "action": "move",
                                "ruleset_id": "root-law-2025-10",
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        if step == 4:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "inspect-rule",
                        "inspect_rule",
                        json.dumps({"rule_ids": RULE_IDS}),
                    )
                ],
                finish_reason="tool_calls",
            )
        if step == 5:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "resolve",
                        "resolve_rule_conflicts",
                        json.dumps({"case_id": self.case_id, "rule_ids": RULE_IDS}),
                    )
                ],
                finish_reason="tool_calls",
            )
        if step == 6:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "simulate",
                        "simulate_action",
                        json.dumps({"case_id": self.case_id}),
                    )
                ],
                finish_reason="tool_calls",
            )
        if step == 7:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCallRequest(
                        "submit",
                        "submit_verdict",
                        json.dumps(
                            {
                                "case_id": self.case_id,
                                "expected_state_revision": 1,
                            }
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="The submitted verdict is authoritative.")


def test_user_can_select_dynamic_agent_and_reach_controller_verdict(tmp_path):
    provider = DynamicProvider()
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=provider,
            maintenance_token="test-secret",
            budget={
                "max_duration_seconds": 30,
                "max_iterations": 10,
                "max_tool_calls": 10,
                "max_total_tokens": 50000,
            },
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={"strategy": "dynamic_agent"}).json()["id"]
        provider.case_id = case_id

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "A 只与 B 相邻。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()

        assert result["status"] == "LEGAL"
        assert result["investigation"]["strategy"] == "dynamic_agent"
        assert result["investigation"]["strategy_version"] == "dynamic-agent-v1"
        assert result["reason"] == "MOVE_LEGAL"

        events = client.get(f"/api/cases/{case_id}/events").json()
        assert any(event["type"] == "rule_searched" for event in events)
        assert any(event["type"] == "rule_inspected" for event in events)
        assert any(event["type"] == "rule_conflicts_resolved" for event in events)
        assert any(event["type"] == "action_simulated" for event in events)
        assert any(event["type"] == "counter_evidence_checked" for event in events)
        assert any(event["type"] == "verdict_submitted" for event in events)
        tool_names = {
            event["name"]
            for event in events
            if event["type"] == "tool_call" and event["status"] == "ok"
        }
        assert {
            "inspect_case",
            "search_rules",
            "inspect_rule",
            "resolve_rule_conflicts",
            "simulate_action",
            "submit_verdict",
        } <= tool_names

        fixed_case_id = client.post("/api/cases", json={"strategy": "fixed_workflow"}).json()["id"]
        fixed_result = client.post(
            f"/api/cases/{fixed_case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "A 只与 B 相邻。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()
        assert fixed_result["status"] == "LEGAL"
        assert fixed_result["reason"] == result["reason"]
