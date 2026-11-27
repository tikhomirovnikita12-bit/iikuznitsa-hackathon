"""Exercise the integrated screen API with deterministic external transports."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch
import httpx
from fastapi.testclient import TestClient

from backend.store import Store as BackendStore
from manual_search.corpus import verified_fragments
from mechanic import main
from mechanic.case_agent import CaseAgent
from mechanic.state import Store


class EvidenceSelector:
    """Only the provider is simulated. Lookup, page resolution and storage are real."""

    def ask(self, prompt, schema, trace):
        fields = schema["properties"]
        if "items" in fields:
            choices = fields["items"]["items"]["properties"]["source"]["enum"]
            if "что такое" in prompt.lower():
                return {"items": []}
            return {"items": [{"source": choices[0], "field": "checks"}]}
        if "action" in fields:
            return {"action": "ticket", "query": "", "question": ""}
        if "query" in fields:
            return {"query": "определение картера"}
        raise AssertionError(fields)


class Member4Tests(unittest.TestCase):
    def setUp(self):
        stack = self.enterContext(ExitStack())
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        corpus = folder / "corpus.json"
        corpus.write_text(json.dumps({"fragments": verified_fragments()}))
        agent = CaseAgent(corpus, folder / "turns.jsonl")
        agent.agent.provider = EvidenceSelector()
        stack.enter_context(patch.object(main, "case_agent", agent))
        stack.enter_context(patch.object(main, "store", Store(folder / "ui.db", 10)))
        workflow = main.team.workflow
        stack.enter_context(patch.object(workflow, "store", BackendStore(folder / "backend.db")))
        self.sent = []

        def upstream(request):
            body = json.loads(request.content)
            if request.url.path == "/tickets":
                self.sent.append(body)
                return httpx.Response(201, json={"ticket_id": "TKT-0001", "status": "created"})
            vehicle = main.VEHICLE_BY_ID[body["vehicle_id"]]
            row = next(s for s in vehicle["sessions"] if s["session_id"] == body["session_id"])
            return httpx.Response(200, json={k: row[k] for k in main.Diagnosis.model_fields})

        for name in ("ml", "toir"):
            transport = httpx.MockTransport(upstream)
            client = stack.enter_context(httpx.Client(base_url="http://fixture", transport=transport))
            stack.enter_context(patch.object(workflow, name, client))
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        self.oil = dict(vehicle_id="E549YT198", session_id="S-0011")

    def post(self, route, pair, **extra):
        response = self.client.post("/api/" + route, json={**pair, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_first_result_and_confirmation(self):
        pair = dict(vehicle_id="M333YK47", session_id="S-0005")
        first = self.post("diagnose", pair)
        self.assertEqual(first["brief"]["origin"], "team")
        self.assertEqual(first["brief"]["references"][0]["pdf_page"], 16)
        proposed = self.post("chat", pair, message="Оформи наряд механика")
        draft = proposed["draft"]
        self.assertEqual(draft["vehicle_id"], pair["vehicle_id"])
        self.assertEqual(draft["session_id"], pair["session_id"])
        self.assertTrue(draft["operations"] and draft["references"])
        self.assertFalse(self.sent)
        self.assertTrue(any(call["tool"] == "agent.search" and call["pages"] for call in proposed["calls"]))
        receipt = self.post("ticket", pair, draft_id=proposed["draft_id"], revision=proposed["draft_revision"],
                            edits={k: draft[k] for k in ("operations", "spare_parts", "urgency", "conclusion")},
                            diagnosis_revision=proposed["admission"]["diagnosis_revision"],
                            applicability_confirmations=["initial_start_or_long_storage"],
                            priority_confirmed=True)
        self.assertEqual(receipt["ticket"]["ticket_id"], "TKT-0001")
        self.assertEqual(len(self.sent), 1)

    def test_q05_on_case_agent_screen_route(self):
        pair = dict(vehicle_id="A464EP47", session_id="S-0096")
        self.post("diagnose", pair)
        reply = self.post("chat", pair, message="Какое давление масла на прогретом двигателе?")
        analysis = reply["messages"][-1]["analysis"]
        self.assertIn("400–635 кПа", analysis["checks"][0]["text"])
        self.assertIn("80–100 °C", analysis["checks"][0]["text"])
        self.assertEqual(analysis["references"][0]["url"], "/sources/yamz536-rk/13")

    def test_measurement_switch_and_recording(self):
        self.post("diagnose", self.oil)
        result = self.post("chat", self.oil, message="Можно ли перегнать машину своим ходом?")
        self.assertEqual(result["messages"][-1]["analysis"]["references"][0]["pdf_page"], 19)
        result = self.post("chat", self.oil, message="350 кПа, прогрет, 2100 об/мин, без допуска")
        self.assertIn("350 кПа — ниже", result["messages"][-1]["analysis"]["checks"][0]["text"])
        normal = dict(self.oil, session_id="S-0010")
        self.post("diagnose", normal)
        reply = self.post("chat", normal, message="А что проверить сначала?")["messages"][-1]["analysis"]
        self.assertEqual(reply["ml_observations"]["status"], "normal")
        self.assertFalse(reply["checks"])
        for plate, session, status in [("P456AT178", "S-0003", "check_failed"), ("Y652CK98", "S-0132", "anomaly")]:
            pair = dict(vehicle_id=plate, session_id=session)
            self.post("diagnose", pair)
            reply = self.post("chat", pair, message="Что проверить?")["messages"][-1]["analysis"]
            self.assertEqual(reply["ml_observations"]["status"], status)
            self.assertFalse(reply["checks"] or reply["ticket_draft"])
            if status == "anomaly":
                self.assertIn("топливоподача", reply["explanation"].lower())
                self.assertIn("Сверьте", reply["clarifying_question"])
            else:
                self.assertIn("Повторите", reply["verdict"])

    def test_missing_definition(self):
        self.post("diagnose", self.oil)
        reply = self.post("chat", self.oil, message="Что такое картер?")["messages"][-1]["analysis"]
        self.assertFalse(reply["references"])
        self.assertIn("дефектовку", reply["clarifying_question"])

    def test_warning_preserves_raw_scores(self):
        pair = dict(vehicle_id="M739BA198", session_id="S-0103")
        card = main.VEHICLE_BY_ID[pair["vehicle_id"]]
        session = next(s for s in card["sessions"] if s["session_id"] == pair["session_id"])
        result = self.post("diagnose", pair)["brief"]["analysis"]["ml_observations"]
        self.assertEqual(result["signal_quality"], "warning")
        for key in ("anomaly_score", "confidence", "description", "model_version"):
            self.assertEqual(result[key], session[key])
        self.assertEqual(result["engine_model"], card["engine_model"])
