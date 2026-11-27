"""Acceptance of actual F08 curated evidence through both integrated entry points."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from urllib.error import URLError

import httpx
import pytest
from fastapi.testclient import TestClient

from mechanic import main
from mechanic.case_agent import CaseAgent
from mechanic.config import CORPUS, MOCKS, ROOT
from mechanic.contracts import Diagnosis, Reply
from mechanic.state import Store
from mechanic.team_workflow import TeamWorkflow


FUEL = {"vehicle_id": "M333YK47", "session_id": "S-0005"}
MISMATCH = {"vehicle_id": "Y652CK98", "session_id": "S-0132"}
CHAIN = {"vehicle_id": "Y815BM178", "session_id": "S-0152"}


class DeclineSelection:
    """No model-selected operation: a successful F08 must come from curated data."""

    def ask(self, _prompt, response_schema, _trace):
        fields = response_schema["properties"]
        if "items" in fields:
            return {"items": []}
        if "query" in fields and "action" not in fields:
            return {"query": "Дизельная топливоподача"}
        raise AssertionError(f"Unexpected model request: {list(fields)}")


@pytest.fixture
def integrated(tmp_path, monkeypatch):
    if not (MOCKS / "mock_ml/app.py").is_file():
        pytest.skip("Подготовьте архив заказчика: python -m mechanic.prepare")
    monkeypatch.syspath_prepend(str(MOCKS))
    from mock_ml.app import app as ml
    from mock_toir.app import app as toir

    monkeypatch.setenv("TEAM_DB_PATH", str(tmp_path / "workflow.sqlite3"))
    adapter = TeamWorkflow(ROOT, main.CATALOG_PATH, main.ML_URL, main.TOIR_URL, main.MOCK_TIMEOUT)
    monkeypatch.setattr(main, "team", adapter)
    monkeypatch.setattr(main, "policy_mechanic", adapter.workflow.mechanic)
    monkeypatch.setattr(main, "store", Store(tmp_path / "screen.sqlite3", send_deadline=1))
    monkeypatch.setattr(main, "TEAM_API_URL", "")
    monkeypatch.setattr(main, "TEAM_AGENT_URL", "")
    agent = CaseAgent(CORPUS, tmp_path / "agent-turns.jsonl")
    agent.agent.provider = DeclineSelection()
    monkeypatch.setattr(main, "case_agent", agent)
    sent = []

    with TestClient(ml) as ml_client, TestClient(toir) as toir_client:
        def relay(client, request):
            return client.request(request.method, request.url.path, content=request.content,
                                  headers={"content-type": "application/json"})

        def toir_request(request):
            sent.append(json.loads(request.content))
            return relay(toir_client, request)

        adapter.ml.close()
        adapter.toir.close()
        adapter.ml = httpx.Client(base_url=main.ML_URL,
                                  transport=httpx.MockTransport(lambda request: relay(ml_client, request)))
        adapter.toir = httpx.Client(base_url=main.TOIR_URL, transport=httpx.MockTransport(toir_request))
        adapter.workflow.ml, adapter.workflow.toir = adapter.ml, adapter.toir
        with TestClient(main.app) as client:
            yield client, adapter, agent, sent


def diagnose(client, pair):
    response = client.post("/api/diagnose", json=pair)
    assert response.status_code == 200, response.text
    return response.json()


def request_draft(client, pair, entry):
    path, data = (("/api/draft", pair) if entry == "button"
                  else ("/api/chat", {**pair, "message": "Подготовь заявку"}))
    response = client.post(path, json=data)
    assert response.status_code == 200, response.text
    return response.json()


def submission(pair, state, confirmed):
    return {**pair, "draft_id": state["draft_id"], "revision": state["draft_revision"],
            "edits": {**{key: state["draft"][key]
                         for key in ("conclusion", "operations", "spare_parts")}, "urgency": "medium"},
            "priority_confirmed": confirmed,
            "diagnosis_revision": state["admission"]["diagnosis_revision"],
            "applicability_confirmations": ["initial_start_or_long_storage"] if pair == FUEL else []}


@pytest.mark.parametrize("entry", ["button", "chat"])
def test_actual_f08_uses_backend_evidence_and_preserves_confirmation(integrated, entry):
    client, adapter, _agent, sent = integrated
    initial = diagnose(client, FUEL)
    assert initial["diagnosis"]["anomaly_type"] == "F08"
    assert initial["draft_available"] is True

    state = request_draft(client, FUEL, entry)
    draft = state["draft"]
    assert draft and draft["anomaly_type"] == "F08"
    assert draft["operations"] and draft["spare_parts"] == []
    assert {("236-3902150-Б РЭ", 16)} <= {
        (item["manual"], item["pdf_page"]) for item in draft["references"]
    }
    text = " ".join([draft["conclusion"], *draft["operations"]]).casefold()
    assert "топливопровод" in text
    assert any(word in text for word in ("первичн", "стоянк", "нового двигателя")), text
    assert sent == [], "Preparing a draft must not submit to ТОиР"

    restored = client.get("/api/state", params=FUEL).json()
    assert restored["draft"] == draft
    assert (restored["draft_id"], restored["draft_revision"]) == (state["draft_id"], state["draft_revision"])
    repeated = request_draft(client, FUEL, entry)
    assert (repeated["draft_id"], repeated["draft_revision"]) == (state["draft_id"], state["draft_revision"])
    assert len(adapter.workflow.drafts(adapter.Pair(**FUEL))) == 1

    rejected = client.post("/api/ticket", json=submission(FUEL, state, False))
    assert rejected.status_code == 422
    assert sent == []
    payload = submission(FUEL, state, True)
    missing_condition = client.post("/api/ticket", json={**payload, "applicability_confirmations": []})
    assert missing_condition.status_code == 422, missing_condition.text
    assert missing_condition.json()["error"]["code"] == "applicability_required"
    assert not sent
    # A rejected attempt may save edits and advance the draft revision.
    state = client.get("/api/state", params=FUEL).json()
    payload = submission(FUEL, state, True)
    response = client.post("/api/ticket", json=payload)
    assert response.status_code == 200, response.text
    ticket = response.json()["ticket"]
    assert ticket["status"] == "created" and ticket["ticket_id"]
    assert len(sent) == 1
    assert client.post("/api/ticket", json=payload).json()["ticket"] == ticket
    assert len(sent) == 1
    assert client.get("/api/state", params=FUEL).json()["ticket"] == ticket


@pytest.mark.parametrize("entry", ["button", "chat"])
def test_gasoline_f08_stays_blocked(integrated, entry):
    client, adapter, _agent, sent = integrated
    state = diagnose(client, MISMATCH)
    assert state["diagnosis"]["anomaly_type"] == "F08"
    assert state["draft_available"] is False
    assert "свер" in json.dumps(state["brief"], ensure_ascii=False).casefold()
    path, payload = (("/api/draft", MISMATCH) if entry == "button"
                     else ("/api/chat", {**MISMATCH, "message": "Подготовь заявку"}))
    response = client.post(path, json=payload)
    assert response.status_code == (422 if entry == "button" else 200), response.text
    assert response.json()["admission"]["block_reason"]["code"] == "fault_engine_mismatch"
    assert response.json().get("draft") is None
    assert client.get("/api/state", params=MISMATCH).json()["draft"] is None
    assert adapter.workflow.drafts(adapter.Pair(**MISMATCH)) == []
    assert sent == []


def test_reviewed_chain_draft_reaches_ticket_id(integrated):
    client, _adapter, agent, sent = integrated

    class SelectFirst:
        def ask(self, _prompt, schema, _trace):
            options = schema["properties"]["items"]["items"]["properties"]["source"]["enum"]
            return {"items": [{"field": "checks", "source": options[0]}]}

    agent.agent.provider = SelectFirst()
    assert diagnose(client, CHAIN)["draft_available"] is True
    state = request_draft(client, CHAIN, "button")
    assert state["draft"]["anomaly_type"] == "F14"
    assert state["draft"]["operations"] and state["draft"]["references"]
    assert sent == []
    response = client.post("/api/ticket", json=submission(CHAIN, state, True))
    assert response.status_code == 200, response.text
    assert response.json()["ticket"]["ticket_id"]
    assert len(sent) == 1


def test_existing_f08_missing_evidence_notice_is_recovered_after_upgrade(integrated):
    client, _adapter, _agent, sent = integrated
    diagnose(client, FUEL)
    main.store.update((FUEL["vehicle_id"], FUEL["session_id"]),
                      lambda state: state.update(draft_unavailable_reason="Ранее не найдено источника."))

    restored = client.get("/api/state", params=FUEL).json()
    assert restored["draft_available"] is True
    assert restored["draft_unavailable_reason"] is None
    prepared = request_draft(client, FUEL, "button")
    assert prepared["draft"]["anomaly_type"] == "F08"
    assert any(item["pdf_page"] == 16 for item in prepared["draft"]["references"])
    assert prepared["draft_unavailable_reason"] is None
    assert sent == []


def test_f08_curated_draft_keeps_operation_and_source_in_case_history(integrated):
    client, _adapter, agent, sent = integrated
    state = diagnose(client, FUEL)
    vehicle = main.VEHICLE_BY_ID[FUEL["vehicle_id"]]
    diagnosis = Diagnosis.model_validate(state["diagnosis"])
    _reply, draft, entry, _calls = agent.chat(vehicle, diagnosis, "Подготовь заявку", [])
    assert draft and entry[3], "A follow-up must know which check was just proposed"
    check = entry[3][0]
    assert check["text"] == draft.operations[0]
    source = agent.mechanic.search.fragments[check["fragment_id"]]
    assert source["pdf_page"] == 16
    assert vehicle["engine_model"] in source["applicable_engine_models"]
    assert sent == []


def test_late_chat_draft_keeps_newer_case_facts(integrated, monkeypatch):
    client, adapter, agent, sent = integrated
    initial = diagnose(client, CHAIN)
    proposal = agent.checked_draft(main.VEHICLE_BY_ID[CHAIN["vehicle_id"]],
                                   Diagnosis.model_validate(initial["diagnosis"]))
    assert proposal
    entered, finish = Event(), Event()
    fresh_entry = ["Новые измерения", "Новые факты сохранены", None, [], {"kpa": 90}, None]
    stale_entry = ["Подготовь заявку", "Старый контекст", None, [], None, None]

    def respond(_vehicle, _diagnosis, _state, question):
        if question == "Подготовь заявку":
            entered.set()
            assert finish.wait(10)
            return Reply(summary="Устаревший черновик."), proposal, "team", [], stale_entry
        return Reply(summary="Новые измерения сохранены."), None, "team", [], fresh_entry

    monkeypatch.setattr(main, "response_to_chat", respond)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(client.post, "/api/chat", json={**CHAIN, "message": "Подготовь заявку"})
        try:
            assert entered.wait(5)
            fresh = client.post("/api/chat", json={**CHAIN, "message": "Новые измерения"})
            assert fresh.status_code == 200, fresh.text
        finally:
            finish.set()
        late = pending.result(timeout=10)

    assert late.status_code == 200, late.text
    latest = client.get("/api/state", params=CHAIN).json()
    assert latest["messages"] == fresh.json()["messages"] == late.json()["messages"]
    assert latest["draft"] is None
    assert main.store.read((CHAIN["vehicle_id"], CHAIN["session_id"]))["agent_history"][-1] == fresh_entry
    assert adapter.workflow.drafts(adapter.Pair(**CHAIN)) == []
    assert sent == []


@pytest.mark.parametrize("failure,status", [(TimeoutError("timeout"), 504), (URLError("offline"), 503)])
def test_transport_errors_do_not_become_fallback_drafts(integrated, failure, status):
    client, adapter, agent, sent = integrated
    assert diagnose(client, CHAIN)["draft_available"] is True

    class Failure:
        def ask(self, *_args):
            raise failure

    agent.agent.provider = Failure()
    response = client.post("/api/chat", json={**CHAIN, "message": "Как проверить состояние цепи в приводе ГРМ?"})
    assert response.status_code == status, response.text
    assert client.get("/api/state", params=CHAIN).json()["draft"] is None
    assert adapter.workflow.drafts(adapter.Pair(**CHAIN)) == []
    assert sent == []
