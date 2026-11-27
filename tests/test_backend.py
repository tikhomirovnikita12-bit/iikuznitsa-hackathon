"""Integration tests against the unmodified customer mock apps over ASGI."""

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
import unittest

import httpx
from fastapi.testclient import TestClient

from backend.app import ROOT, Settings, create_app


NORMAL = {"vehicle_id": "E549YT198", "session_id": "S-0010"}
ANOMALY = {"vehicle_id": "E549YT198", "session_id": "S-0011"}
BAD_RECORDING = {"vehicle_id": "P456AT178", "session_id": "S-0003"}


def relay(client, request):
    return client.request(request.method, request.url.path, content=request.content,
                          headers={"content-type": "application/json"})


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        customer = ROOT / "artifacts/customer"
        if not (customer / "mock_ml/app.py").is_file():
            raise unittest.SkipTest("Extract the customer archive into artifacts/customer")
        sys.path.insert(0, str(customer))
        from mock_ml.app import app as ml
        from mock_toir.app import app as toir

        cls.ml = TestClient(ml)
        cls.toir = TestClient(toir)

    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(database=Path(temp.name) / "state.sqlite3")
        self.ml_requests = []
        self.toir_requests = []
        self.ml_override = None
        self.toir_override = None
        self.client = self.api()
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def api(self, settings=None, *, ollama_transport=None):
        return TestClient(create_app(
            settings or self.settings,
            ml_transport=httpx.MockTransport(self.ml_request),
            toir_transport=httpx.MockTransport(self.toir_request),
            ollama_transport=ollama_transport,
        ))

    def ml_request(self, request):
        self.ml_requests.append(json.loads(request.content))
        return self.ml_override(request) if self.ml_override else relay(self.ml, request)

    def toir_request(self, request):
        self.toir_requests.append(json.loads(request.content))
        return self.toir_override(request) if self.toir_override else relay(self.toir, request)

    def draft(self, pair=ANOMALY):
        self.assertEqual(self.client.post("/api/diagnose", json=pair).status_code, 200)
        response = self.client.post("/api/drafts", json=pair)
        self.assertEqual(response.status_code, 200, response.text)
        draft = response.json()
        return "/api/drafts/" + draft["draft_id"], draft

    def confirm(self, path, revision=1):
        return self.client.post(path + "/confirm", json={
            "revision": revision, "urgency": "high", "confirmed": True,
        })

    def test_engine_manual_catalog(self):
        response = self.client.get("/api/manuals")
        self.assertEqual(response.status_code, 200)
        library = response.json()
        self.assertEqual(len(library), 6)
        manual = next(item for item in library if item["manual_id"] == "yamz-236-3902150-b-re")
        self.assertIn("ЯМЗ-236М2", manual["applicable_engine_models"])
        self.assertEqual(manual["page_count"], 172)

    def test_customer_health_catalog_and_three_diagnoses(self):
        self.assertEqual(self.ml.get("/health").json(), {
            "status": "ok", "service": "mock-ml", "vehicles": 50, "sessions": 195,
        })
        self.assertEqual(self.toir.get("/health").json()["service"], "mock-toir")
        vehicles = self.client.get("/api/vehicles").json()
        self.assertEqual(len(vehicles), 50)
        self.assertEqual(set(vehicles[0]), {"vehicle_id", "make", "model", "engine_model"})
        self.assertIsNone(next(v["model"] for v in vehicles if v["vehicle_id"] == "O727CA198"))
        sessions = self.client.get("/api/vehicles/E549YT198/sessions").json()
        self.assertTrue(any(item["session_id"] == "S-0011" for item in sessions))
        self.assertEqual(set(sessions[0]), {"session_id", "checked_at"})

        for pair, expected in ((NORMAL, "normal"), (ANOMALY, "anomaly"), (BAD_RECORDING, "check_failed")):
            with self.subTest(expected=expected):
                response = self.client.post("/api/diagnose", json=pair)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data["diagnosis"]["status"], expected)
                self.assertEqual(data["diagnosis"]["model_version"], "mock-v1")
                self.assertEqual(data["engine_model"], "ЯМЗ-236М2")
                self.assertEqual(self.client.get("/api/diagnosis", params=pair).json(), data)
                if expected == "check_failed":
                    self.assertIsNone(data["diagnosis"]["anomaly_score"])
                    self.assertIsNone(data["diagnosis"]["confidence"])
                    self.assertIsNone(data["assessment"]["conclusion"])
                    self.assertIn("Повторить запись", data["assessment"]["next_step"])
        self.assertEqual(self.ml_requests, [NORMAL, ANOMALY, BAD_RECORDING])

    def test_diagnosis_failures_are_not_recording_failures(self):
        response = self.client.post("/api/diagnose", json={**ANOMALY, "vehicle_id": "P456AT178"})
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (404, "session_not_found"))
        self.assertEqual(self.ml_requests, [])

        def raise_http(error, request):
            raise error("test", request=request)

        for status, code, override in (
            (404, "ml_not_found", lambda req: httpx.Response(404)),
            (422, "ml_rejected", lambda req: httpx.Response(422)),
            (504, "ml_timeout", lambda req: raise_http(httpx.ReadTimeout, req)),
            (503, "ml_unavailable", lambda req: raise_http(httpx.ConnectError, req)),
            (502, "ml_invalid_response", lambda req: httpx.Response(200, json={"status": "normal"})),
            (502, "ml_pair_mismatch", lambda req: self.ml.post("/diagnose", json=NORMAL)),
        ):
            with self.subTest(code=code):
                self.ml_override = override
                response = self.client.post("/api/diagnose", json=ANOMALY)
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn("diagnosis", response.json())
                self.assertEqual(self.client.get("/api/diagnosis", params=ANOMALY).status_code, 409)

    def test_draft_validation_edit_confirm_and_read_after_restart(self):
        self.client.post("/api/diagnose", json=BAD_RECORDING)
        self.assertEqual(self.client.post("/api/drafts", json=BAD_RECORDING).status_code, 409)
        path, draft = self.draft()
        self.assertIsNone(draft["urgency"])
        self.assertEqual(draft["spare_parts"], [])
        self.assertEqual(self.client.post("/api/drafts", json=ANOMALY).json(), draft)

        wrong_engine = {"manual": "536.3902150 РЭ", "pdf_page": 49}
        invalid = self.client.patch(path, json={"revision": 1, "references": [wrong_engine]})
        self.assertEqual(invalid.json()["error"]["code"], "invalid_reference")
        blank = self.client.patch(path, json={"revision": 1, "operations": ["   "]})
        self.assertEqual(blank.status_code, 422)
        cleared = self.client.patch(path, json={"revision": 1, "operations": []})
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(self.confirm(path, 2).json()["error"]["code"], "invalid_ticket")
        denied = self.client.post(path + "/confirm", json={"revision": 2, "urgency": "high", "confirmed": False})
        self.assertEqual(denied.status_code, 422)
        updated = self.client.patch(path, json={"revision": 2, "operations": draft["operations"]})
        self.assertEqual(updated.json()["revision"], 3)
        self.assertEqual(self.confirm(path, 2).json()["error"]["code"], "stale_draft")

        response = self.confirm(path, 3)
        self.assertEqual(response.status_code, 200, response.text)
        sent = response.json()
        self.assertEqual(sent["state"], "sent")
        self.assertTrue(sent["ticket_id"].startswith("TKT-"))
        self.assertEqual(self.confirm(path, 3).json(), sent)
        self.assertEqual(len(self.toir_requests), 1)
        self.assertEqual(self.toir_requests[0], {
            **ANOMALY, "anomaly_type": "F02", "urgency": "high", "spare_parts": [],
            "conclusion": draft["conclusion"], "operations": draft["operations"],
            "references": draft["references"],
        })
        with self.api() as reopened:
            self.assertEqual(reopened.get(path).json(), sent)
            self.assertEqual(reopened.get("/api/drafts", params=ANOMALY).json(), [sent])

    def test_parallel_confirm_only_sends_once(self):
        path, _ = self.draft()
        started, release = Event(), Event()

        def slow_toir(request):
            started.set()
            if not release.wait(10):
                raise RuntimeError("test did not release delivery")
            return relay(self.toir, request)

        self.toir_override = slow_toir
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.confirm, path)
            try:
                self.assertTrue(started.wait(10))
                self.assertEqual(self.confirm(path).json()["state"], "sending")
            finally:
                release.set()
            self.assertEqual(pending.result().json()["state"], "sent")
        self.assertEqual(len(self.toir_requests), 1)

    def test_delivery_error_is_recoverable_only_when_no_ticket_was_sent(self):
        path, original = self.draft()

        def unavailable(request):
            raise httpx.ConnectError("test connection refused", request=request)

        self.toir_override = unavailable
        response = self.confirm(path)
        self.assertEqual(response.status_code, 503)
        failed = response.json()["draft"]
        self.assertEqual(failed["state"], "delivery_failed")
        self.assertEqual(failed["operations"], original["operations"])
        self.assertEqual(failed, self.client.get(path).json())

        receipt = None

        def accepted_but_timed_out(request):
            nonlocal receipt
            accepted = relay(self.toir, request)
            self.assertEqual(accepted.status_code, 201)
            receipt = accepted.json()["ticket_id"]
            raise httpx.ReadTimeout("test lost receipt", request=request)

        self.toir_override = accepted_but_timed_out
        response = self.confirm(path)
        self.assertEqual(response.status_code, 504)
        unknown = response.json()["draft"]
        self.assertEqual(unknown["state"], "delivery_unknown")
        self.assertIsNone(unknown["ticket_id"])
        with self.api() as reopened:
            self.assertEqual(reopened.get(path).json(), unknown)
        self.assertEqual(self.confirm(path).status_code, 409)
        self.assertEqual(len(self.toir_requests), 2)

        verified = {"revision": 1, "ticket_id": receipt, "verified": True}
        reconcile = path + "/reconcile"
        self.assertEqual(self.client.post(reconcile, json={**verified, "ticket_id": "123"}).status_code, 422)
        self.assertEqual(self.client.post(reconcile, json={**verified, "verified": False}).status_code, 422)
        self.assertEqual(self.client.post(reconcile, json={**verified, "revision": 2}).status_code, 409)
        sent = self.client.post(reconcile, json=verified).json()
        self.assertEqual((sent["state"], sent["ticket_id"], sent["revision"]), ("sent", receipt, 2))
        self.assertIsNone(sent["delivery_error"])
        self.assertEqual(self.client.post(reconcile, json=verified).json(), sent)
        self.assertEqual(self.client.post(reconcile, json={**verified, "ticket_id": "TKT-9999"}).status_code, 409)
        self.assertEqual(self.confirm(path).json(), sent)
        self.assertEqual(len(self.toir_requests), 2)
        with self.api() as reopened:
            self.assertEqual(reopened.get(path).json(), sent)

    def test_customer_anomalies_have_sourced_work_even_without_extracted_index(self):
        settings = replace(self.settings, corpus=self.settings.database.parent / "no-index.json")
        vehicles = json.loads(settings.vehicles.read_text(encoding="utf-8"))["vehicles"]
        statuses = {"normal": 0, "anomaly": 0, "check_failed": 0}
        with self.api(settings) as api:
            for vehicle in vehicles:
                for session in vehicle["sessions"]:
                    pair = {"vehicle_id": vehicle["vehicle_id"], "session_id": session["session_id"]}
                    with self.subTest(**pair):
                        response = api.post("/api/diagnose", json=pair)
                        self.assertEqual(response.status_code, 200, response.text)
                        result = response.json()
                        status = result["diagnosis"]["status"]
                        self.assertEqual(status, session["status"])
                        statuses[status] += 1
                        if status != "anomaly":
                            continue
                        response = api.post("/api/drafts", json=pair)
                        self.assertEqual(response.status_code, 200, response.text)
                        draft = response.json()
                        self.assertTrue(draft["operations"])
                        self.assertTrue(draft["references"])
                        self.assertEqual(draft["operations"], result["assessment"]["checks"])
                        reference = draft["references"][0]
                        check_reference = api.patch(f"/api/drafts/{draft['draft_id']}", json={
                            "revision": 1, "references": [reference],
                        })
                        self.assertEqual(check_reference.status_code, 200, check_reference.text)
        self.assertEqual(sum(statuses.values()), 195)
        self.assertEqual(statuses["anomaly"], 69)
        self.assertEqual(self.toir_requests, [])

    def test_f08_draft_requires_confirmation_and_survives_reopening(self):
        pair = {"vehicle_id": "M333YK47", "session_id": "S-0005"}
        path, draft = self.draft(pair)
        self.assertEqual(draft["anomaly_type"], "F08")
        self.assertEqual(draft["operations"], [
            "При подготовке к первичному пуску нового двигателя или после длительной стоянки проверить соединения и надёжность крепления топливопроводов.",
        ])
        self.assertEqual(draft["references"], [{"manual": "236-3902150-Б РЭ", "pdf_page": 16}])
        self.assertEqual(draft["spare_parts"], [])
        self.assertEqual(self.toir_requests, [])
        rejected = self.client.post(path + "/confirm", json={
            "revision": draft["revision"], "urgency": "medium", "confirmed": False,
        })
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(self.toir_requests, [])
        request = {"revision": draft["revision"], "urgency": "medium", "confirmed": True}
        sent = self.client.post(path + "/confirm", json=request)
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertEqual(sent.json()["state"], "sent")
        self.assertEqual(self.client.post(path + "/confirm", json=request).json(), sent.json())
        self.assertEqual(len(self.toir_requests), 1)
        self.assertEqual(self.toir_requests[0]["vehicle_id"], pair["vehicle_id"])
        self.assertEqual(self.toir_requests[0]["anomaly_type"], "F08")
        with self.api() as reopened:
            self.assertEqual(reopened.get(path).json(), sent.json())

    def test_latest_diagnosis_must_still_match_draft(self):
        path, _ = self.draft()
        changed = self.ml.post("/diagnose", json=NORMAL).json()
        self.ml_override = lambda req: httpx.Response(200, json={**changed, **ANOMALY})
        self.assertEqual(self.client.post("/api/diagnose", json=ANOMALY).json()["diagnosis"]["status"], "normal")
        self.assertEqual(self.confirm(path).json()["error"]["code"], "diagnosis_changed")
        self.assertEqual(self.toir_requests, [])

    def test_chat_source_clarifications_and_document_route(self):
        self.client.post("/api/diagnose", json=ANOMALY)
        for question, needs_clarification in (
            ("При каких условиях проверять давление масла?", False),
            ("Измерили 90 кПа. Это нормально?", True),
            ("Можно ли своим ходом перегнать машину на базу?", True),
            ("Как обновить прошивку мультимедиа?", True),
        ):
            response = self.client.post("/api/chat", json={**ANOMALY, "question": question})
            self.assertEqual(response.status_code, 200, response.text)
            reply = response.json()
            self.assertEqual(reply["needs_clarification"], needs_clarification)
            if "мультимедиа" in question:
                self.assertEqual(reply["evidence"], [])
                continue
            source = reply["evidence"][0]["source"]
            self.assertEqual((source["manual"], source["pdf_page"]), ("236-3902150-Б РЭ", 19))
            if source["url"]:
                file_response = self.client.get(source["url"].split("#")[0], headers={"Range": "bytes=0-4"})
                self.assertEqual(file_response.status_code, 206)
                self.assertEqual(file_response.content, b"%PDF-")

    def test_chat_follows_topic_and_ignores_history_for_a_new_question(self):
        initial_question = "При каких условиях проверять давление масла?"
        reply = self.client.post("/api/chat", json={
            **ANOMALY, "question": "Получи результат диагностики",
        }).json()
        self.assertEqual(reply["diagnostic_result"]["diagnosis"]["status"], "anomaly")

        reply = self.client.post("/api/chat", json={**ANOMALY, "question": initial_question}).json()
        history = [{"question": initial_question, "answer": reply["answer"]}]
        reply = self.client.post("/api/chat", json={
            **ANOMALY, "question": "А на холостых?", "history": history,
        }).json()
        self.assertEqual(reply["evidence"][0]["source"]["pdf_page"], 19)
        self.assertIn("холостом ходу", reply["answer"])
        reply = self.client.post("/api/chat", json={
            **ANOMALY, "question": "Какие причины пониженного давления указаны в руководстве?", "history": history,
        }).json()
        self.assertEqual(reply["evidence"][0]["source"]["pdf_page"], 53)
        reply = self.client.post("/api/chat", json={
            **ANOMALY, "question": "Как обновить прошивку мультимедиа?", "history": history,
        }).json()
        self.assertTrue(reply["needs_clarification"])
        self.assertEqual(reply["evidence"], [])

        reply = self.client.post("/api/chat", json={**ANOMALY, "question": "Создай заявку"}).json()
        self.assertEqual(reply["draft"]["state"], "draft")
        self.assertEqual(self.toir_requests, [])

    def test_ollama_plans_a_scoped_action_and_recovers_from_bad_output(self):
        settings = replace(self.settings, ollama_model="test-model")
        history = [{"question": "Что проверить при низком давлении масла?", "answer": "Сверьте условия измерения."}]
        requests = []

        def ollama(request):
            requests.append(json.loads(request.content))
            content = {
                1: '{"tool":"diagnose","query":null}',
                2: '{"tool":"create_draft","query":null}',
                3: '{"tool":"create_draft","query":null}',
            }.get(len(requests), "invalid")
            return httpx.Response(200, json={"message": {"content": content}})

        with self.api(settings, ollama_transport=httpx.MockTransport(ollama)) as api:
            # These commands go beyond the exact phrases accepted by the offline router.
            reply = api.post("/api/chat", json={**ANOMALY, "question": "Проведи диагностику машины"}).json()
            self.assertEqual(reply["diagnostic_result"]["diagnosis"]["status"], "anomaly")
            reply = api.post("/api/chat", json={**ANOMALY, "question": "Оформи черновик по этой проверке"}).json()
            self.assertEqual(reply["draft"]["state"], "draft")
            reply = api.post("/api/chat", json={**ANOMALY, "question": "Какое давление масла?"}).json()
            self.assertIsNone(reply["draft"])
            reply = api.post("/api/chat", json={**ANOMALY, "question": "А на холостых?", "history": history}).json()
            self.assertEqual(reply["evidence"][0]["source"]["pdf_page"], 19)
        self.assertEqual(requests[-1]["messages"][1], {"role": "user", "content": history[0]["question"]})
        self.assertIn("format", requests[0])
        self.assertEqual(self.ml_requests, [ANOMALY])
        self.assertEqual(self.toir_requests, [])
