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


def test_unqualified_conflict_requires_clarification_and_removes_old_fact_from_current_state(
    tmp_path,
):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]
        submit(client, case_id, "A 有 2 个猫兵。")

        result = submit(client, case_id, "A 有 3 个猫兵。")

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "STATE_CONFLICT"
        assert result["state_update"]["accepted"] is False
        assert result["state_update"]["missing_fields"] == [
            "clearings.A.presence.marquise.warriors"
        ]
        assert result["state_update"]["clarification_questions"][0]["field"] == (
            "clearings.A.presence.marquise.warriors"
        )
        assert any(issue["code"] == "FACT_CONFLICT" for issue in result["state_update"]["issues"])

        case = client.get(f"/api/cases/{case_id}").json()
        assert (
            case["confirmed_state"]
            .get("clearings", {})
            .get("A", {})
            .get("presence", {})
            .get("marquise", {})
            .get("warriors")
            is None
        )
        candidates = [
            fact
            for fact in case["state_facts"]
            if fact["field_path"] == "clearings.A.presence.marquise.warriors"
        ]
        assert [fact["status"] for fact in candidates] == ["conflicted", "conflicted"]
        assert {fact["evidence_refs"][0]["source_message_id"] for fact in candidates} == {
            case["messages"][0]["id"],
            case["messages"][1]["id"],
        }


def test_conflicted_field_rejects_a_later_unqualified_assertion(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]
        submit(client, case_id, "A 有 2 个猫兵。")
        submit(client, case_id, "A 有 3 个猫兵。")
        conflicted = client.get(f"/api/cases/{case_id}").json()
        field_path = "clearings.A.presence.marquise.warriors"

        response = client.post(
            f"/api/cases/{case_id}/state",
            json={
                "expected_state_revision": conflicted["revision"],
                "changes": [
                    {
                        "operation": "assert",
                        "field_path": field_path,
                        "value": 4,
                        "evidence": [
                            {
                                "field_path": field_path,
                                "source_message_id": conflicted["messages"][0]["id"],
                                "source_span": "A has the original number of warriors",
                            }
                        ],
                    }
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()
        assert result["accepted"] is False
        assert any(issue["code"] == "FACT_CONFLICT" for issue in result["issues"])
        current = client.get(f"/api/cases/{case_id}").json()
        assert current["revision"] == conflicted["revision"]
        assert [
            item["status"] for item in current["state_facts"] if item["field_path"] == field_path
        ] == ["conflicted", "conflicted"]
