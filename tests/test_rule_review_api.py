import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from rulecourt.api import create_app
from rulecourt.root_adapter import RootAdapter


def package_payload(**overrides):
    source_content = "Law of Root Oct 2025\nSection 4.2: Move"
    payload = {
        "game_id": "root",
        "title": "Root Law draft for review",
        "source_type": "law",
        "revision": "2025-10",
        "authority": "official",
        "locator": "https://example.test/root-law#4.2",
        "scope_strategy": {
            "name": "M0 local move adjudication candidate",
            "included_factions": ["marquise", "eyrie"],
            "included_actions": ["move", "rule"],
            "notes": [
                "Candidate boundary; requires T03 human verification.",
                "Does not cover complete turn legality.",
            ],
        },
        "source_content": source_content,
        "checksum": hashlib.sha256(source_content.encode()).hexdigest(),
        "rules": [
            {
                "id": "root-4.2",
                "section": "4.2",
                "title": "Move",
                "text": "A faction may move warriors between adjacent clearings.",
                "scope": {"factions": ["marquise"], "actions": ["move"]},
                "keywords": ["move", "adjacent"],
            },
            {
                "id": "root-7.2.2",
                "section": "7.2.2",
                "title": "Lords of the Forest",
                "text": "The Eyrie rule for determining rule is recorded here.",
                "scope": {"factions": ["eyrie"], "actions": ["rule"]},
                "keywords": ["rule", "eyrie"],
            },
        ],
        "relations": [
            {"source_rule_id": "root-7.2.2", "target_rule_id": "root-4.2", "relation": "overrides"}
        ],
        "coverage_obligations": [
            {
                "id": "move-adjacency",
                "rule_ids": ["root-4.2"],
                "applies_when": ["The requested action is a warrior Move."],
                "acceptable_evidence": ["Inspected official rule 4.2."],
                "satisfied_when": ["Origin and destination are adjacent."],
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_rule_package_is_draft_until_review_and_public_index_stays_empty(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        assert "Rule package review" in client.get("/rules").text
        response = client.post("/api/rule-packages", json=package_payload())
        assert response.status_code == 201
        package = response.json()
        package_id = package["id"]
        assert package["status"] == "draft"
        assert package["enabled"] is False

        review_view = client.get(f"/api/rule-packages/{package_id}").json()
        assert review_view["scope_strategy"]["name"] == "M0 local move adjudication candidate"
        assert review_view["source_content"].startswith("Law of Root Oct 2025")
        assert review_view["coverage_obligations"][0]["id"] == "move-adjacency"
        assert review_view["reviews"] == []
        assert client.get("/api/rules", params={"q": "adjacent"}).json() == []
        assert client.get("/api/rules/root-4.2").status_code == 404


def test_verified_package_can_be_enabled_and_public_rules_exclude_private_coverage(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        package_id = client.post("/api/rule-packages", json=package_payload()).json()["id"]
        review = client.post(
            f"/api/rule-packages/{package_id}/reviews",
            json={
                "status": "verified",
                "reviewer_id": "human-reviewer-1",
                "basis": "Checked the source locator and the recorded section text.",
                "evidence": ["review-note:T02-demo-1"],
            },
        )
        assert review.status_code == 200
        assert review.json()["status"] == "verified"

        enabled = client.post(f"/api/rule-packages/{package_id}/enable")
        assert enabled.status_code == 200
        assert enabled.json()["enabled"] is True

        results = client.get("/api/rules", params={"q": "adjacent"}).json()
        assert [rule["id"] for rule in results] == ["root-4.2"]
        rule = client.get("/api/rules/root-4.2").json()
        assert rule["text"].startswith("A faction may move")
        assert rule["source"]["revision"] == "2025-10"
        assert rule["relations"][0]["relation"] == "overrides"
        assert "coverage_obligations" not in rule
        assert "source_content" not in rule


def test_disputed_or_unreviewed_package_cannot_be_enabled(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        draft_id = client.post("/api/rule-packages", json=package_payload()).json()["id"]
        assert client.post(f"/api/rule-packages/{draft_id}/enable").status_code == 409

        disputed_id = client.post(
            "/api/rule-packages",
            json=package_payload(title="Disputed rule draft", revision="2025-10-disputed"),
        ).json()["id"]
        response = client.post(
            f"/api/rule-packages/{disputed_id}/reviews",
            json={
                "status": "disputed",
                "reviewer_id": "human-reviewer-2",
                "basis": "Locator could not be independently checked.",
                "evidence": ["review-note:T02-demo-dispute"],
            },
        )
        assert response.status_code == 200
        assert client.post(f"/api/rule-packages/{disputed_id}/enable").status_code == 409


def test_package_validation_requires_matching_checksum_and_safe_coverage_shape(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        bad_checksum = client.post("/api/rule-packages", json=package_payload(checksum="0" * 64))
        assert bad_checksum.status_code == 422

        forbidden_field = package_payload()
        forbidden_field["coverage_obligations"][0]["query"] = "search the web"
        response = client.post("/api/rule-packages", json=forbidden_field)
        assert response.status_code == 422


def test_candidate_fixture_is_explicitly_unverified_and_importable(tmp_path):
    fixture_path = Path(__file__).parents[1] / "examples" / "root-m0-candidate-package.json"
    package = json.loads(fixture_path.read_text(encoding="utf-8"))
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        response = client.post("/api/rule-packages", json=package)
        assert response.status_code == 201
        result = response.json()
        assert result["status"] == "draft"
        assert "unverified" in result["title"]
        assert len(result["rules"]) == 11
        decree_state = {
            "action": {
                "type": "move",
                "actor": "eyrie",
                "origin": "A",
                "destination": "B",
                "warrior_count": 1,
            },
            "decree": {"column": "move", "card_suit": "bird"},
        }
        index = RootAdapter().rule_index(package, decree_state)
        assert index is not None
        assert index["path"] == "root-2.2.1"
        assert index["suit"] == "root-2.2.2"
        assert index["bird_wild"] == "root-2.1.1"
        assert index["decree_move"] == "root-7.5.2.II"
        ordinary_eyrie = {"action": {"type": "move", "actor": "eyrie"}}
        assert RootAdapter().rule_index(package, ordinary_eyrie)["eyrie_rule"] == "root-7.2.2"
        missing_eyrie_rule = {
            **package,
            "rules": [rule for rule in package["rules"] if rule["section"] != "7.2.2"],
        }
        assert RootAdapter().rule_index(missing_eyrie_rule, ordinary_eyrie) is None
        assert {"applies_when", "acceptable_evidence", "satisfied_when"} <= {
            key for obligation in result["coverage_obligations"] for key in obligation
        }


def test_public_rule_lookup_requires_a_package_for_duplicate_enabled_versions(tmp_path):
    with TestClient(
        create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret"),
        headers={"X-RuleCourt-Maintenance-Token": "test-secret"},
    ) as client:
        first = client.post("/api/rule-packages", json=package_payload()).json()
        second = client.post(
            "/api/rule-packages",
            json=package_payload(title="Root Law later candidate", revision="2025-11"),
        ).json()
        for package in (first, second):
            package_id = package["id"]
            review = client.post(
                f"/api/rule-packages/{package_id}/reviews",
                json={
                    "status": "verified",
                    "reviewer_id": "human-reviewer-version-test",
                    "basis": "Compared the versioned package source and relations.",
                    "evidence": ["review-note:T02-version-test"],
                },
            )
            assert review.status_code == 200
            assert client.post(f"/api/rule-packages/{package_id}/enable").status_code == 200

        ambiguous = client.get("/api/rules/root-4.2")
        assert ambiguous.status_code == 409
        selected = client.get("/api/rules/root-4.2", params={"package_id": first["id"]})
        assert selected.status_code == 200
        assert selected.json()["source"]["id"] == first["id"]


def test_coverage_and_review_operations_require_maintainer_token(tmp_path):
    app = create_app(tmp_path / "cases.sqlite3", provider=None, maintenance_token="test-secret")
    with TestClient(app) as client:
        token = {"X-RuleCourt-Maintenance-Token": "test-secret"}
        imported = client.post("/api/rule-packages", json=package_payload(), headers=token)
        assert imported.status_code == 201
        package_id = imported.json()["id"]
        assert client.get("/api/rule-packages").status_code == 403
        assert client.get(f"/api/rule-packages/{package_id}").status_code == 403
        assert (
            client.get(
                f"/api/rule-packages/{package_id}",
                headers={"X-RuleCourt-Maintenance-Token": "wrong"},
            ).status_code
            == 403
        )
        assert client.post("/api/rule-packages", json=package_payload()).status_code == 403
        assert (
            client.post(
                f"/api/rule-packages/{package_id}/reviews",
                json={
                    "status": "verified",
                    "reviewer_id": "self-signed",
                    "basis": "Unverified claim",
                    "evidence": ["none"],
                },
            ).status_code
            == 403
        )
        assert client.post(f"/api/rule-packages/{package_id}/enable").status_code == 403
        assert client.get("/api/rules").json() == []
        assert client.get(f"/api/rule-packages/{package_id}", headers=token).json()[
            "coverage_obligations"
        ]


def test_maintenance_endpoints_remain_closed_without_configured_token(tmp_path, monkeypatch):
    monkeypatch.delenv("RULECOURT_MAINTENANCE_TOKEN", raising=False)
    with TestClient(create_app(tmp_path / "cases.sqlite3", provider=None)) as client:
        assert client.get("/api/rule-packages").status_code == 503
        assert client.post("/api/rule-packages", json=package_payload()).status_code == 503
