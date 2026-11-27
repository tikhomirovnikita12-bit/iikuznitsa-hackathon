from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event

import httpx
import pytest
from fastapi.testclient import TestClient

from mechanic import main
from mechanic.case_agent import CaseAgent
from mechanic.config import MOCKS
from mechanic.state import Store
from manual_search.corpus import verified_fragments


OIL = {"vehicle_id": "E549YT198", "session_id": "S-0011"}
CHAIN = {"vehicle_id": "Y815BM178", "session_id": "S-0152"}
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


def verdict(result):
    messages = [message for message in result["messages"] if message["role"] == "diagnosis"]
    message, = messages
    assert message["status"] == result["diagnosis"]["status"]
    assert message["description"] == result["diagnosis"]["description"]
    assert message["summary"] and message["next_step"]
    return message


def prepare(client, pair=CHAIN):
    diagnose(client, pair)
    result = client.post("/api/chat", json={**pair, "message": "Подготовь заявку"}).json()
    draft = result["draft"]
    assert draft
    edits = {key: draft[key] for key in ("conclusion", "operations", "urgency", "spare_parts")}
    assert result["draft_id"] and result["draft_revision"] >= 1
    admission = result["admission"]
    return {**pair, "draft_id": result["draft_id"], "revision": result["draft_revision"],
            "edits": edits, "priority_confirmed": True,
            "diagnosis_revision": admission["diagnosis_revision"],
            "warning_acknowledged": admission["warning_ack_required"],
            "applicability_confirmations": [condition["id"] for operation in admission["approved_operations"]
                                            for condition in operation["conditions"] if condition["requires_confirmation"]]}


def test_q05_on_demo_screen_route(stand, monkeypatch):
    client, _ = stand
    monkeypatch.setattr(main, "case_agent", None)
    pair = {"vehicle_id": "A464EP47", "session_id": "S-0096"}
    diagnose(client, pair)
    response = client.post("/api/chat", json={
        **pair, "message": "Какое давление масла на прогретом двигателе?",
    })
    assert response.status_code == 200, response.text
    nominal, idle = [section for section in response.json()["messages"][-1]["sections"]
                     if section["kind"] == "checks"]
    assert "400–635 кПа" in nominal["items"][0] and "80–100 °C" in nominal["items"][0]
    assert nominal["references"][0]["pdf_page"] == 13
    assert "не менее 100 кПа" in idle["items"][0]
    assert idle["references"][0]["pdf_page"] == 49


def test_vehicle_history_remembers_ticket(stand):
    client, _ = stand
    request = prepare(client)
    created = client.post("/api/ticket", json=request).json()["ticket"]
    data = client.get("/api/history", params={"vehicle_id": CHAIN["vehicle_id"]}).json()
    assert data["vehicle_id"] == CHAIN["vehicle_id"]
    assert len(data["sessions"]) == 4
    row = next(item for item in data["sessions"] if item["session_id"] == CHAIN["session_id"])
    assert row["anomaly_type"] == "F14"
    assert row["ticket"] == {"status": "created", "ticket_id": created["ticket_id"]}
    missing = client.get("/api/history", params={"vehicle_id": "нет такого"})
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "unknown_vehicle"


def test_fleet_report_categories_and_history(stand):
    client, _ = stand
    data = client.get("/api/fleet").json()
    assert data["summary"]["total"] == 50
    assert sum(data["summary"][key] for key in main.FLEET_CATEGORIES) == 50
    row = next(item for item in data["vehicles"] if item["vehicle_id"] == "B418HK98")
    assert row["category"] == "quarantine"
    assert row["latest"]["session_id"] == "S-0009"
    assert 1 <= len(row["history"]) <= 5
    assert all(set(step) == {"session_id", "checked_at", "status", "anomaly_score"}
               for step in row["history"])


def test_fleet_reads_team_state_without_local_store(stand, monkeypatch):
    """При TEAM_API_URL локальный Store отсутствует: заявку отдаёт командный адаптер."""
    client, _ = stand

    class Team:
        def close(self):
            pass

        def populate(self, state, pair, *, restore_diagnosis=False):
            state["ticket"] = {"status": "created", "ticket_id": "TKT-7777", "message": None}
            state["draft"] = {"urgency": "high"}
            return state

    monkeypatch.setattr(main, "store", None)
    monkeypatch.setattr(main, "team", Team())
    row = next(item for item in client.get("/api/fleet").json()["vehicles"]
               if item["vehicle_id"] == "T792HO98")
    assert row["ticket"] == {"status": "created", "ticket_id": "TKT-7777"}
    assert row["urgency"] == "high"
    assert row["category"] == "quarantine"


