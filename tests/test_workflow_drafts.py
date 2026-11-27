import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.store import Store as WorkflowStore
from mechanic import main
from mechanic.state import Store


OIL = {"vehicle_id": "E549YT198", "session_id": "S-0011"}
FUEL = {"vehicle_id": "M333YK47", "session_id": "S-0005"}
EDITABLE = ("conclusion", "urgency", "operations", "spare_parts")


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    workflow = main.team.workflow
    monkeypatch.setattr(main, "case_agent", None)
    monkeypatch.setattr(main, "store", Store(tmp_path / "screen.sqlite3", send_deadline=10))
    monkeypatch.setattr(workflow, "store", WorkflowStore(tmp_path / "workflow.sqlite3"))
    responses, deliveries = {}, []

    def upstream(request):
        body = json.loads(request.content)
        if request.url.path == "/tickets":
            deliveries.append(body)
            if responses.get("timeout"):
                raise httpx.ReadTimeout("Ответ ТОиР потерян", request=request)
            return httpx.Response(201, json={"status": "created", "ticket_id": f"TKT-{len(deliveries):04}"})
        key = (body["vehicle_id"], body["session_id"])
        vehicle = main.VEHICLE_BY_ID[key[0]]
        session = next(item for item in vehicle["sessions"] if item["session_id"] == key[1])
        diagnosis = {field: session[field] for field in main.Diagnosis.model_fields}
        diagnosis.update(responses.get(key, {}))
        return httpx.Response(200, json=diagnosis)

    with httpx.Client(base_url="http://mock", transport=httpx.MockTransport(upstream)) as mocks:
        monkeypatch.setattr(workflow, "ml", mocks)
        monkeypatch.setattr(workflow, "toir", mocks)
        client = TestClient(main.app)
        try:
            yield client, responses, deliveries
        finally:
            client.close()


def post(client, route, payload):
    response = client.post("/api/" + route, json=payload)
    assert response.is_success, response.text
    return response.json()


def submission(pair, state):
    return {
        **pair, "draft_id": state["draft_id"], "revision": state["draft_revision"],
        "edits": {**{key: state["draft"][key] for key in EDITABLE}, "urgency": "medium"},
        "priority_confirmed": True,
    }


@pytest.mark.parametrize("route", ["draft", "chat"])
def test_fuel_draft_uses_workflow_without_model(integrated, route):
    client, _, deliveries = integrated
    result = post(client, "diagnose", FUEL)
    assert result["diagnosis"]["anomaly_type"] == "F08"
    assert result["draft_available"]
    payload = {**FUEL, "message": "Создай заявку"} if route == "chat" else FUEL
    state = post(client, route, payload)
    assert state["draft"]["operations"] == ["Проверить соединения и надёжность крепления топливопроводов."]
    assert state["draft"]["references"][0]["pdf_page"] == 16
    assert state["draft"]["urgency"] is None
    assert not deliveries
    result = post(client, "ticket", submission(FUEL, state))
    assert result["ticket"]["ticket_id"] == "TKT-0001"


@pytest.mark.parametrize("timeout", [False, True])
def test_rediagnosis_keeps_ticket_on_its_original_draft(integrated, timeout):
    client, responses, deliveries = integrated
    post(client, "diagnose", OIL)
    oil = post(client, "draft", OIL)
    responses["timeout"] = timeout
    after_send = post(client, "ticket", submission(OIL, oil))
    responses[(OIL["vehicle_id"], OIL["session_id"])] = {
        "anomaly_type": "F01", "anomaly_title": "Клапанный механизм",
    }
    new_result = post(client, "diagnose", OIL)
    assert new_result["draft"] is None and new_result["draft_available"]
    valves = post(client, "draft", OIL)
    assert valves["draft"]["anomaly_type"] == "F01"
    assert valves["draft_id"] != oil["draft_id"]
    assert valves["draft"]["references"][0]["pdf_page"] == 31
    previous, = valves["previous_drafts"]
    assert previous["draft_id"] == oil["draft_id"]
    assert previous["status"] == after_send["ticket"]["status"]
    stale = client.put("/api/draft", json=submission(OIL, oil))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_draft"
    if timeout:
        valves = post(client, f"drafts/{oil['draft_id']}/reconcile", {
            **OIL, "revision": previous["revision"], "ticket_id": "TKT-0042", "verified": True,
        })
        assert valves["draft"]["anomaly_type"] == "F01"
        assert valves["previous_drafts"][0]["ticket_id"] == "TKT-0042"
    else:
        assert previous["ticket_id"] == "TKT-0001"
    assert len(deliveries) == 1
    del responses[(OIL["vehicle_id"], OIL["session_id"])]
    assert post(client, "diagnose", OIL)["draft_id"] == oil["draft_id"]
