"""Changing the diagnosis must select its own draft without deleting history."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.models import Confirm
from mechanic import main
from mechanic.config import ROOT
from mechanic.contracts import Diagnosis, DraftFields, Reply
from mechanic.state import Store
from mechanic.team_workflow import TeamWorkflow


PAIR = {"vehicle_id": "E549YT198", "session_id": "S-0011"}


@pytest.fixture
def changing_case(tmp_path, monkeypatch):
    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "workflow.sqlite3"))
    adapter = TeamWorkflow(ROOT, main.CATALOG_PATH, main.ML_URL, main.TOIR_URL, main.MOCK_TIMEOUT)
    original = next(session for session in main.VEHICLE_BY_ID[PAIR["vehicle_id"]]["sessions"]
                    if session["session_id"] == PAIR["session_id"])
    response = Diagnosis.model_validate(original).model_dump()
    sent = []

    def toir(request):
        sent.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "created", "ticket_id": f"TKT-{len(sent):04d}"})

    adapter.ml.close()
    adapter.toir.close()
    adapter.ml = httpx.Client(base_url=main.ML_URL,
                              transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response)))
    adapter.toir = httpx.Client(base_url=main.TOIR_URL, transport=httpx.MockTransport(toir))
    adapter.workflow.ml, adapter.workflow.toir = adapter.ml, adapter.toir
    monkeypatch.setattr(main, "team", adapter)
    monkeypatch.setattr(main, "store", Store(tmp_path / "screen.sqlite3", send_deadline=1))
    monkeypatch.setattr(main, "case_agent", None)
    monkeypatch.setattr(main, "TEAM_API_URL", "")
    monkeypatch.setattr(main, "TEAM_AGENT_URL", "")
    with TestClient(main.app) as client:
        yield client, adapter, response, sent


def diagnose(client):
    result = client.post("/api/diagnose", json=PAIR)
    assert result.status_code == 200, result.text
    return result.json()


def create_oil(client):
    diagnose(client)
    response = client.post("/api/draft", json=PAIR)
    assert response.status_code == 200, response.text
    return response.json()


def edits(state):
    return {**PAIR, "draft_id": state["draft_id"], "revision": state["draft_revision"],
            "edits": {key: state["draft"][key] for key in ("conclusion", "urgency", "operations", "spare_parts")}}


def set_valves(response):
    response.update(anomaly_type="F01", anomaly_title="Клапанный механизм",
                    description="Изменённый ответ ML: требуется проверка клапанного механизма.")


def test_each_diagnosis_selects_its_draft_and_rejects_old_requests(changing_case, monkeypatch):
    client, adapter, response, sent = changing_case
    oil = create_oil(client)
    old_payload = edits(oil)
    set_valves(response)
    changed = diagnose(client)
    assert changed["diagnosis"]["anomaly_type"] == "F01"
    assert changed["draft"] is None
    assert changed["ticket"]["status"] == "idle"
    assert client.put("/api/draft", json=old_payload).status_code == 409
    assert client.post("/api/ticket", json={**old_payload, "priority_confirmed": True}).status_code == 409
    assert sent == []

    class VerifiedValveCheck:
        def can_prepare(self, _vehicle, _diagnosis, _history=()):
            return True

        def chat(self, vehicle, diagnosis, _question, _history):
            fields = adapter.workflow.mechanic.suggest_draft(diagnosis, vehicle["engine_model"]).model_dump()
            fields["urgency"] = "medium"
            return Reply(summary="Проверка по действующему диагнозу."), DraftFields.model_validate(fields), None

    monkeypatch.setattr(main, "case_agent", VerifiedValveCheck())
    prepared = client.post("/api/draft", json=PAIR)
    assert prepared.status_code == 200, prepared.text
    valves = adapter.workflow.get_draft(prepared.json()["draft_id"])
    assert valves.anomaly_type == "F01"
    assert valves.draft_id != oil["draft_id"]
    restored = client.get("/api/state", params=PAIR).json()
    assert restored["draft_id"] == valves.draft_id
    assert restored["draft"]["anomaly_type"] == "F01"
    assert client.post("/api/draft", json=PAIR).json()["draft_id"] == valves.draft_id
    assert sent == []
    submission = {**edits(restored), "priority_confirmed": True}
    submitted = client.post("/api/ticket", json=submission)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["ticket"]["status"] == "created"
    assert sent[0]["anomaly_type"] == "F01"
    assert client.post("/api/ticket", json=submission).json()["ticket"] == submitted.json()["ticket"]
    assert len(sent) == 1

    response.update(anomaly_type="F02", anomaly_title="Низкое давление масла")
    back_to_oil = diagnose(client)
    assert back_to_oil["draft_id"] == oil["draft_id"], "F01 was created later but must not replace F02"
    assert back_to_oil["draft_revision"] == oil["draft_revision"]
    assert client.post("/api/draft", json=PAIR).json()["draft_id"] == oil["draft_id"]
    history = adapter.workflow.drafts(adapter.Pair(**PAIR))
    assert {draft.draft_id for draft in history} == {oil["draft_id"], valves.draft_id}


@pytest.mark.parametrize("status", ["normal", "check_failed"])
def test_non_anomaly_hides_old_draft_without_deleting_it(changing_case, status):
    client, adapter, response, sent = changing_case
    oil = create_oil(client)
    response.update(status=status, anomaly_type=None, anomaly_title=None)
    if status == "check_failed":
        response.update(anomaly_score=None, confidence=None, signal_quality="bad")
    state = diagnose(client)
    assert state["draft"] is None
    assert state["ticket"]["status"] == "idle"
    assert state["draft_available"] is False
    assert client.get("/api/state", params=PAIR).json()["draft"] is None
    assert adapter.workflow.drafts(adapter.Pair(**PAIR))[0].draft_id == oil["draft_id"]
    assert sent == []


@pytest.mark.parametrize("old_status", ["sent", "delivery_unknown"])
def test_historical_delivery_does_not_block_new_diagnosis(changing_case, old_status):
    client, adapter, response, sent = changing_case
    oil = create_oil(client)
    if old_status == "sent":
        adapter.workflow.confirm_draft(oil["draft_id"], Confirm(
            revision=oil["draft_revision"], urgency="medium", confirmed=True))
    else:
        with adapter.workflow.store.connection(write=True) as conn:
            conn.execute("UPDATE drafts SET state = 'delivery_unknown' WHERE draft_id = ?", (oil["draft_id"],))
    assert client.get("/api/state", params=PAIR).json()["ticket"]["status"] in ("created", "unknown")
    set_valves(response)
    changed = diagnose(client)
    assert changed["draft"] is None
    assert changed["ticket"] == {"status": "idle", "ticket_id": None, "message": None}
    assert adapter.workflow.get_draft(oil["draft_id"]).state == old_status
    assert len(sent) == (1 if old_status == "sent" else 0)


def test_get_restores_new_diagnosis_and_clears_cached_draft_context(changing_case):
    client, adapter, response, _sent = changing_case
    oil = create_oil(client)

    def remember(state):
        state["messages"].append({"role": "assistant", "summary": "Старый ответ F02", "sections": []})
        state["agent_history"] = [["Старый вопрос", "Старый ответ"]]
    main.store.update((PAIR["vehicle_id"], PAIR["session_id"]), remember)
    set_valves(response)
    adapter.workflow.diagnose(adapter.Pair(**PAIR))
    result = client.get("/api/state", params=PAIR).json()
    assert result["diagnosis"]["anomaly_type"] == "F01"
    assert result["draft"] is None
    assert result["ticket"]["status"] == "idle"
    assert not any(message.get("summary") == "Старый ответ F02" for message in result["messages"])
    assert adapter.workflow.get_draft(oil["draft_id"]).anomaly_type == "F02"
