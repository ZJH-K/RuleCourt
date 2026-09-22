import hashlib

from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse

from rulecourt.api import create_app


class PassiveProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="passive")

    def get_default_model(self):
        return "passive-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        return LLMResponse(content="The move is legal.")


def package_payload():
    source_content = "Root Law October 2025: movement and rule excerpts"
    return {
        "game_id": "root",
        "title": "Verified Root M0 movement rules",
        "source_type": "law",
        "revision": "2025-10",
        "authority": "official",
        "locator": "https://example.test/root-law#movement",
        "scope_strategy": {
            "name": "M0 Marquise ordinary move",
            "included_factions": ["marquise"],
            "included_actions": ["move"],
            "notes": ["Local move conditions only; not complete turn legality."],
        },
        "source_content": source_content,
        "checksum": hashlib.sha256(source_content.encode()).hexdigest(),
        "rules": [
            {
                "id": "root-2.2",
                "section": "2.2",
                "title": "Path",
                "text": "A path connects adjacent clearings for movement.",
                "scope": {"actions": ["move"], "tags": ["movement"]},
                "keywords": ["path", "adjacent"],
            },
            {
                "id": "root-2.5",
                "section": "2.5",
                "title": "Rule",
                "text": "A faction rules a clearing if it has more pieces there than any other faction.",
                "scope": {"actions": ["rule"], "tags": ["control"]},
                "keywords": ["rule", "warriors", "buildings"],
            },
            {
                "id": "root-4.2",
                "section": "4.2",
                "title": "Move",
                "text": "A faction may move warriors from one clearing to an adjacent clearing.",
                "scope": {
                    "actions": ["move"],
                    "tags": ["movement"],
                },
                "keywords": ["move", "adjacent", "warriors"],
            },
            {
                "id": "root-4.2.1",
                "section": "4.2.1",
                "title": "Move restriction",
                "text": "A faction must rule the origin or destination clearing to move.",
                "scope": {"actions": ["move"], "tags": ["movement", "rule"]},
                "keywords": ["move", "origin", "destination", "rule"],
            },
        ],
        "coverage_obligations": [
            {
                "id": "movement-adjacency",
                "rule_ids": ["root-4.2", "root-4.2.1"],
                "applies_when": ["The requested action is a Move between clearings."],
                "acceptable_evidence": ["Verified source sections 4.2 and 4.2.1."],
                "satisfied_when": ["The origin and destination are adjacent."],
            },
            {
                "id": "movement-path",
                "rule_ids": ["root-2.2"],
                "applies_when": ["The requested action moves warriors between clearings."],
                "acceptable_evidence": ["Verified source section 2.2."],
                "satisfied_when": ["The clearings are connected by a path."],
            },
            {
                "id": "rule-control",
                "rule_ids": ["root-2.5"],
                "applies_when": ["The requested action depends on clearing rule."],
                "acceptable_evidence": ["Verified source section 2.5."],
                "satisfied_when": ["The moving faction rules one endpoint."],
            },
        ],
    }


def install_verified_package(client):
    headers = {"X-RuleCourt-Maintenance-Token": "test-secret"}
    imported = client.post("/api/rule-packages", json=package_payload(), headers=headers)
    assert imported.status_code == 201
    package_id = imported.json()["id"]
    reviewed = client.post(
        f"/api/rule-packages/{package_id}/reviews",
        json={
            "status": "verified",
            "reviewer_id": "t03-reviewer",
            "basis": "Compared the movement excerpts with the reviewed source.",
            "evidence": ["review-note:t03-move"],
        },
        headers=headers,
    )
    assert reviewed.status_code == 200
    enabled = client.post(f"/api/rule-packages/{package_id}/enable", headers=headers)
    assert enabled.status_code == 200


def complete_marquise_move(
    count=1,
    adjacency="B",
    marquise_a=3,
    eyrie_a=0,
    marquise_b=0,
    eyrie_b=0,
):
    return (
        "确认本次只裁决 Marquise 普通移动范围。"
        f"Marquise 在 A 有 {marquise_a} 个 warriors。"
        "Marquise 在 A 有 0 个 buildings。"
        f"Eyrie 在 A 有 {eyrie_a} 个 warriors。"
        "Eyrie 在 A 有 0 个 buildings。"
        f"Marquise 在 B 有 {marquise_b} 个 warriors。"
        "Marquise 在 B 有 0 个 buildings。"
        f"Eyrie 在 B 有 {eyrie_b} 个 warriors。"
        "Eyrie 在 B 有 0 个 buildings。"
        f"A 只与 {adjacency} 相邻。"
        f"Marquise 从 A 移动 {count} 个 warriors 到 B。"
    )


