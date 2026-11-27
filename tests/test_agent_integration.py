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

    def __init__(self):
        self.requests = 0

    def ask(self, prompt, schema, trace):
        self.requests += 1
        fields = schema["properties"]
        if "items" in fields:
            choices = fields["items"]["items"]["properties"]["source"]["enum"]
            if "что такое" in prompt.lower():
                return {"items": []}
            return {"items": [{"source": choices[0], "field": "checks"}]}
        if "action" in fields:
            return {"action": "ticket", "query": "", "question": ""}
        if "topic" in fields:
            return {"topic": "mechanic"}
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
        self.provider = EvidenceSelector()
        agent.agent.provider = self.provider
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
        self.fuel = dict(vehicle_id="M333YK47", session_id="S-0005")

    def post(self, route, pair, **extra):
        response = self.client.post("/api/" + route, json={**pair, **extra})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_first_result_and_confirmation(self):
        pair = self.fuel
        first = self.post("diagnose", pair)
        self.assertEqual(first["brief"]["origin"], "team")
        self.assertEqual(first["brief"]["references"][0]["pdf_page"], 16)
        proposed = self.post("chat", pair, message="Оформи наряд механика")
        draft = proposed["draft"]
        self.assertEqual(draft["vehicle_id"], pair["vehicle_id"])
        self.assertEqual(draft["session_id"], pair["session_id"])
        self.assertTrue(draft["operations"])
        self.assertIn(16, {reference["pdf_page"] for reference in draft["references"]})
        repeat = self.post("chat", pair, message="Оформи наряд механика")
        self.assertEqual(repeat["draft_id"], proposed["draft_id"])
        self.assertEqual(repeat["draft_revision"], proposed["draft_revision"])
        self.assertFalse(self.sent)
        receipt = self.post("ticket", pair, draft_id=proposed["draft_id"], revision=proposed["draft_revision"],
                            diagnosis_revision=proposed["admission"]["diagnosis_revision"],
                            applicability_confirmations=["initial_start_or_long_storage"],
                            edits={**{k: draft[k] for k in ("operations", "spare_parts", "conclusion")},
                                   "urgency": "medium"},
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

    def test_known_diagnoses_and_first_checks_without_model(self):
        for pair, code, page, subject in (
            (self.oil, "F02", 53, "масл"),
            (self.fuel, "F08", 16, "топливопровод"),
        ):
            with self.subTest(code=code):
                before = self.provider.requests
                state = self.post("diagnose", pair)
                self.assertEqual(state["diagnosis"]["anomaly_type"], code)
                self.assertIn(subject, state["brief"]["first_check"].casefold())
                self.assertIn(page, {ref["pdf_page"] for ref in state["brief"]["references"]})
                reply = self.post("chat", pair, message="Что проверить сначала?")["messages"][-1]["analysis"]
                checks = [check["text"].casefold() for check in reply["checks"]]
                self.assertTrue(checks)
                self.assertIn(subject, checks[0])
                self.assertEqual(len(checks), len(set(checks)))
                self.assertIn(f"/sources/yamz236-re/{page}", {ref["url"] for ref in reply["references"]})
                self.assertEqual(self.provider.requests, before)

    def test_first_check_from_manual_for_blocked_draft(self):
        for plate, session, page in (("B332BP98", "S-0159", 97), ("Y439TH198", "S-0071", 161)):
            with self.subTest(session=session):
                pair = dict(vehicle_id=plate, session_id=session)
                before = self.provider.requests
                self.post("diagnose", pair)
                result = self.post("chat", pair, message="Что сначала проверить?")
                reply = result["messages"][-1]["analysis"]
                self.assertIn(page, {reference["pdf_page"] for reference in reply["references"]})
                self.assertTrue(reply["checks"] and reply["clarifying_question"])
                self.assertIsNone(result["draft"])
                self.assertEqual(self.provider.requests, before)

    def test_pressure_in_bar_and_ambiguous_range(self):
        self.post("diagnose", self.oil)
        state = self.post("chat", self.oil, message="3,5 бар, двигатель прогрет, 2100 об/мин, без допуска")
        reading = state["messages"][-1]["analysis"]
        comparison = reading["checks"][0]["text"]
        self.assertRegex(comparison, r"350(?:[.,]0+)?\s*кПа")
        self.assertIn("ниже", comparison.casefold())
        self.assertIn(19, {ref["pdf_page"] for ref in reading["references"]})
        state = self.post("chat", self.oil, message="350–450 кПа")
        ambiguous = state["messages"][-1]["analysis"]
        self.assertTrue(ambiguous["clarifying_question"])
        self.assertFalse(ambiguous["checks"], ambiguous)

    def test_fluid_report_keeps_distinct_levels(self):
        self.post("diagnose", self.oil)
        state = self.post("chat", self.oil, message="Масло в норме и антифриза мало")
        report = state["messages"][-1]["analysis"]
        self.assertEqual(report["fluid_check"]["oil"], "ok")
        self.assertEqual(report["fluid_check"]["coolant"], "low")
        self.assertIn(53, {ref["pdf_page"] for ref in report["references"]})
        self.assertIsNone(state["draft"])

    def test_f02_agent_continues_after_manometer_check(self):
        self.post("diagnose", self.oil)
        before = self.provider.requests
        first = self.post("chat", self.oil, message="Масло и охл. жидкость в норме")["messages"][-1]
        self.assertIn("Проверьте исправность манометра", first["summary"])
        for message in ("Он в норме, что дальше?", "Что проверять первым делом?", "Какую гипотезу проверять?"):
            state = self.post("chat", self.oil, message=message)
            analysis = state["messages"][-1]["analysis"]
            self.assertEqual(analysis["fluid_check"], {"oil": "ok", "coolant": "ok", "gauge": "ok"})
            self.assertIn("давления масла замером", analysis["checks"][0]["text"])
            self.assertEqual([ref["pdf_page"] for ref in analysis["references"]], [19])
            self.assertIsNone(state["draft"])
        result = self.post("chat", self.oil, message="350 кПа, прогрет, 2100 об/мин, без допуска")
        analysis = result["messages"][-1]["analysis"]
        self.assertIn("350 кПа — ниже", analysis["checks"][0]["text"])
        self.assertIn("загрязнение фильтра грубой очистки", analysis["checks"][1]["text"])
        self.assertEqual([ref["pdf_page"] for ref in analysis["references"]], [19, 53])
        self.assertEqual(self.provider.requests, before)
        self.assertFalse(self.sent)

    def test_movement_question_does_not_create_a_draft(self):
        self.post("diagnose", self.fuel)
        movement = self.post("chat", self.fuel, message="Можно ли ехать?")
        self.assertRegex(movement["messages"][-1]["summary"].casefold(), r"не подтвержд[её]н|нельзя|не разреш")
        self.assertIsNone(movement["draft"])
        self.assertFalse(self.sent)

    def test_negated_and_explanatory_ticket_questions_do_not_create_drafts(self):
        self.post("diagnose", self.fuel)
        for message in ("Не создавай заявку, скажи, что проверить", "Объясни, как оформить заявку на дефектовку"):
            with self.subTest(message=message):
                state = self.post("chat", self.fuel, message=message)
                self.assertIsNone(state["draft"])
        self.assertFalse(self.sent)

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
