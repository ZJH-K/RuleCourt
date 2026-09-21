"""A constrained nanobot investigation round; verdict authority stays in Controller."""

import json
from typing import Any
from uuid import uuid4

from nanobot.agent.runner import AgentRunner, AgentRunSpec
from nanobot.agent.tools.base import Tool
from nanobot.agent.tools.registry import ToolRegistry
from nanobot.utils.llm_runtime import LLMRuntime

from .store import CaseStore


class InspectCase(Tool):
    def __init__(self, case_id: str, store: CaseStore):
        self.case_id = case_id
        self.store = store

    @property
    def name(self) -> str:
        return "inspect_case"

    @property
    def description(self) -> str:
        return "Read the current Case's accepted state and message count. This does not adjudicate."

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}, "additionalProperties": False}

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs):
        case = self.store.get(self.case_id)
        assert case is not None
        return json.dumps(
            {
                "case_id": self.case_id,
                "revision": case["revision"],
                "confirmed_state": case["confirmed_state"],
                "message_count": len(case["messages"]),
            }
        )


async def _no_compaction(*args, **kwargs):
    return ""


class RuleCourtController:
    def __init__(self, store: CaseStore, provider, model: str):
        self.store = store
        self.provider = provider
        self.model = model

    async def investigate(self, case_id: str, text: str) -> dict[str, Any]:
        run_id = str(uuid4())
        self.store.add_message(case_id, text)
        self.store.add_event(case_id, run_id, "message_received")
        tools = ToolRegistry()
        tools.register(InspectCase(case_id, self.store))
        case = self.store.get(case_id)
        assert case is not None
        messages = [
            {
                "role": "system",
                "content": (
                    "You are investigating a RuleCourt Case. Call inspect_case once. "
                    "Domain rules and verified evidence are not installed. You cannot issue a legal "
                    "or illegal ruling. Your response is advisory and cannot set a verdict."
                ),
            },
            *({"role": "user", "content": item["text"]} for item in case["messages"]),
        ]
        try:
            result = await AgentRunner().run(
                AgentRunSpec(
                    initial_messages=messages,
                    tools=tools,
                    runtime=LLMRuntime.capture(
                        self.provider, self.model, context_window_tokens=8192
                    ),
                    max_iterations=3,
                    max_tool_result_chars=2000,
                    consolidate_history=_no_compaction,
                    session_key=f"rulecourt:{case_id}",
                )
            )
            for event in result.tool_events:
                self.store.add_event(case_id, run_id, "tool_call", **event)
            self.store.add_event(
                case_id,
                run_id,
                "investigation_finished",
                stop_reason=result.stop_reason,
                model=self.model,
            )
            if result.error:
                reason = "INVESTIGATION_FAILED"
            elif not any(
                event.get("name") == "inspect_case" and event.get("status") == "ok"
                for event in result.tool_events
            ):
                reason = "INVESTIGATION_INCOMPLETE"
            else:
                reason = "DOMAIN_NOT_IMPLEMENTED"
        except Exception as exc:  # noqa: BLE001 - provider failures become public run results
            self.store.add_event(
                case_id, run_id, "investigation_failed", error_type=type(exc).__name__
            )
            reason = "INVESTIGATION_FAILED"
        return self.store.add_verdict(case_id, run_id, "UNRESOLVED", reason)