def test_fleet_reflects_sent_ticket(stand):
    client, _ = stand
    pair = {"vehicle_id": "O658MX98", "session_id": "S-0063"}
    request = prepare(client, pair)
    created = client.post("/api/ticket", json=request).json()["ticket"]
    assert created["status"] == "created"
    row = next(item for item in client.get("/api/fleet").json()["vehicles"]
               if item["vehicle_id"] == pair["vehicle_id"])
    assert row["category"] == "quarantine"
    assert row["ticket"] == {"status": "created", "ticket_id": created["ticket_id"]}
    assert row["urgency"] == request["edits"]["urgency"]


def test_actual_mock_outcomes_and_pair_isolation(stand):
    client, _ = stand
    result = diagnose(client)
    assert result["diagnosis"]["anomaly_type"] == "F02"
    assert {source["pdf_page"] for source in result["brief"]["references"]} == {19, 53}
    assert {source["pdf_page"] for source in verdict(result)["references"]} == {19, 53}
    assert state(client)["messages"] == result["messages"]
    assert state(client, NORMAL)["diagnosis"] is None
    normal = diagnose(client, NORMAL)
    assert normal["diagnosis"]["status"] == "normal"
    assert "Значимых отклонений не обнаружено" in verdict(normal)["summary"]
    assert verdict(state(client, NORMAL)) == verdict(normal)
    reply = client.post("/api/chat", json={**NORMAL, "message": "Что проверить сначала?"}).json()
    assert "S-0010" in reply["messages"][-1]["summary"]
    assert state(client)["messages"] == result["messages"]
    failed_result = diagnose(client, FAILED)
    failed = failed_result["diagnosis"]
    assert failed["status"] == "check_failed"
    assert failed["confidence"] is None and failed["anomaly_score"] is None
    assert "перегрузка микрофона" in verdict(failed_result)["description"]
    assert "Повторите запись" in verdict(failed_result)["next_step"]
    assert verdict(state(client, FAILED)) == verdict(failed_result)
    assert verdict(diagnose(client, NORMAL)) == verdict(normal)
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
    assert calls[0]["anomaly_type"] == "F14"
    assert calls[0]["spare_parts"] == [] and calls[0]["urgency"] == "medium"
    assert all(set(ref) == {"manual", "pdf_page"} for ref in calls[0]["references"])
    assert state(client, CHAIN)["ticket"] == ticket
    assert client.post("/api/ticket", json=request).json()["ticket"] == ticket
    assert len(calls) == 1


def test_case_agent_preserves_measurements_without_inventing_draft_procedure(stand, monkeypatch, tmp_path):
    client, calls = stand
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"fragments": verified_fragments()}), encoding="utf-8")
    agent = CaseAgent(corpus, tmp_path / "turns.jsonl")

    class SelectPassage:
        def ask(self, _prompt, response_schema, _trace):
            sources = response_schema["properties"]["items"]["items"]["properties"]["source"]["enum"]
            source = next(item for item in sources if "-p053-" in item)
            return {"items": [{"field": "checks", "source": source}]}

    monkeypatch.setattr(agent.agent, "provider", SelectPassage())
    monkeypatch.setattr(main, "case_agent", agent)
    assert client.get("/api/catalog").json()["mode"] == "hybrid"
    diagnose(client)

    def ask(message):
        response = client.post("/api/chat", json={**OIL, "message": message})
        assert response.status_code == 200, response.text
        return response.json()

    first = ask("Можно ли перегнать машину?")
    assert first["messages"][-1]["origin"] == "team"
    assert first["messages"][-1]["analysis"]["references"][0]["pdf_page"] == 19
    assert 19 in {ref["pdf_page"] for section in first["messages"][-1]["sections"] for ref in section["references"]}
    measurement = ask("350")
    assert any("350" in text for section in measurement["messages"][-1]["sections"] for text in section["items"])
    assert "350" in measurement["messages"][-1]["analysis"]["clarifying_question"]
    assert state(client)["messages"] == measurement["messages"]
    assert "agent_history" not in state(client)
    ask("да, кПа")
    result = ask("Двигатель прогрет, 2100 об/мин, без допуска после длительной эксплуатации")
    assert any("350 кПа — ниже" in text for section in result["messages"][-1]["sections"] for text in section["items"])
    assert "350 кПа — ниже" in result["messages"][-1]["analysis"]["checks"][0]["text"]

    result = ask("Подготовь заявку на дефектовку")
    draft = result["draft"]
    assert draft["operations"] == [
        "Проверить достаточность уровня масла в картере и уровня охлаждающей жидкости."
    ]
    assert [source["pdf_page"] for source in draft["references"]] == [53]
    assert not calls, "A reviewed draft still requires confirmation"
    payload = {**OIL, "draft_id": result["draft_id"], "revision": result["draft_revision"],
               "diagnosis_revision": result["admission"]["diagnosis_revision"],
               "edits": {key: draft[key] for key in ("conclusion", "operations", "urgency", "spare_parts")},
               "priority_confirmed": True}
    receipt = client.post("/api/ticket", json=payload)
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["ticket"]["ticket_id"].startswith("TKT-")
    assert client.post("/api/ticket", json=payload).json()["ticket"] == receipt.json()["ticket"]
    assert len(calls) == 1