def test_verified_marquise_move_returns_legal_with_rule_evidence(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        response = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": complete_marquise_move()},
        )

        assert response.status_code == 200
        result = response.json()
        assert result["status"] == "LEGAL"
        assert result["state_update"]["sufficient"] is True
        assert result["scope"] == "local_move_conditions"
        assert result["evidence"] == ["root-2.2", "root-2.5", "root-4.2", "root-4.2.1"]
        assert result["checks"] == {"state_sufficient": True, "evidence_verified": True}
        assert "full_turn_action_availability" in result["not_checked"]
        assert result["decision"]["status"] == "allow"
        assert result["verification"]["status"] == "passed"
        case = client.get(f"/api/cases/{case_id}").json()
        assert case["decisions"][0]["status"] == "allow"
        assert case["verifications"][0]["status"] == "passed"
        events = client.get(f"/api/cases/{case_id}/events").json()
        assert any(event["type"] == "decision_computed" for event in events)
        assert any(event["type"] == "verification_finished" for event in events)


def test_zero_warrior_move_is_illegal_even_if_provider_would_say_legal(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": complete_marquise_move(count=0)},
        ).json()

        assert result["status"] == "ILLEGAL"
        assert result["reason"] == "MOVE_ZERO_PIECES"
        assert result["decision"]["status"] == "deny"
        assert result["verification"]["status"] == "passed"


def test_move_with_insufficient_origin_warriors_is_illegal(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": complete_marquise_move(count=4)},
        ).json()

        assert result["status"] == "ILLEGAL"
        assert result["reason"] == "MOVE_INSUFFICIENT_WARRIORS"


def test_move_to_a_non_adjacent_clearing_is_illegal(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": complete_marquise_move(adjacency="C")},
        ).json()

        assert result["status"] == "ILLEGAL"
        assert result["reason"] == "MOVE_NOT_ADJACENT"


def test_move_is_illegal_when_marquise_rules_neither_endpoint(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": complete_marquise_move(
                    marquise_a=1,
                    eyrie_a=2,
                    marquise_b=0,
                    eyrie_b=2,
                )
            },
        ).json()

        assert result["status"] == "ILLEGAL"
        assert result["reason"] == "MOVE_RULES_NEITHER_CLEARING"


def test_missing_adjacency_returns_insufficient_information(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
        text = "确认本次只裁决 Marquise 普通移动范围。Marquise 从 A 移动 1 个 warriors 到 B。"

        result = client.post(f"/api/cases/{case_id}/messages", json={"text": text}).json()

        assert result["status"] == "INSUFFICIENT_INFORMATION"
        assert result["reason"] == "INSUFFICIENT_INFORMATION"
        assert result["decision"]["status"] == "unknown"
        assert "clearings.A.adjacent_to" in result["missing_fields"]


def test_eyrie_ordinary_move_is_explicitly_unsupported_in_t05(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
        text = complete_marquise_move().replace(
            "确认本次只裁决 Marquise 普通移动范围。",
            "确认本次只裁决 Eyrie 普通移动范围。",
        )

        result = client.post(f"/api/cases/{case_id}/messages", json={"text": text}).json()

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "UNSUPPORTED_FACTION"


def test_missing_verified_rule_package_fails_closed_at_verification_gate(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": complete_marquise_move()},
        ).json()

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "VERIFICATION_NOT_SATISFIED"
        assert result["verification"]["status"] == "failed"
        assert result["evidence"] == []


def test_insufficient_information_requests_only_relevant_fields_and_resumes_same_case(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]

        first = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()

        assert first["status"] == "INSUFFICIENT_INFORMATION"
        assert first["reason"] == "INSUFFICIENT_INFORMATION"
        assert first["clarification_questions"]
        question_fields = {question["field"] for question in first["clarification_questions"]}
        assert "clearings.A.adjacent_to" in question_fields
        assert "clearings.A.presence.marquise.warriors" in question_fields
        assert all(question["question"] for question in first["clarification_questions"])

        second = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "A 只与 B 相邻。"
                )
            },
        ).json()

        assert second["status"] == "LEGAL"
        assert second["clarification_questions"] == []
        case = client.get(f"/api/cases/{case_id}").json()
        assert len(case["messages"]) == 2
        assert [verdict["status"] for verdict in case["verdicts"]] == [
            "INSUFFICIENT_INFORMATION",
            "LEGAL",
        ]
        assert case["verdicts"][0]["details"]["clarification_questions"]
        warrior_fact = next(
            fact
            for fact in case["state_facts"]
            if fact["field_path"] == "clearings.A.presence.marquise.warriors"
            and fact["status"] == "active"
        )
        assert warrior_fact["evidence_refs"][0]["source_message_id"] == case["messages"][1]["id"]
        events = client.get(f"/api/cases/{case_id}/events").json()
        assert any(event["type"] == "clarification_requested" for event in events)
        assert any(event["type"] == "clarification_resumed" for event in events)


