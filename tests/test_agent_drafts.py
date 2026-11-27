"""A draft needs source-linked checks, including when the model declines a case."""

import json
from pathlib import Path

import pytest

from agent.dialog import Agent
from agent.manual_agent import ManualAgent
from agent.tools.diagnosis import describe
from agent.tools.manual import Manuals
from agent.tools.ticket import prepare
from mechanic.case_agent import CaseAgent
from mechanic.contracts import Diagnosis


ROOT = Path(__file__).resolve().parents[1]


def diagnosis():
    fleet = json.loads((ROOT / "data/vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    vehicle = next(item for item in fleet if item["vehicle_id"] == "M333YK47")
    session = next(item for item in vehicle["sessions"] if item["session_id"] == "S-0005")
    return {**session, "vehicle_id": vehicle["vehicle_id"],
            "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}


class NoEvidence:
    def __init__(self):
        self.calls = []

    def ask(self, prompt, format_, trace):
        self.calls.append(prompt)
        if "query" in format_["properties"]:
            return {"query": "Дизельная топливоподача"}
        return {"items": []}


def test_f08_uses_reviewed_operation_without_model_selection(tmp_path):
    provider = NoEvidence()
    agent = Agent(None, Manuals(ROOT / "artifacts/manual-corpus.json"), provider,
                  log=tmp_path / "turns.jsonl")
    result = agent.turn("M333YK47", "S-0005", "Подготовь заявку", diagnosis=diagnosis())
    assert result["ml_observations"]["anomaly_type"] == "F08"
    assert result["ticket_draft"]
    assert result["clarifying_question"]
    assert result["checks"] and result["references"][0]["pdf_page"] == 16
    assert result["admission"]["can_prepare"]
    assert not result["admission"]["can_submit"]
    assert result["admission"]["approved_operations"][0]["conditions"][0]["requires_confirmation"]
    assert provider.calls == []


def test_capability_matches_ticket_sources_without_calling_model(tmp_path):
    class NoCalls:
        def ask(self, *args, **kwargs):
            raise AssertionError("Availability must never call the model")

    agent = CaseAgent(ROOT / "artifacts/manual-corpus.json", tmp_path / "turns.jsonl")
    agent.agent.provider = NoCalls()
    fleet = json.loads((ROOT / "data/vehicles.json").read_text(encoding="utf-8"))["vehicles"]
    for plate, session, expected in [("M333YK47", "S-0005", True),
                                     ("E549YT198", "S-0011", True),
                                     ("E549YT198", "S-0010", False),
                                     ("O710HH78", "S-0014", True),
                                     ("C933KX98", "S-0163", False),
                                     ("Y652CK98", "S-0132", False),
                                     ("P456AT178", "S-0003", False)]:
        vehicle = next(item for item in fleet if item["vehicle_id"] == plate)
        result = next(item for item in vehicle["sessions"] if item["session_id"] == session)
        assert agent.can_prepare(vehicle, Diagnosis.model_validate(result)) is expected
    assert not (tmp_path / "turns.jsonl").exists()


def grounded_reply():
    reply = describe(diagnosis())
    reply["checks"] = [{"text": "Проверить соединения топливопроводов.", "fragment_id": "checked"}]
    reply["references"] = [{"fragment_id": "checked", "manual": "Руководство", "pdf_page": 16}]
    return reply


@pytest.mark.parametrize("change", [
    lambda reply: reply.update(references=[]),
    lambda reply: reply["checks"][0].update(fragment_id="missing"),
    lambda reply: reply["checks"][0].update(text=" "),
    lambda reply: reply["references"][0].update(pdf_page=0),
    lambda reply: reply["references"][0].update(pdf_page=True),
    lambda reply: reply["references"][0].update(manual=""),
])
def test_draft_rejects_checks_without_valid_references(change):
    reply = grounded_reply()
    change(reply)
    assert prepare(reply) is None


def test_draft_contains_only_sources_of_its_checks():
    reply = grounded_reply()
    reply["references"].append({"fragment_id": "risk-only", "manual": "Другое руководство", "pdf_page": 7})
    result = prepare(reply)
    assert result["operations"] == [reply["checks"][0]["text"]]
    assert result["references"] == [{"manual": "Руководство", "pdf_page": 16}]
    assert result["spare_parts"] == []


def test_ticket_selection_cannot_write_a_disallowed_field():
    class Provider:
        def ask(self, prompt, format_, trace):
            properties = format_["properties"]["items"]["items"]["properties"]
            assert properties["field"]["enum"] == ["checks"]
            source = properties["source"]["enum"][0]
            return {"items": [{"source": source, "field": "risk"}]}

    class Sources:
        def reference(self, fragment, model):
            raise AssertionError("A disallowed selection must not resolve a source")

    reply = describe(diagnosis())
    reply["intent"] = "ticket"
    hit = {"fragment_id": "checked", "text": "Проверить соединения топливопроводов.", "verified": True}
    ManualAgent(Sources()).select(reply, "Подготовь заявку", "соединения топливопроводов", [hit],
                                  Provider(), {"calls": []}, coolant=False, next_step=False, previous=None)
    assert not reply["checks"] and not reply["references"] and not reply["risk"]