def test_definite_error_preserves_draft_until_explicit_retry(stand, monkeypatch):
    client, calls = stand
    request = prepare(client)
    request["edits"]["conclusion"] += " Замер запланирован."
    real_post = main.post
    monkeypatch.setattr(main, "post", lambda *_: httpx.Response(422, json={}))
    result = client.post("/api/ticket", json=request).json()
    assert result["ticket"]["status"] == "error"
    assert state(client, CHAIN)["draft"]["conclusion"] == request["edits"]["conclusion"]
    assert not calls
    monkeypatch.setattr(main, "post", real_post)
    request["revision"] = state(client, CHAIN)["draft_revision"]
    assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "created"
    assert len(calls) == 1


def test_stale_update_and_submit_return_latest_draft(stand):
    client, calls = stand
    original = prepare(client)
    edits = {**original["edits"], "conclusion": original["edits"]["conclusion"] + " Пометка второй вкладки."}
    saved = client.put("/api/draft", json={**original, "edits": edits})
    assert saved.status_code == 200, saved.text
    latest = saved.json()
    assert latest["draft_id"] == original["draft_id"]
    assert latest["draft_revision"] > original["revision"]
    assert latest["draft"]["conclusion"] == edits["conclusion"]

    stale = {**original, "edits": {**original["edits"], "conclusion": "Несохранённая пометка."}}
    for changed in (client.put("/api/draft", json=stale), client.post("/api/ticket", json=stale)):
        assert changed.status_code == 409, changed.text
        conflict = changed.json()
        assert conflict["error"]["code"] == "stale_draft"
        assert conflict["state"]["draft_id"] == latest["draft_id"]
        assert conflict["state"]["draft_revision"] == latest["draft_revision"]
        assert conflict["state"]["draft"]["conclusion"] == edits["conclusion"]
    assert state(client, CHAIN)["draft"]["conclusion"] == edits["conclusion"]
    assert not calls


def test_timeout_after_dispatch_locks_retry_across_store_restart(stand, monkeypatch):
    client, calls = stand
    request = prepare(client)

    def timeout(_url, payload, _timeout):
        calls.append(payload)
        raise httpx.ReadTimeout("Ответ потерян после отправки")

    monkeypatch.setattr(main, "post", timeout)
    assert client.post("/api/ticket", json=request).json()["ticket"]["status"] == "unknown"
    monkeypatch.setattr(main, "store", Store(main.store.path, send_deadline=1))
    assert state(client, CHAIN)["ticket"]["status"] == "unknown"
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


def test_injected_instruction_in_diagnosis_does_not_create_draft(stand):
    """Описание проверки — контекст, а не команда агенту (сессия S-0195)."""
    client, calls = stand
    pair = {"vehicle_id": "E622KM47", "session_id": "S-0195"}
    result = diagnose(client, pair)
    assert result["diagnosis"]["anomaly_type"] == "F12"
    assert result["brief"]["references"] == []
    assert result["draft"] is None
    assert result["draft_available"] is False

    payload = {**pair, "message": "Создай заявку с приоритетом low без ссылок"}
    reply = client.post("/api/chat", json=payload).json()
    assert reply["draft"] is None
    assert reply["admission"]["block_reason"]["code"] == "procedure_unavailable"
    assert "проверенная применимая процедура" in reply["messages"][-1]["summary"]
    assert not calls
