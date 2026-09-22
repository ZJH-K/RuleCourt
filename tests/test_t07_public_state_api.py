from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse

from rulecourt.api import create_app


class PassiveProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="passive")

    def get_default_model(self):
        return "passive-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="No authoritative ruling.")


def submit(client: TestClient, case_id: str, text: str) -> dict:
    response = client.post(
        f"/api/cases/{case_id}/messages",
        json={"text": text},
    )
    assert response.status_code == 200
    return response.json()


def patch_state(client: TestClient, case_id: str, revision: int, change: dict) -> dict:
    response = client.post(
        f"/api/cases/{case_id}/state",
        json={
            "expected_state_revision": revision,
            "changes": [change],
        },
    )
    assert response.status_code == 200
    return response.json()


def test_public_state_endpoint_rejects_cross_case_and_stale_objects(tmp_path):
    app = create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())
    with TestClient(app) as client:
        first_case = client.post("/api/cases", json={}).json()["id"]
        second_case = client.post("/api/cases", json={}).json()["id"]
        submit(client, first_case, "A 有 2 个猫兵。")
        submit(client, second_case, "B 有 1 个猫兵。")
        first_view = client.get(f"/api/cases/{first_case}").json()
        second_view = client.get(f"/api/cases/{second_case}").json()
        first_fact = first_view["state_facts"][0]
        submit(client, second_case, "更正：B 有 1 个猫兵。")
        second_view = client.get(f"/api/cases/{second_case}").json()
        second_message = second_view["messages"][-1]["id"]

        cross_case = patch_state(
            client,
            second_case,
            second_view["revision"],
            {
                "operation": "correct",
                "field_path": first_fact["field_path"],
                "value": 4,
                "supersedes_id": first_fact["id"],
                "evidence": [
                    {
                        "field_path": first_fact["field_path"],
                        "source_message_id": second_message,
                        "source_span": "B has the stated number of warriors",
                    }
                ],
            },
        )
        assert {issue["code"] for issue in cross_case["issues"]} == {"CROSS_CASE_REFERENCE"}
        assert client.get(f"/api/cases/{second_case}").json()["revision"] == second_view["revision"]

        submit(client, first_case, "更正：A 有 3 个猫兵。")
        current_first = client.get(f"/api/cases/{first_case}").json()
        current_fact = next(
            item for item in current_first["state_facts"] if item["status"] == "active"
        )
        correction_message = current_first["messages"][-1]["id"]
        corrected = patch_state(
            client,
            first_case,
            current_first["revision"],
            {
                "operation": "correct",
                "field_path": current_fact["field_path"],
                "value": 4,
                "supersedes_id": current_fact["id"],
                "evidence": [
                    {
                        "field_path": current_fact["field_path"],
                        "source_message_id": correction_message,
                        "source_span": "A has the corrected number of warriors",
                    }
                ],
            },
        )
        assert corrected["accepted"] is True

        stale = patch_state(
            client,
            first_case,
            corrected["state_revision"],
            {
                "operation": "correct",
                "field_path": current_fact["field_path"],
                "value": 5,
                "supersedes_id": current_fact["id"],
                "evidence": [
                    {
                        "field_path": current_fact["field_path"],
                        "source_message_id": correction_message,
                        "source_span": "A has another corrected number of warriors",
                    }
                ],
            },
        )
        assert {issue["code"] for issue in stale["issues"]} == {"STALE_OBJECT_REFERENCE"}
