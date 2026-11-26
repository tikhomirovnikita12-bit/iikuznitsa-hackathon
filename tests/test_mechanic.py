from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from mechanic import main
from mechanic.config import MOCKS
from mechanic.state import Store


OIL = {"vehicle_id": "E549YT198", "session_id": "S-0011"}
NORMAL = {"vehicle_id": "E549YT198", "session_id": "S-0010"}
FAILED = {"vehicle_id": "P456AT178", "session_id": "S-0003"}


@pytest.fixture
def stand(tmp_path, monkeypatch):
    if not (MOCKS / "mock_ml" / "app.py").is_file():
        pytest.skip("Подготовьте архив заказчика: python -m mechanic.prepare")
    monkeypatch.syspath_prepend(str(MOCKS))
    from mock_ml.app import app as ml
    from mock_toir.app import app as toir

    monkeypatch.setattr(main, "store", Store(tmp_path / "state.sqlite3", send_deadline=1))
    monkeypatch.setattr(main, "team", None)
    calls = []
    with TestClient(ml) as ml_client, TestClient(toir) as toir_client, TestClient(main.app) as client:
        def upstream(url, payload, _timeout):
            if url == f"{main.ML_URL}/diagnose":
                return ml_client.post("/diagnose", json=payload)
            assert url == f"{main.TOIR_URL}/tickets"
            calls.append(payload)
            return toir_client.post("/tickets", json=payload)

        monkeypatch.setattr(main, "post", upstream)
        yield client, calls


def state(client, pair=OIL):
    return client.get("/api/state", params=pair).json()


def diagnose(client, pair=OIL):
    response = client.post("/api/diagnose", json=pair)
    assert response.status_code == 200, response.text
    return response.json()


def prepare(client, pair=OIL):
    diagnose(client, pair)
    result = client.post("/api/chat", json={**pair, "message": "Подготовь заявку"}).json()
    draft = result["draft"]
    assert draft
    edits = {key: draft[key] for key in ("conclusion", "operations", "urgency", "spare_parts")}
    return {**pair, "edits": edits, "priority_confirmed": True}


def test_actual_mock_outcomes_and_pair_isolation(stand):
    client, _ = stand
    result = diagnose(client)
    assert result["diagnosis"]["anomaly_type"] == "F02"
    assert {source["pdf_page"] for source in result["brief"]["references"]} == {19, 53}
    assert state(client, NORMAL)["diagnosis"] is None
    assert diagnose(client, NORMAL)["diagnosis"]["status"] == "normal"
    reply = client.post("/api/chat", json={**NORMAL, "message": "Что проверить сначала?"}).json()
    assert "S-0010" in reply["messages"][-1]["summary"]
    assert not state(client)["messages"]
    failed = diagnose(client, FAILED)["diagnosis"]
    assert failed["status"] == "check_failed"
    assert failed["confidence"] is None and failed["anomaly_score"] is None
    wrong = client.post("/api/diagnose", json={**FAILED, "vehicle_id": OIL["vehicle_id"]})
    assert wrong.status_code == 404 and wrong.json()["error"]["code"] == "invalid_pair"


def test_mock_payload_receipt_and_concurrent_duplicate(stand, monkeypatch):
    client, calls = stand
    request = prepare(client)
    waiting, finish = Event(), Event()
    real_post = main.post

    def slow_toir(url, payload, timeout):
        waiting.set()
        assert finish.wait(5)
        return real_post(url, payload, timeout)

    monkeypatch.setattr(main, "post", slow_toir)
    request["edits"]["urgency"] = "medium"
    rejected = client.post("/api/ticket", json={**request, "priority_confirmed": False})
    assert rejected.status_code == 422
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(client.post, "/api/ticket", json=request)
        try:
            assert waiting.wait(5)
            assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "sending"
        finally:
            finish.set()
        ticket = first.result(timeout=5).json()["ticket"]
    assert ticket["status"] == "created" and ticket["ticket_id"].startswith("TKT-")
    assert len(calls) == 1
    assert calls[0]["anomaly_type"] == "F02"
    assert calls[0]["spare_parts"] == [] and calls[0]["urgency"] == "medium"
    assert all(set(ref) == {"manual", "pdf_page"} for ref in calls[0]["references"])
    assert state(client)["ticket"] == ticket
    assert client.post("/api/ticket", json=request).json()["ticket"] == ticket
    assert len(calls) == 1


def test_definite_error_preserves_draft_until_explicit_retry(stand, monkeypatch):
    client, calls = stand
    request = prepare(client)
    request["edits"]["conclusion"] += " Замер запланирован."
    real_post = main.post
    monkeypatch.setattr(main, "post", lambda *_: httpx.Response(422, json={}))
    result = client.post("/api/ticket", json=request).json()
    assert result["ticket"]["status"] == "error"
    assert state(client)["draft"]["conclusion"] == request["edits"]["conclusion"]
    assert not calls
    monkeypatch.setattr(main, "post", real_post)
    assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "created"
    assert len(calls) == 1


def test_timeout_after_dispatch_locks_retry_across_store_restart(stand, monkeypatch):
    client, calls = stand
    request = prepare(client)

    def timeout(_url, payload, _timeout):
        calls.append(payload)
        raise httpx.ReadTimeout("Ответ потерян после отправки")

    monkeypatch.setattr(main, "post", timeout)
    assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "unknown"
    monkeypatch.setattr(main, "store", Store(main.store.path, send_deadline=1))
    assert state(client)["ticket"]["status"] == "unknown"
    assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "unknown"
    assert len(calls) == 1


@pytest.mark.parametrize(("failure", "status", "code"), [
    (httpx.ReadTimeout("test"), 504, "ml_timeout"),
    (httpx.ConnectError("test"), 503, "ml_unavailable"),
])
def test_diagnosis_transport_errors_are_distinct(stand, monkeypatch, failure, status, code):
    client, _ = stand

    def raise_failure(*_):
        raise failure

    monkeypatch.setattr(main, "post", raise_failure)
    response = client.post("/api/diagnose", json=OIL)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert state(client)["diagnosis"] is None


def test_engine_mismatch_blocks_draft(stand):
    client, _ = stand
    pair = {"vehicle_id": "Y652CK98", "session_id": "S-0132"}
    result = diagnose(client, pair)
    assert result["diagnosis"]["anomaly_type"] == "F08"
    assert "бензиновый" in result["brief"]["rationale"]
    payload = {**pair, "message": "Создай заявку"}
    assert client.post("/api/chat", json=payload).json()["draft"] is None
