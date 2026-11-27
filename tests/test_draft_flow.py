"""Screen readiness follows the server's reviewed-operation registry.

A deliberately unreviewed agent-index fixture must not replace the approved work.
Delivery tests use the real F14 operation; F02 remains documentation-only.
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
CHAIN = {"vehicle_id": "Y815BM178", "session_id": "S-0152"}
NORMAL = {"vehicle_id": "E549YT198", "session_id": "S-0010"}
FAILED = {"vehicle_id": "P456AT178", "session_id": "S-0003"}
MISMATCH = {"vehicle_id": "Y652CK98", "session_id": "S-0132"}
COOLANT = {"vehicle_id": "A427MA78", "session_id": "S-0032"}
GENERATOR = {"vehicle_id": "P348EE78", "session_id": "S-0035"}


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


class PlanProvider:
    """Deterministic planner for F03 answers that stay on the reviewed case."""

    def ask(self, _prompt, response_schema, _trace):
        properties = response_schema["properties"]
        if "items" in properties:
            sources = properties["items"]["items"]["properties"]["source"]["enum"]
            return {"items": [{"field": "checks", "source": sources[0]}]}
        if "action" in properties and "query" in properties:
            return {"action": "diagnosis", "query": ""}
        if "query" in properties:
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
    monkeypatch.setattr(main, "policy_mechanic", adapter.workflow.mechanic)
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


def enable_agent(monkeypatch, tmp_path, *, fragments=None, accept=True, provider=None):
    corpus = CORPUS
    if fragments is not None:
        corpus = tmp_path / "case-corpus.json"
        corpus.write_text(json.dumps({"fragments": fragments}, ensure_ascii=False), encoding="utf-8")
    agent = CaseAgent(corpus, tmp_path / "agent-turns.jsonl")
    provider = provider or SourceSelection(accept=accept)
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
    # Remove the actual reviewed fragment from both server and agent sources.
    fragment = "yamz-236-3902150-b-re-p016-fuel-lines-start-review"
    for mechanic in (adapter.workflow.mechanic, main.case_agent.mechanic):
        monkeypatch.delitem(mechanic.search.fragments, fragment)
    diagnose(client, FUEL)
    calls_after_diagnosis = provider.calls
    current = client.get("/api/state", params=FUEL)
    assert current.status_code == 200
    state = current.json()
    assert state["diagnosis"]["anomaly_type"] == "F08"
    assert state["draft_available"] is False
    assert state["admission"]["block_reason"]["code"] == "procedure_unavailable"
    assert provider.calls == calls_after_diagnosis, "Reading readiness must not invoke a paid model"
    response = client.post("/api/draft", json=FUEL)
    assert response.status_code == 422, response.text
    assert response.json()["admission"]["block_reason"]["code"] == "procedure_unavailable"
    chat = client.post("/api/chat", json={**FUEL, "message": "Подготовь заявку"})
    assert chat.status_code == 200, chat.text
    assert chat.json()["draft"] is None
    assert any(section["kind"] == "clarify" for section in chat.json()["messages"][-1]["sections"])
    assert provider.calls == calls_after_diagnosis, "Unavailable draft must not invoke a paid model"
    assert adapter.workflow.drafts(adapter.Pair(**FUEL)) == []
    assert sent == []


def test_unreviewed_agent_index_cannot_override_reviewed_operation(integrated, monkeypatch, tmp_path):
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
    assert fixture["text"] not in draft["operations"]
    assert draft["references"][0]["pdf_page"] == 16
    assert draft["spare_parts"] == []
    assert sent == []

    repeated = client.post("/api/draft", json=FUEL).json()
    assert (repeated["draft_id"], repeated["draft_revision"]) == (state["draft_id"], state["draft_revision"])
    assert len(adapter.workflow.drafts(adapter.Pair(**FUEL))) == 1
    payload = {**FUEL, "draft_id": state["draft_id"], "revision": state["draft_revision"],
               "edits": {key: draft[key] for key in ("conclusion", "urgency", "operations", "spare_parts")},
               "priority_confirmed": False, "diagnosis_revision": state["admission"]["diagnosis_revision"],
               "applicability_confirmations": ["initial_start_or_long_storage"]}
    assert client.post("/api/ticket", json=payload).status_code == 422
    assert sent == []
    payload["priority_confirmed"] = True
    submitted = client.post("/api/ticket", json=payload)
    assert submitted.status_code == 200, submitted.text
    assert submitted.json()["ticket"]["status"] == "created"
    assert client.post("/api/ticket", json=payload).json()["ticket"]["ticket_id"] == submitted.json()["ticket"]["ticket_id"]
    assert len(sent) == 1


def test_model_decline_does_not_remove_server_approved_operation(integrated, monkeypatch, tmp_path):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path, fragments=verified_fragments(), accept=False)
    assert diagnose(client, CHAIN)["draft_available"] is True
    calls_after_diagnosis = provider.calls
    response = client.post("/api/draft", json=CHAIN)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["draft"] and result["draft"]["operations"]
    assert result["draft"]["references"][0]["pdf_page"] == 143
    assert provider.calls == calls_after_diagnosis, "Preparing a reviewed draft must not call the model"
    assert len(adapter.workflow.drafts(adapter.Pair(**CHAIN))) == 1
    assert sent == []


def test_verified_chain_draft_works_through_team_workflow(integrated, monkeypatch, tmp_path):
    client, _adapter, sent = integrated
    enable_agent(monkeypatch, tmp_path, fragments=verified_fragments())
    assert diagnose(client, CHAIN)["draft_available"] is True
    response = client.post("/api/draft", json=CHAIN)
    assert response.status_code == 200, response.text
    state = response.json()
    assert state["draft"]["operations"]
    assert state["draft"]["references"]
    assert state["ticket"]["status"] == "draft"
    assert client.post("/api/draft", json=CHAIN).json()["draft_id"] == state["draft_id"]
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
    initial = diagnose(client, CHAIN)
    proposal = main.demo_agent.draft(main.VEHICLE_BY_ID[CHAIN["vehicle_id"]],
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
        pending = executor.submit(client.post, "/api/draft", json=CHAIN)
        try:
            assert entered.wait(5)
            fresh = client.post("/api/chat", json={**CHAIN, "message": "Новые измерения"})
            assert fresh.status_code == 200, fresh.text
            assert fresh.json()["draft_available"] is True
        finally:
            finish.set()
        late = pending.result(timeout=10)

    assert late.status_code == 200, late.text
    latest = client.get("/api/state", params=CHAIN).json()
    assert late.json()["messages"] == fresh.json()["messages"] == latest["messages"]
    assert latest["draft"] is None
    assert latest["draft_available"] is True
    assert latest["draft_unavailable_reason"] is None
    assert main.store.read((CHAIN["vehicle_id"], CHAIN["session_id"]))["agent_history"][-1] == fresh_entry
    assert adapter.workflow.drafts(adapter.Pair(**CHAIN)) == []
    assert sent == []


@pytest.mark.parametrize("failure,status,code", [
    (TimeoutError("test timeout"), 504, "agent_timeout"),
    (URLError("test connection"), 503, "agent_unavailable"),
])
def test_actual_agent_failure_is_not_missing_evidence(integrated, monkeypatch, tmp_path, failure, status, code):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path, fragments=verified_fragments())
    assert diagnose(client, CHAIN)["draft_available"] is True

    def fail(*_args):
        raise failure

    monkeypatch.setattr(provider, "ask", fail)
    response = client.post("/api/chat", json={**CHAIN, "message": "Как проверить состояние цепи в приводе ГРМ?"})
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    state = client.get("/api/state", params=CHAIN).json()
    assert state["draft"] is None
    assert state["draft_available"] is True
    assert state["draft_unavailable_reason"] is None
    assert adapter.workflow.drafts(adapter.Pair(**CHAIN)) == []
    assert sent == []


def test_f03_case_agent_reports_risk_without_reviewed_operation(integrated, monkeypatch, tmp_path):
    client, adapter, sent = integrated
    enable_agent(monkeypatch, tmp_path, provider=PlanProvider())
    result = diagnose(client, COOLANT)
    brief = result["brief"]
    assert brief["risk"] == "high"
    assert "Высокий" in brief["rationale"]
    assert [ref["pdf_page"] for ref in brief["references"]] == [23]
    analysis = brief["analysis"]
    assert any("прокладок" in item["text"] for item in analysis["possible_causes"])
    assert all(item["fragment_id"].endswith("coolant-in-oil") for item in analysis["possible_causes"])
    assert analysis["repair_actions"]
    assert result["draft_available"] is False
    assert result["admission"]["block_reason"]["code"] == "procedure_unavailable"
    assert sent == []

    def sections(message):
        return client.post("/api/chat", json={**COOLANT, "message": message}).json()["messages"][-1]["sections"]

    work = sections("Какие работы предусмотрены руководством?")
    assert any(section["kind"] == "work" and section["references"][0]["pdf_page"] == 54 for section in work)
    norm = sections("Какая норма температуры охлаждающей жидкости?")
    assert any(section["kind"] == "checks"
               and any(ref["pdf_page"] == 4 for ref in section["references"]) for section in norm)
    assert any(section["kind"] == "risk" for section in sections("Почему опасно?"))
    assert adapter.workflow.drafts(adapter.Pair(**COOLANT)) == []
    assert sent == []


@pytest.mark.parametrize("hybrid", [False, True])
def test_f05_risk_follows_the_confirmed_reading(integrated, monkeypatch, tmp_path, hybrid):
    client, adapter, sent = integrated
    provider = enable_agent(monkeypatch, tmp_path) if hybrid else None
    first = diagnose(client, GENERATOR)
    assert first["brief"]["risk"] == "unassessed"
    assert [ref["pdf_page"] for ref in first["brief"]["references"]] == [55]
    revision = first["generator_check"]["diagnosis_revision"]

    def chat(message, **extra):
        return client.post("/api/chat", json={**GENERATOR, "message": message, **extra})

    hypothetical = chat("Если на номинальных оборотах будет разряд, можно ли ехать?")
    assert hypothetical.status_code == 200, hypothetical.text
    assert hypothetical.json()["generator_check"]["value"] is None
    assert hypothetical.json()["brief"]["risk"] == "unassessed"

    def report(value, diagnosis_revision=revision):
        return chat("Показание амперметра", generator_reading={
            "value": value, "diagnosis_revision": diagnosis_revision,
        })

    stale = report("discharge", "0" * 64)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "diagnosis_changed"
    confirmed = report("discharge")
    assert confirmed.status_code == 200, confirmed.text
    state = confirmed.json()
    assert state["brief"]["risk"] == "high"
    assert "подшипников генератора" in state["brief"]["first_check"]
    assert state["generator_check"] == {"value": "discharge", "diagnosis_revision": revision}
    assert state["messages"][-1]["analysis"]["risk_level"] == "high"
    assert report("discharge").json()["messages"] == state["messages"]

    restored = client.get("/api/state", params=GENERATOR).json()
    assert restored["brief"] == state["brief"]
    assert diagnose(client, GENERATOR)["brief"] == state["brief"]
    followup = chat("Что проверять первым делом?")
    assert followup.status_code == 200, followup.text
    assert followup.json()["messages"][-1]["sections"][0]["items"] == [state["brief"]["first_check"]]

    no_discharge = report("no_discharge").json()
    assert no_discharge["brief"]["risk"] == "unassessed"
    assert "Разряд на номинальных оборотах не наблюдался" in no_discharge["brief"]["rationale"]
    unknown = report("unknown").json()
    assert unknown["brief"]["first_check"] == first["brief"]["first_check"]
    assert adapter.workflow.drafts(adapter.Pair(**GENERATOR)) == []
    assert not sent
    assert not provider or provider.calls == 0
