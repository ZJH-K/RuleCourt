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


def new_case(client):
    return client.post("/api/cases", json={}).json()["id"]


def test_retraction_keeps_a_source_backed_retraction_record(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "A 有 2 个老鹰兵。")
        submit(client, case_id, "撤回 A 的老鹰兵数量。")

        case = client.get(f"/api/cases/{case_id}").json()
        retraction = next(fact for fact in case["state_facts"] if fact["operation"] == "retract")
        assert retraction["status"] == "retracted"
        assert retraction["value"] is None
        assert retraction["supersedes_id"] is not None
        assert retraction["evidence_refs"][0]["source_message_id"] == case["messages"][1]["id"]
