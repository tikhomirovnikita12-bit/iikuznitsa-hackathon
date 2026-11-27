"""Server-side admission is repeated at delivery; rejected cases never call TOiR."""
import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.app import ROOT, Settings, create_app


WARNING = {"vehicle_id": "O658MX98", "session_id": "S-0063"}
LOW = {"vehicle_id": "M739BA198", "session_id": "S-0103"}
FAILED = {"vehicle_id": "P456AT178", "session_id": "S-0003"}
MISMATCH = {"vehicle_id": "Y652CK98", "session_id": "S-0132"}
UNSUPPORTED = {"vehicle_id": "O710HH78", "session_id": "S-0014"}
FUEL = {"vehicle_id": "M333YK47", "session_id": "S-0005"}
CHAIN = {"vehicle_id": "Y815BM178", "session_id": "S-0152"}


@pytest.fixture
def stand(tmp_path):
    vehicles = json.loads((ROOT / "data/vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    sessions = {(vehicle["vehicle_id"], session["session_id"]): session
                for vehicle in vehicles for session in vehicle["sessions"]}
    overrides, tickets = {}, []

    def ml(request):
        pair = json.loads(request.content)
        key = pair["vehicle_id"], pair["session_id"]
        return httpx.Response(200, json={**sessions[key], **overrides.get(key, {})})

    def toir(request):
        tickets.append(json.loads(request.content))
        return httpx.Response(201, json={"ticket_id": f"TKT-{len(tickets):04d}", "status": "created"})

    app = create_app(Settings(database=tmp_path / "state.sqlite3", ollama_model=""),
                     ml_transport=httpx.MockTransport(ml), toir_transport=httpx.MockTransport(toir))
    with TestClient(app) as api:
        yield api, overrides, tickets


def diagnose(api, pair):
    response = api.post("/api/diagnose", json=pair)
    assert response.status_code == 200, response.text
    return response.json()


def draft(api, pair):
    diagnose(api, pair)
    response = api.post("/api/drafts", json=pair)
    assert response.status_code == 200, response.text
    return response.json()


def confirmation(value, **updates):
    required = {condition["id"] for operation in value["admission"]["approved_operations"]
                for condition in operation.get("conditions", []) if condition.get("requires_confirmation")}
    return {"revision": value["revision"], "urgency": "medium", "confirmed": True,
            "diagnosis_revision": value["admission"]["diagnosis_revision"],
            "applicability_confirmations": sorted(required), **updates}


@pytest.mark.parametrize(("pair", "reason", "warning"), [
    (LOW, "confidence_too_low", True), (FAILED, "recording_invalid", False),
    (MISMATCH, "fault_engine_mismatch", False), (UNSUPPORTED, "procedure_unavailable", False),
])
def test_blocked_diagnostics_never_create_or_deliver_ticket(stand, pair, reason, warning):
    api, _, tickets = stand
    result = diagnose(api, pair)
    assert result["admission"]["block_reason"]["code"] == reason
    assert result["admission"]["warning_ack_required"] is warning
    assert not result["admission"]["can_prepare"]
    response = api.post("/api/drafts", json=pair)
    assert response.status_code == 422
    assert response.json()["admission"]["block_reason"]["next_action"]
    assert tickets == []
    if pair == FAILED:
        assert result["assessment"]["conclusion"] is None


def test_warning_and_priority_are_separate_and_idempotent(stand):
    api, _, tickets = stand
    value = draft(api, WARNING)
    assert value["suggested_urgency"] == "medium" and value["confirmed_urgency"] is None
    path = f"/api/drafts/{value['draft_id']}/confirm"
    request = confirmation(value, urgency="high")
    denied = api.post(path, json=request)
    assert denied.status_code == 422 and denied.json()["error"]["code"] == "warning_ack_required"
    assert tickets == []
    request["warning_acknowledged"] = True
    sent = api.post(path, json=request)
    assert sent.status_code == 200, sent.text
    assert sent.json()["ticket_id"] == "TKT-0001"
    assert sent.json()["suggested_urgency"] == "medium" and sent.json()["confirmed_urgency"] == "high"
    assert api.post(path, json=request).json()["ticket_id"] == "TKT-0001"
    assert len(tickets) == 1
    assert set(tickets[0]) == {"vehicle_id", "session_id", "anomaly_type", "conclusion", "urgency", "operations", "spare_parts", "references"}


def test_confidence_boundary_and_changed_diagnosis_reset_confirmation(stand):
    api, overrides, tickets = stand
    overrides[(WARNING["vehicle_id"], WARNING["session_id"])] = {"confidence": 0.50}
    value = draft(api, WARNING)
    assert value["admission"]["can_prepare"]
    old = confirmation(value, warning_acknowledged=True)
    overrides[(WARNING["vehicle_id"], WARNING["session_id"])] = {"confidence": 0.54}
    diagnose(api, WARNING)
    path = f"/api/drafts/{value['draft_id']}"
    stale = api.post(path + "/confirm", json=old)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_draft"
    latest = api.get(path).json()
    assert latest["revision"] > value["revision"]
    assert latest["diagnosis_revision"] != value["diagnosis_revision"]
    assert latest["applicability_confirmations"] == []
    assert api.post(path + "/confirm", json=confirmation(latest)).status_code == 422
    assert tickets == []
    sent = api.post(path + "/confirm", json=confirmation(latest, warning_acknowledged=True))
    assert sent.status_code == 200 and len(tickets) == 1


def test_same_engine_other_node_and_unreviewed_work_are_rejected(stand):
    api, _, tickets = stand
    value = draft(api, WARNING)
    path = f"/api/drafts/{value['draft_id']}"
    wrong_work = api.patch(path, json={"revision": value["revision"], "operations": ["Заменить топливный насос."]})
    assert wrong_work.status_code == 422
    assert wrong_work.json()["error"]["code"] == "operation_not_approved"
    operation = value["admission"]["approved_operations"][0]
    wrong_page = api.patch(path, json={"revision": value["revision"], "references": [{"manual": operation["manual"], "pdf_page": 49}]})
    assert wrong_page.status_code == 422
    assert wrong_page.json()["error"]["code"] == "operation_source_mismatch"
    assert tickets == []


def test_f08_requires_known_startup_condition(stand):
    api, _, tickets = stand
    value = draft(api, FUEL)
    path = f"/api/drafts/{value['draft_id']}/confirm"
    request = confirmation(value, applicability_confirmations=[])
    response = api.post(path, json=request)
    assert response.status_code == 422 and response.json()["error"]["code"] == "applicability_required"
    assert tickets == []
    request = confirmation(value)
    assert request["applicability_confirmations"]
    assert api.post(path, json=request).status_code == 200
    assert len(tickets) == 1


def test_unknown_delivery_is_not_retried(stand):
    api, _, tickets = stand
    value = draft(api, CHAIN)
    workflow = api.app.state.workflow

    def timeout(request):
        tickets.append(json.loads(request.content))
        raise httpx.ReadTimeout("response lost", request=request)

    workflow.toir.close()
    workflow.toir = httpx.Client(base_url="http://toir", transport=httpx.MockTransport(timeout))
    path = f"/api/drafts/{value['draft_id']}/confirm"
    request = confirmation(value, warning_acknowledged=True)
    response = api.post(path, json=request)
    assert response.status_code == 504 and response.json()["draft"]["state"] == "delivery_unknown"
    assert api.post(path, json=request).status_code == 409
    assert len(tickets) == 1
    workflow.toir.close()


def test_confirmation_rechecks_persisted_operation_source(stand):
    api, _, tickets = stand
    value = draft(api, WARNING)
    workflow = api.app.state.workflow
    with workflow.store.connection(write=True) as conn:
        row = workflow.row(conn, value["draft_id"])
        fields = json.loads(row["fields"])
        fields["references"] = [{"manual": fields["references"][0]["manual"], "pdf_page": 49}]
        conn.execute("UPDATE drafts SET fields=? WHERE draft_id=?", (json.dumps(fields), value["draft_id"]))
    response = api.post(f"/api/drafts/{value['draft_id']}/confirm", json=confirmation(value, warning_acknowledged=True))
    assert response.status_code == 422 and response.json()["error"]["code"] == "operation_source_mismatch"
    assert tickets == []


def test_legacy_allowed_draft_refreshes_reviewed_work_preserving_edits(stand):
    api, _, tickets = stand
    value = draft(api, CHAIN)
    workflow = api.app.state.workflow
    with workflow.store.connection(write=True) as conn:
        row = workflow.row(conn, value["draft_id"])
        fields = json.loads(row["fields"])
        fields.update(conclusion="Заключение механика сохранено.", urgency="low", operations=["Старая операция."], references=[])
        conn.execute("UPDATE drafts SET fields=?, diagnosis_revision=NULL, suggested_urgency=NULL WHERE draft_id=?",
                     (json.dumps(fields), value["draft_id"]))
    refreshed = api.get(f"/api/drafts/{value['draft_id']}").json()
    assert refreshed["revision"] == value["revision"] + 1
    assert refreshed["conclusion"] == "Заключение механика сохранено." and refreshed["urgency"] == "low"
    assert refreshed["operations"] == [row["operation"] for row in refreshed["admission"]["approved_operations"]]
    assert refreshed["references"] and refreshed["diagnosis_revision"]
    assert refreshed["confirmed_urgency"] is None and refreshed["applicability_confirmations"] == []
    assert tickets == []


def test_shared_policy_rejects_warning_ack_without_diagnosis_fingerprint(stand):
    from backend.admission import evaluate_admission

    api, _, tickets = stand
    value = draft(api, WARNING)
    workflow = api.app.state.workflow
    pair = workflow.catalog.vehicle(WARNING["vehicle_id"])
    diagnosis = diagnose(api, WARNING)["diagnosis"]
    fields = {key: value[key] for key in ("conclusion", "urgency", "operations", "spare_parts", "references")}
    decision = evaluate_admission(pair, diagnosis, fields, mechanic=workflow.mechanic, warning_acknowledged=True)
    assert decision["can_prepare"] and not decision["can_submit"]
    assert decision["block_reason"]["code"] == "diagnosis_changed"
    assert tickets == []


def test_missing_original_pdf_blocks_preparation_and_delivery(stand, tmp_path):
    api, _, tickets = stand
    mechanic = api.app.state.workflow.mechanic
    mechanic.pdf_paths = {key: tmp_path / "not-provided" / path.name for key, path in mechanic.pdf_paths.items()}
    result = diagnose(api, CHAIN)
    assert not result["admission"]["can_prepare"]
    assert result["admission"]["block_reason"]["code"] == "procedure_unavailable"
    response = api.post("/api/drafts", json=CHAIN)
    assert response.status_code == 422
    assert tickets == []
