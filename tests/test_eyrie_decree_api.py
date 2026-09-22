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
    source_content = "Root Law October 2025: movement, rule, Eyrie and Decree excerpts"
    rules = [
        ("root-2.1", "2.1", "Clearing suit", "Each clearing has a suit."),
        ("root-2.2", "2.2", "Path", "A path connects adjacent clearings for movement."),
        (
            "root-2.5",
            "2.5",
            "Rule",
            "A faction rules a clearing if it has more pieces there than any other faction.",
        ),
        (
            "root-4.2",
            "4.2",
            "Move",
            "A faction may move warriors from one clearing to an adjacent clearing.",
        ),
        (
            "root-4.2.1",
            "4.2.1",
            "Move restriction",
            "A faction must rule the origin or destination clearing to move.",
        ),
        (
            "root-7.2.2",
            "7.2.2",
            "Lords of the Forest",
            "The Eyrie may rule when tied for most pieces and present in the clearing.",
        ),
        (
            "root-7.5.2",
            "7.5.2",
            "Resolve the Decree",
            "The Eyrie resolves the Move column during Daylight from the matching suit.",
        ),
    ]
    return {
        "game_id": "root",
        "title": "Verified Root M0 Eyrie movement rules",
        "source_type": "law",
        "revision": "2025-10",
        "authority": "official",
        "locator": "https://example.test/root-law#eyrie-decree",
        "scope_strategy": {
            "name": "M0 local Eyrie and Marquise move adjudication",
            "included_factions": ["eyrie", "marquise"],
            "included_actions": ["move"],
            "notes": ["Local move conditions only; not complete turn legality."],
        },
        "source_content": source_content,
        "checksum": hashlib.sha256(source_content.encode()).hexdigest(),
        "rules": [
            {
                "id": rule_id,
                "section": section,
                "title": title,
                "text": text,
                "scope": {"actions": ["move"], "tags": ["movement"]},
                "keywords": [title.lower(), "move"],
            }
            for rule_id, section, title, text in rules
        ],
        "relations": [
            {"source_rule_id": "root-7.2.2", "target_rule_id": "root-2.5", "relation": "overrides"},
            {"source_rule_id": "root-7.5.2", "target_rule_id": "root-4.2", "relation": "depends_on"},
        ],
        "coverage_obligations": [
            {
                "id": "eyrie-move-coverage",
                "rule_ids": [rule_id for rule_id, *_ in rules],
                "applies_when": ["The requested action is an Eyrie or Marquise warrior Move."],
                "acceptable_evidence": ["The reviewed Root source sections cover the local move."],
                "satisfied_when": ["The deterministic Root adapter validates the action."],
            }
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
            "reviewer_id": "t08-reviewer",
            "basis": "Compared the movement and Eyrie excerpts with the reviewed source.",
            "evidence": ["review-note:t08-move"],
        },
        headers=headers,
    )
    assert reviewed.status_code == 200
    enabled = client.post(f"/api/rule-packages/{package_id}/enable", headers=headers)
    assert enabled.status_code == 200


def golden_case(
    *, marquise_a=3, eyrie_b=0, card_suit="Fox", origin_suit="fox", phase="Daylight"
):
    return (
        "确认本次只裁决 Eyrie Decree Move 范围。"
        f"当前为 {phase}。"
        f"A 是 {origin_suit} clearing。"
        "Eyrie 在 A 有 2 个 warriors。"
        "Eyrie 在 A 有 1 个 roost。"
        f"Marquise 在 A 有 {marquise_a} 个 warriors。"
        "Marquise 在 A 有 0 个 buildings。"
        f"Eyrie 在 B 有 {eyrie_b} 个 warriors。"
        "Eyrie 在 B 有 0 个 buildings。"
        "Marquise 在 B 有 0 个 warriors。"
        "Marquise 在 B 有 0 个 buildings。"
        "B 为空。"
        "A 只与 B 相邻。"
        f"我的 {card_suit} Decree Move 从 A 移动 1 个 warriors 到 B。"
    )


def ordinary_eyrie_case():
    return (
        "Scope is limited to Eyrie ordinary Move. "
        "Eyrie at A has 2 warriors. "
        "Eyrie at A has 1 roost. "
        "Marquise at A has 3 warriors. "
        "Marquise at A has 0 buildings. "
        "Eyrie at B has 0 warriors. "
        "Eyrie at B has 0 buildings. "
        "Marquise at B has 0 warriors. "
        "Marquise at B has 0 buildings. "
        "B is empty. "
        "A is adjacent to B. "
        "Eyrie from A move 1 warrior to B."
    )


