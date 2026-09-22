from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse

from rulecourt.api import create_app


class PassiveProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="passive")

    def get_default_model(self):
        return "passive-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="The state was recorded.")


def submit(client, case_id, text):
    response = client.post(f"/api/cases/{case_id}/messages", json={"text": text})
    assert response.status_code == 200
    return response.json()


def test_scope_is_rechecked_when_a_related_action_fact_changes_but_reused_for_presence(
    tmp_path,
):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]
        submit(
            client,
            case_id,
            "确认本次只裁决 Marquise 普通移动范围。Marquise 从 A 移动 1 个 warriors 到 B。",
        )

        submit(client, case_id, "Marquise 在 A 有 3 个 warriors。")
        reused = client.get(f"/api/cases/{case_id}").json()
        assert reused["scope_assumptions"][0]["status"] == "confirmed"

        submit(client, case_id, "更正：Eyrie 从 A 移动 1 个 warriors 到 B。")
        invalidated = client.get(f"/api/cases/{case_id}").json()
        assert invalidated["scope_assumptions"][0]["status"] == "invalidated"
