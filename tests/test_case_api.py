from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest

from rulecourt.api import create_app


class ControlledProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="controlled")
        self.calls = []

    def get_default_model(self):
        return "controlled-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        self.calls.append({"messages": messages, "tools": tools})
        if len(self.calls) % 2:
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest("read-1", "inspect_case", "{}")],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="This move is legal.")


def test_case_round_trip_persists_after_app_reload(tmp_path):
    db = tmp_path / "cases.sqlite3"
    provider = ControlledProvider()
    with TestClient(create_app(db, provider=provider)) as client:
        assert "Create Case" in client.get("/").text
        created = client.post("/api/cases", json={}).json()
        case_id = created["id"]
        response = client.post(
            f"/api/cases/{case_id}/messages", json={"text": "Can my warrior move?"}
        )
        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "DOMAIN_NOT_IMPLEMENTED"
        assert result["revision"] == 0
        assert result["evidence"] == []

    with TestClient(create_app(db, provider=ControlledProvider())) as client:
        state = client.get(f"/api/cases/{case_id}").json()
        assert state["confirmed_state"] == {}
        assert state["messages"][0]["text"] == "Can my warrior move?"
        assert state["verdicts"][0]["status"] == "UNRESOLVED"
        events = client.get(f"/api/cases/{case_id}/events").json()
        assert any(e["type"] == "tool_call" and e["name"] == "inspect_case" for e in events)
        assert any(e["type"] == "verdict" for e in events)
        assert all(e.get("name") not in {"exec", "write_file", "web_search"} for e in events)
    assert [t["function"]["name"] for t in provider.calls[0]["tools"]] == ["inspect_case"]


def test_public_api_rejects_control_fields_and_invalid_messages(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=ControlledProvider())
    ) as client:
        assert client.post("/api/cases", json={"revision": 5}).status_code == 422
        case_id = client.post("/api/cases", json={}).json()["id"]
        assert client.post(f"/api/cases/{case_id}/messages", json={"text": "  "}).status_code == 422
        assert (
            client.post(
                f"/api/cases/{case_id}/messages",
                json={"text": "move", "verdict": "LEGAL", "revision": 999},
            ).status_code
            == 422
        )
        assert client.get("/api/cases/not-a-case").status_code == 404