def new_case(client):
    return client.post("/api/cases", json={}).json()["id"]


def test_eyrie_decree_golden_case_uses_tie_override_and_returns_legal(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)

        result = client.post(
            f"/api/cases/{case_id}/messages", json={"text": golden_case()}
        ).json()

        assert result["status"] == "LEGAL"
        assert result["reason"] == "MOVE_LEGAL"
        assert result["decision"]["status"] == "allow"
        assert result["decision"]["derived_facts"]["origin_ruler"]["value"] == "eyrie"
        assert "root-7.2.2" in result["evidence"]
        assert "root-7.5.2" in result["evidence"]
        assert "full_decree_progress" in result["not_checked"]
        assert "full_turn_action_availability" in result["not_checked"]


def test_eyrie_ordinary_move_uses_the_same_local_predicate(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)

        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": ordinary_eyrie_case()},
        ).json()

        assert result["status"] == "LEGAL"
        assert result["reason"] == "MOVE_LEGAL"
        assert "root-7.2.2" in result["evidence"]
        assert "full_decree_progress" in result["not_checked"]


def test_eyrie_decree_mutations_recalculate_rule_and_destination_override(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)

        first = client.post(
            f"/api/cases/{case_id}/messages", json={"text": golden_case(marquise_a=4)}
        ).json()
        assert first["status"] == "ILLEGAL"
        assert first["reason"] == "MOVE_RULES_NEITHER_CLEARING"

        second = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "更正：B 有 1 个老鹰兵。"},
        ).json()
        assert second["status"] == "LEGAL"
        assert second["decision"]["derived_facts"]["destination_ruler"]["value"] == "eyrie"


def test_decree_suit_mismatch_is_illegal_and_bird_is_wildcard(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)

        mismatch = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": golden_case(card_suit="Rabbit")},
        ).json()
        assert mismatch["status"] == "ILLEGAL"
        assert mismatch["reason"] == "DECREE_SUIT_MISMATCH"

        bird_case_id = new_case(client)
        bird = client.post(
            f"/api/cases/{bird_case_id}/messages",
            json={"text": golden_case(card_suit="Bird")},
        ).json()
        assert bird["status"] == "LEGAL"
        assert bird["decision"]["derived_facts"]["decree_suit"]["value"] == "bird"


def test_decree_missing_context_returns_relevant_fields(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Eyrie Decree Move 范围。"
                    "Eyrie 在 A 有 2 个 warriors。Eyrie 在 A 有 1 个 roost。"
                    "Marquise 在 A 有 3 个 warriors。Marquise 在 A 有 0 个 buildings。"
                    "A 只与 B 相邻。"
                    "Eyrie 的 Decree Move 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()

        assert result["status"] == "INSUFFICIENT_INFORMATION"
        assert "phase" in result["missing_fields"]
        assert "decree.card_suit" in result["missing_fields"]
        assert "clearings.A.suit" in result["missing_fields"]
        assert result["clarification_questions"]


def test_incomplete_decree_query_uses_deterministic_clarification_path(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "Scope is limited to Eyrie Decree Move."},
        ).json()

        assert result["status"] == "INSUFFICIENT_INFORMATION"
        assert result["investigation"]["strategy"] == "fixed_workflow"
        assert result["decision"]["status"] == "unknown"
        assert "phase" in result["missing_fields"]
        assert "decree.card_suit" in result["missing_fields"]
        assert "action.origin" in result["missing_fields"]


def test_unsupported_root_faction_fails_closed_before_adjudication(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={"text": "Scope is limited to Woodland Alliance Move."},
        ).json()

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "STATE_SCOPE_OUT_OF_BOUNDS"


def test_marquise_decree_interaction_is_unresolved(tmp_path):
    with TestClient(
        create_app(
            tmp_path / "cases.sqlite3",
            provider=PassiveProvider(),
            maintenance_token="test-secret",
        )
    ) as client:
        install_verified_package(client)
        case_id = new_case(client)
        result = client.post(
            f"/api/cases/{case_id}/messages",
            json={
                "text": (
                    "确认本次只裁决 Marquise Decree Move 范围。"
                    "Marquise 从 A 移动 1 个 warriors 到 B。"
                )
            },
        ).json()

        assert result["status"] == "UNRESOLVED"
        assert result["reason"] == "UNSUPPORTED_INTERACTION"
