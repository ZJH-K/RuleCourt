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


def test_natural_language_keeps_zero_distinct_from_unknown_and_records_source(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)

        result = submit(client, case_id, "Eyrie 在 A 有 0 个兵，想从 A 移到 B。")
        assert result["state_update"]["accepted"] is True

        case = client.get(f"/api/cases/{case_id}").json()
        assert case["confirmed_state"]["clearings"]["A"]["presence"]["eyrie"]["warriors"] == 0
        assert "marquise" not in case["confirmed_state"]["clearings"]["A"]["presence"]
        assert "B" not in case["confirmed_state"]["clearings"]
        fact = next(
            item
            for item in case["state_facts"]
            if item["field_path"] == "clearings.A.presence.eyrie.warriors"
        )
        assert fact["evidence_refs"][0]["source_message_id"] == case["messages"][0]["id"]
        assert "0 个兵" in fact["evidence_refs"][0]["source_span"]
        assert "clearings.B.presence" in case["unknown_fields"]


def test_complete_empty_warrior_list_does_not_make_buildings_zero(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)

        result = submit(client, case_id, "A 只有 0 个老鹰兵，老鹰兵清单是完整的。")
        assert result["state_update"]["accepted"] is True

        case = client.get(f"/api/cases/{case_id}").json()
        presence = case["confirmed_state"]["clearings"]["A"]["presence"]
        assert presence["eyrie"]["warriors"] == 0
        assert "buildings" not in presence["eyrie"]
        assertion = case["completeness_assertions"][0]
        assert assertion["status"] == "confirmed"
        assert assertion["completeness"] == "complete"
        assert assertion["covered_scope"] == {
            "kind": "presence",
            "clearing_id": "A",
            "faction": "eyrie",
            "piece_type": "warriors",
        }
        assert assertion["member_snapshot"]["members"] == []


def test_partial_adjacency_is_not_an_exhaustive_list(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)

        result = submit(client, case_id, "A 与 B 相邻。")
        assert result["state_update"]["accepted"] is True

        case = client.get(f"/api/cases/{case_id}").json()
        assert case["confirmed_state"]["clearings"]["A"]["adjacent_to"] == ["B"]
        assertion = case["completeness_assertions"][0]
        assert assertion["completeness"] == "partial"
        assert assertion["collection_target"] == "clearings.A.adjacent_to"
        assert assertion["covered_scope"]["kind"] == "adjacency"
        assert assertion["covered_scope"]["clearing_id"] == "A"


def test_out_of_scope_declaration_is_explained_and_not_accepted(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)

        result = submit(client, case_id, "按 Vagabond 的规则判断这次移动，范围只包括 Vagabond。")
        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "STATE_SCOPE_OUT_OF_BOUNDS"
        assert result["state_update"]["accepted"] is False
        assert any(
            issue["code"] == "SCOPE_OUT_OF_BOUNDS" for issue in result["state_update"]["issues"]
        )

        case = client.get(f"/api/cases/{case_id}").json()
        assert case["confirmed_state"] == {}
        assert case["scope_assumptions"] == []
        assert any(
            event["type"] == "state_rejected"
            for event in client.get(f"/api/cases/{case_id}/events").json()
        )


def test_scope_question_does_not_confirm_an_assumption(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "这个范围是什么？")
        case = client.get(f"/api/cases/{case_id}").json()
        assert case["scope_assumptions"] == []
        assert case["revision"] == 0


def test_explicit_correction_invalidates_old_complete_zero(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "A 只有 0 个老鹰兵，老鹰兵清单是完整的。")
        result = submit(client, case_id, "更正：A 有 2 个老鹰兵。")
        assert result["state_update"]["accepted"] is True
        case = client.get(f"/api/cases/{case_id}").json()
        assert case["confirmed_state"]["clearings"]["A"]["presence"]["eyrie"]["warriors"] == 2
        assert case["completeness_assertions"][0]["status"] == "invalidated"


def test_explicit_adjacency_correction_replaces_complete_list(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "A 只与 B 相邻。")
        result = submit(client, case_id, "更正：A 与 C 相邻。")
        assert result["state_update"]["accepted"] is True
        case = client.get(f"/api/cases/{case_id}").json()
        assert case["confirmed_state"]["clearings"]["A"]["adjacent_to"] == ["C"]
        assert case["completeness_assertions"][0]["status"] == "invalidated"


def test_retraction_restores_unknown_and_invalidates_complete_list(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "A 只有 0 个老鹰兵，老鹰兵清单是完整的。")
        result = submit(client, case_id, "撤回 A 的老鹰兵数量。")
        assert result["state_update"]["accepted"] is True
        case = client.get(f"/api/cases/{case_id}").json()
        assert "clearings.A.presence.eyrie.warriors" in case["unknown_fields"]
        assert "presence" not in case["confirmed_state"].get("clearings", {}).get("A", {})
        assert case["completeness_assertions"][0]["status"] == "invalidated"


def test_unrelated_presence_list_stays_complete_after_another_faction_changes(tmp_path):
    with TestClient(create_app(tmp_path / "case.sqlite3", provider=PassiveProvider())) as client:
        case_id = new_case(client)
        submit(client, case_id, "A 只有 0 个老鹰兵，老鹰兵清单是完整的。")
        result = submit(client, case_id, "A 有 3 个猫兵。")
        assert result["state_update"]["accepted"] is True
        case = client.get(f"/api/cases/{case_id}").json()
        presence = case["confirmed_state"]["clearings"]["A"]["presence"]
        assert presence["eyrie"]["warriors"] == 0
        assert presence["marquise"]["warriors"] == 3
        assert case["completeness_assertions"][0]["status"] == "confirmed"
