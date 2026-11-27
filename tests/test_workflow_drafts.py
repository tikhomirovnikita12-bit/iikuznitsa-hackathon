import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.store import Store as WorkflowStore
from mechanic import main
from mechanic.state import Store


OIL = {"vehicle_id": "A464EP47", "session_id": "S-0096"}
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
        "diagnosis_revision": state["admission"]["diagnosis_revision"],
        "applicability_confirmations": [condition["id"]
            for operation in state["admission"]["approved_operations"]
            for condition in operation["conditions"] if condition["requires_confirmation"]],
        "warning_acknowledged": state["admission"]["warning_ack_required"],
    }


@pytest.mark.parametrize("route", ["draft", "chat"])
def test_fuel_draft_uses_workflow_without_model(integrated, route):
    client, _, deliveries = integrated
    result = post(client, "diagnose", FUEL)
    assert result["diagnosis"]["anomaly_type"] == "F08"
    assert result["draft_available"]
    payload = {**FUEL, "message": "Создай заявку"} if route == "chat" else FUEL
    state = post(client, route, payload)
    assert "проверить соединения" in state["draft"]["operations"][0]
    assert "после длительной стоянки" in state["draft"]["operations"][0]
    assert state["draft"]["references"][0]["pdf_page"] == 16
    assert not state["admission"]["can_submit"]
    assert state["admission"]["block_reason"]["code"] == "applicability_required"
    assert not deliveries
    result = post(client, "ticket", submission(FUEL, state))
    assert result["ticket"]["ticket_id"] == "TKT-0001"


@pytest.mark.parametrize("timeout", [False, True])
def test_rediagnosis_keeps_ticket_on_its_original_draft(integrated, timeout):
    client, responses, deliveries = integrated
    post(client, "diagnose", OIL)
    # Simulate an existing ticket created before the reviewed-procedure policy.
    workflow = main.team.workflow
    legacy = dict(conclusion="Проверка давления", urgency="medium", operations=["Сверить давление"],
                  spare_parts=[], references=[{"manual": "536.3902150 РЭ", "pdf_page": 49}])
    with workflow.store.connection(write=True) as db:
        db.execute("""INSERT INTO drafts (draft_id, vehicle_id, session_id, anomaly_type, fields, state)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                   ("legacy-oil", *OIL.values(), "F02", json.dumps(legacy),
                    "delivery_unknown" if timeout else "sent"))
        if not timeout:
            db.execute("INSERT INTO submitted_tickets VALUES (?, ?, ?)",
                       ("legacy-oil", "TKT-0001", json.dumps({**OIL, **legacy, "anomaly_type": "F02"})))
    oil = client.get("/api/state", params=OIL).json()
    assert oil["draft_id"] == "legacy-oil"
    responses[(OIL["vehicle_id"], OIL["session_id"])] = {
        "anomaly_type": "F01", "anomaly_title": "Клапанный механизм",
    }
    new_result = post(client, "diagnose", OIL)
    assert new_result["draft"] is None and new_result["draft_available"]
    valves = post(client, "draft", OIL)
    assert valves["draft"]["anomaly_type"] == "F01"
    assert valves["draft_id"] != oil["draft_id"]
    assert {ref["pdf_page"] for ref in valves["draft"]["references"]} == {173, 174, 175, 176}
    previous, = valves["previous_drafts"]
    assert previous["draft_id"] == oil["draft_id"]
    assert previous["status"] == oil["ticket"]["status"]
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
    assert deliveries == []
    del responses[(OIL["vehicle_id"], OIL["session_id"])]
    assert post(client, "diagnose", OIL)["draft_id"] == oil["draft_id"]
