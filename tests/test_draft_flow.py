"""The integrated screen must agree with the real draft workflow about readiness.

The F08 success fixture below is deliberately synthetic: it verifies transport and
confirmation when evidence exists, not real F08 knowledge coverage.
"""

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
from mechanic.contracts import Diagnosis, Reply, Section
from mechanic.state import Store
from mechanic.team_workflow import TeamWorkflow
from manual_search.corpus import verified_fragments


FUEL = {"vehicle_id": "M333YK47", "session_id": "S-0005"}
OIL = {"vehicle_id": "E549YT198", "session_id": "S-0011"}
NORMAL = {"vehicle_id": "E549YT198", "session_id": "S-0010"}
FAILED = {"vehicle_id": "P456AT178", "session_id": "S-0003"}
MISMATCH = {"vehicle_id": "Y652CK98", "session_id": "S-0132"}


class SourceSelection:
    def __init__(self, *, accept=True):
        self.accept = accept
        self.calls = 0

    def ask(self, _prompt, response_schema, _trace):
        self.calls += 1
        properties = response_schema["properties"]
        if "items" in properties:
            sources = properties["items"]["items"]["properties"]["source"]["enum"]
            return {"items": [{"field": "checks", "source": sources[0]}] if self.accept else []}
        if "query" in properties and "action" not in properties:
            return {"query": "Дизельная топливоподача"}
        raise AssertionError(f"Unexpected model call: {list(properties)}")


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
    monkeypatch.setattr(main, "store", Store(tmp_path / "screen.sqlite3", send_deadline=1))
    monkeypatch.setattr(main, "TEAM_API_URL", "")
    monkeypatch.setattr(main, "TEAM_AGENT_URL", "")
    monkeypatch.setattr(main, "case_agent", None)
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
            yield client, adapter, sent


def enable_agent(monkeypatch, tmp_path, *, fragments=None, accept=True):
    corpus = CORPUS
    if fragments is not None:
        corpus = tmp_path / "case-corpus.json"
        corpus.write_text(json.dumps({"fragments": fragments}, ensure_ascii=False), encoding="utf-8")
    agent = CaseAgent(corpus, tmp_path / "agent-turns.jsonl")
    provider = SourceSelection(accept=accept)
    agent.agent.provider = provider
    monkeypatch.setattr(main, "case_agent", agent)
    return provider


def diagnose(client, pair):
    response = client.post("/api/diagnose", json=pair)
    assert response.status_code == 200, response.text
    return response.json()


def test_missing_both_agent_and_checked_sources_returns_clarification(integrated, monkeypatch, tmp_path):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path)
    monkeypatch.setattr(main.case_agent.mechanic, "defect_checks", {})
    state = diagnose(client, FUEL)
    assert state["diagnosis"]["anomaly_type"] == "F08"
    assert state["draft_available"] is False
    assert state["draft_unavailable_reason"]
    assert provider.calls == 0, "Reading readiness must not invoke a paid model"

    response = client.post("/api/draft", json=FUEL)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["draft"] is None
    assert result["draft_available"] is False
    assert result["draft_unavailable_reason"]
    assert any(section["kind"] == "clarify" for section in result["messages"][-1]["sections"])
    assert adapter.workflow.drafts(adapter.Pair(**FUEL)) == []
    assert sent == []


def test_f08_with_test_evidence_creates_one_draft_then_requires_confirmation(integrated, monkeypatch, tmp_path):
    client, adapter, sent = integrated
    fixture = {**verified_fragments()[0], "fragment_id": "fixture-f08-p037", "pdf_page": 37,
               "section": "Тестовая проверка топливоподачи",
               "text": "Проверить тестовый узел топливоподачи по тестовой процедуре.",
               "search_terms": ["Дизельная топливоподача"], "verified": True}
    enable_agent(monkeypatch, tmp_path, fragments=[fixture])
    assert diagnose(client, FUEL)["draft_available"] is True
    response = client.post("/api/draft", json=FUEL)
    assert response.status_code == 200, response.text
    state = response.json()
    draft = state["draft"]
    assert draft and draft["anomaly_type"] == "F08"
    assert draft["operations"] == [fixture["text"]]
    assert draft["references"][0]["pdf_page"] == 37
    assert draft["spare_parts"] == []
    assert sent == []

    repeated = client.post("/api/draft", json=FUEL).json()
    assert (repeated["draft_id"], repeated["draft_revision"]) == (state["draft_id"], state["draft_revision"])
    assert len(adapter.workflow.drafts(adapter.Pair(**FUEL))) == 1
    payload = {**FUEL, "draft_id": state["draft_id"], "revision": state["draft_revision"],
               "edits": {key: draft[key] for key in ("conclusion", "urgency", "operations", "spare_parts")},
               "priority_confirmed": False}
    assert client.post("/api/ticket", json=payload).status_code == 422
    assert sent == []
    payload["priority_confirmed"] = True
    submitted = client.post("/api/ticket", json=payload)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["ticket"]["status"] == "created"
    assert client.post("/api/ticket", json=payload).json()["ticket"]["ticket_id"] == submitted.json()["ticket"]["ticket_id"]
    assert len(sent) == 1


