import asyncio
import threading
from threading import Thread

from fastapi.testclient import TestClient
from nanobot.providers.base import LLMProvider, LLMResponse

from rulecourt.api import create_app


class BlockingProvider(LLMProvider):
    def __init__(self):
        super().__init__(provider_name="blocking")
        self.started = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self.calls = 0

    def get_default_model(self):
        return "blocking-model"

    async def chat(self, messages, tools=None, model=None, **kwargs):
        with self._lock:
            self.calls += 1
            call_number = self.calls
        if call_number == 1:
            self.started.set()
            await asyncio.to_thread(self.release.wait, 10)
        return LLMResponse(content="No authoritative ruling.")


def test_concurrent_correction_discards_old_investigation_result(tmp_path):
    provider = BlockingProvider()
    app = create_app(tmp_path / "case.sqlite3", provider=provider)
    first_result = {}

    with TestClient(app) as first_client, TestClient(app) as second_client:
        case_id = first_client.post("/api/cases", json={}).json()["id"]

        def investigate_old_state():
            response = first_client.post(
                f"/api/cases/{case_id}/messages", json={"text": "A 有 2 个猫兵。"}
            )
            first_result["response"] = response

        worker = Thread(target=investigate_old_state)
        worker.start()
        assert provider.started.wait(5)

        correction = second_client.post(
            f"/api/cases/{case_id}/messages", json={"text": "更正：A 有 3 个猫兵。"}
        )
        assert correction.status_code == 200
        assert correction.json()["state_update"]["accepted"] is True

        provider.release.set()
        worker.join(timeout=10)
        assert not worker.is_alive()

        stale_response = first_result["response"]
        assert stale_response.status_code == 200
        assert stale_response.json()["reason"] == "STATE_REVISION_CONFLICT"

        case = second_client.get(f"/api/cases/{case_id}").json()
        assert case["revision"] == 2
        assert len(case["verdicts"]) == 1
        assert case["verdicts"][0]["revision"] == 2
        assert all(verdict["reason"] != "DOMAIN_NOT_IMPLEMENTED" for verdict in case["verdicts"])
        events = second_client.get(f"/api/cases/{case_id}/events").json()
        assert any(event["type"] == "adjudication_discarded" for event in events)
