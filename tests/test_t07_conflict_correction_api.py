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


def test_public_correction_resolves_an_explicit_conflict_candidate(tmp_path):
    app = create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())
    with TestClient(app) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]
        for text in ("A 有 2 个猫兵。", "A 有 3 个猫兵。"):
            response = client.post(
                f"/api/cases/{case_id}/messages",
                json={"text": text},
            )
            assert response.status_code == 200
        conflicted = client.get(f"/api/cases/{case_id}").json()
        candidate = next(
            item
            for item in conflicted["state_facts"]
            if item["status"] == "conflicted" and item["value"] == 3
        )

        client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "更正：A 有 4 个猫兵。"},
        )
        current = client.get(f"/api/cases/{case_id}").json()
        source_message = current["messages"][-1]["id"]
        resolved = client.post(
            f"/api/cases/{case_id}/state",
            json={
                "expected_state_revision": current["revision"],
                "changes": [
                    {
                        "operation": "correct",
                        "field_path": candidate["field_path"],
                        "value": 4,
                        "supersedes_id": candidate["id"],
                        "evidence": [
                            {
                                "field_path": candidate["field_path"],
                                "source_message_id": source_message,
                                "source_span": "A has four warriors",
                            }
                        ],
                    }
                ],
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["accepted"] is True
        updated = client.get(f"/api/cases/{case_id}").json()
        active = next(
            item
            for item in updated["state_facts"]
            if item["status"] == "active" and item["field_path"] == candidate["field_path"]
        )
        assert active["value"] == 4
        assert active["supersedes_id"] == candidate["id"]