def test_model_declining_existing_evidence_returns_clarification_not_422(integrated, monkeypatch, tmp_path):
    client, adapter, sent = integrated
    enable_agent(monkeypatch, tmp_path, fragments=verified_fragments(), accept=False)
    monkeypatch.setattr(main.case_agent, "checked_draft", lambda *_args: None)
    assert diagnose(client, OIL)["draft_available"] is True
    response = client.post("/api/draft", json=OIL)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["draft"] is None
    assert result["draft_available"] is False
    assert result["draft_unavailable_reason"]
    assert client.get("/api/state", params=OIL).json()["draft_available"] is False
    assert adapter.workflow.drafts(adapter.Pair(**OIL)) == []
    assert sent == []


def test_verified_oil_draft_still_works_through_team_workflow(integrated, monkeypatch, tmp_path):
    client, _adapter, sent = integrated
    enable_agent(monkeypatch, tmp_path, fragments=verified_fragments())
    assert diagnose(client, OIL)["draft_available"] is True
    response = client.post("/api/draft", json=OIL)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["draft"]["operations"]
    assert state["draft"]["references"]
    assert state["ticket"]["status"] == "draft"
    assert client.post("/api/draft", json=OIL).json()["draft_id"] == state["draft_id"]
    assert sent == []


@pytest.mark.parametrize("pair", [NORMAL, FAILED, MISMATCH])
def test_non_actionable_diagnosis_never_offers_draft(integrated, monkeypatch, tmp_path, pair):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path)
    assert diagnose(client, pair)["draft_available"] is False
    assert provider.calls == 0
    assert adapter.workflow.drafts(adapter.Pair(**pair)) == []
    assert sent == []


@pytest.mark.parametrize("with_proposal", [False, True])
def test_late_preparation_keeps_newer_case_facts(integrated, monkeypatch, tmp_path, with_proposal):
    client, adapter, sent = integrated
    enable_agent(monkeypatch, tmp_path, fragments=verified_fragments())
    initial = diagnose(client, OIL)
    proposal = main.demo_agent.draft(main.VEHICLE_BY_ID[OIL["vehicle_id"]],
                                     Diagnosis.model_validate(initial["diagnosis"]))
    entered, finish = Event(), Event()
    fresh_entry = ["Новые измерения", "anomaly", None, [], {"kpa": 90}, None]

    def respond(_vehicle, _diagnosis, _state, question):
        if question == "Подготовь заявку":
            entered.set()
            assert finish.wait(10)
            reply = Reply(summary="Устаревший результат подготовки.", sections=[
                Section(kind="clarify", items=["Не хватает старых наблюдений."]),
            ])
            return reply, proposal if with_proposal else None, "team", [], None
        return Reply(summary="Новые наблюдения сохранены."), None, "team", [], fresh_entry

    monkeypatch.setattr(main, "response_to_chat", respond)
    with ThreadPoolExecutor(max_workers=2) as executor:
        pending = executor.submit(client.post, "/api/draft", json=OIL)
        try:
            assert entered.wait(5)
            fresh = client.post("/api/chat", json={**OIL, "message": "Новые измерения"})
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["draft_available"] is True
        finally:
            finish.set()
        late = pending.result(timeout=10)

    assert late.status_code == 200, late.text
    latest = client.get("/api/state", params=OIL).json()
    assert late.json()["messages"] == fresh.json()["messages"] == latest["messages"]
    assert latest["draft"] is None
    assert latest["draft_available"] is True
    assert latest["draft_unavailable_reason"] is None
    assert main.store.read((OIL["vehicle_id"], OIL["session_id"]))["agent_history"][-1] == fresh_entry
    assert adapter.workflow.drafts(adapter.Pair(**OIL)) == []
    assert sent == []


@pytest.mark.parametrize("failure,status,code", [
    (TimeoutError("test timeout"), 504, "agent_timeout"),
    (URLError("test connection"), 503, "agent_unavailable"),
])
def test_actual_agent_failure_is_not_missing_evidence(integrated, monkeypatch, tmp_path, failure, status, code):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path, fragments=verified_fragments())
    assert diagnose(client, OIL)["draft_available"] is True

    def fail(*_args):
        raise failure

    monkeypatch.setattr(provider, "ask", fail)
    response = client.post("/api/draft", json=OIL)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    state = client.get("/api/state", params=OIL).json()
    assert state["draft"] is None
    assert state["draft_available"] is True
    assert state["draft_unavailable_reason"] is None
    assert adapter.workflow.drafts(adapter.Pair(**OIL)) == []
    assert sent == []