def test_known_origin_ruler_does_not_request_unknown_destination_presence(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
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
        assert result["missing_fields"] == []
        assert result["clarification_questions"] == []
        assert "destination_ruler" not in result["decision"]["derived_facts"]
        case = client.get(f"/api/cases/{case_id}").json()
        assert "B" not in case["confirmed_state"].get("clearings", {})
        assert "clearings.B.presence" in case["unknown_fields"]


def test_known_insufficient_warriors_denies_without_unrelated_adjacency_facts(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 1 个 warriors。"
                    "Marquise 从 A 移动 2 个 warriors 到 B。"
                )
            },
        ).json()

        assert result["status"] == "ILLEGAL"
        assert result["reason"] == "MOVE_INSUFFICIENT_WARRIORS"
        assert result["missing_fields"] == []
        assert result["clarification_questions"] == []


def test_partial_and_complete_adjacency_change_the_move_result(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        partial_case = client.post("/api/cases", json={}).json()["id"]
        partial = client.post(
            f"/api/cases/{partial_case}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "A 与 C 相邻。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()
        assert partial["status"] == "INSUFFICIENT_INFORMATION"
        assert partial["reason"] == "INSUFFICIENT_INFORMATION"
        assert "clearings.A.adjacent_to" in partial["missing_fields"]
        assert partial["reason"] != "MOVE_NOT_ADJACENT"

        complete_case = client.post("/api/cases", json={}).json()["id"]
        complete = client.post(
            f"/api/cases/{complete_case}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "A 只与 C 相邻。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()
        assert complete["status"] == "ILLEGAL"
        assert complete["reason"] == "MOVE_NOT_ADJACENT"


def test_unavailable_unknown_stays_insufficient_after_user_cannot_supply_it(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
        first = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise 普通移动范围。"
                    "Marquise 在 A 有 3 个 warriors。"
                    "Marquise 在 A 有 0 个 buildings。"
                    "Eyrie 在 A 有 0 个 warriors。"
                    "Eyrie 在 A 有 0 个 buildings。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()
        assert first["status"] == "INSUFFICIENT_INFORMATION"
        assert "clearings.A.adjacent_to" in first["missing_fields"]

        second = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "我不知道 A 与谁相邻。"},
        ).json()

        assert second["status"] == "INSUFFICIENT_INFORMATION"
        assert second["reason"] == "INSUFFICIENT_INFORMATION"
        assert second["missing_fields"] == ["clearings.A.adjacent_to"]
        assert [question["field"] for question in second["clarification_questions"]] == [
            "clearings.A.adjacent_to"
        ]
        case = client.get(f"/api/cases/{case_id}").json()
        assert len(case["messages"]) == 2
        assert [verdict["status"] for verdict in case["verdicts"]] == [
            "INSUFFICIENT_INFORMATION",
            "INSUFFICIENT_INFORMATION",
        ]


def test_scope_unknown_is_a_clarification_before_using_complete_move_facts(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = client.post("/api/cases", json={}).json()["id"]
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": complete_marquise_move().replace(
                    "确认本次只裁决 Marquise 普通移动范围。",
                    "",
                )
            },
        ).json()

        assert result["status"] == "INSUFFICIENT_INFORMATION"
        assert result["reason"] == "SCOPE_NOT_CONFIRMED"
        assert result["missing_fields"] == ["scope.root-local-move"]
        assert result["clarification_questions"][0]["field"] == "scope.root-local-move"


class FailingProvider(PassiveProvider):
    async def chat(self, messages, tools=None, model=None, **kwargs):
        raise RuntimeError("provider unavailable")


def test_provider_failure_is_unresolved_not_information_insufficient(tmp_path):
    with TestClient(create_app(tmp_path / "cases.sqlite3", provider=FailingProvider())) as client:
        case_id = client.post("/api/cases", json={}).json()["id"]
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "Can my warrior move?"},
        ).json()

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "INVESTIGATION_FAILED"
        assert result.get("clarification_questions", []) == []
