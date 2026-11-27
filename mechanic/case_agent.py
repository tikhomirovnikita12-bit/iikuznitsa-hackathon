from time import perf_counter
from urllib.error import URLError

from agent.case_context import blocked_message
from agent.tools.diagnosis import describe
from agent.dialog import Agent, history_entry, incompatible_fault
from agent.tools.manual import Manuals
from backend.mechanic import Mechanic
from backend.models import Diagnosis as BackendDiagnosis

from .sources import BY_NAME
from .contracts import DraftFields, Reference, Reply, Section
from .config import MATERIALS
from .demo_agent import wants_draft


class CaseAgent:
    def __init__(self, corpus, log, *, mechanic=None):
        self.agent = Agent(backend=None, manuals=Manuals(corpus), log=log)
        self.mechanic = mechanic if mechanic is not None else Mechanic(corpus, MATERIALS)

    def checked_draft(self, vehicle, diagnosis):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        if diagnosis.signal_quality == "bad" or diagnosis.status != "anomaly" or incompatible_fault(context):
            return None
        backend_diagnosis = BackendDiagnosis.model_validate(diagnosis.model_dump())
        fields = self.mechanic.suggest_draft(backend_diagnosis, vehicle["engine_model"])
        if not fields.operations or not fields.references or any(
            not self.mechanic.reference_applies(reference, vehicle["engine_model"]) for reference in fields.references
        ):
            return None
        evidence = self.mechanic.assess(backend_diagnosis, vehicle["engine_model"]).evidence
        if not evidence or any(not self.mechanic.pdf_paths[
            self.mechanic.search.fragments[item.source.fragment_id]["manual_id"]
        ].is_file() for item in evidence):
            return None
        return DraftFields.model_validate({**fields.model_dump(), "urgency": fields.urgency or "medium"})

    def checked_reply(self, question, fields, vehicle, diagnosis):
        summary = "Черновик подготовлен по проверенной операции руководства. Проверьте применимость и приоритет."
        reply = Reply(summary=summary, sections=[Section(kind="checks", items=fields.operations,
                                                       references=fields.references)])
        evidence = self.mechanic.assess(BackendDiagnosis.model_validate(diagnosis.model_dump()),
                                        vehicle["engine_model"]).evidence
        first = fields.operations[0]
        source = next((item.source for item in evidence if len(fields.references) == 1
                       or f"стр. {item.source.pdf_page}" in first), None)
        entry = history_entry(question, {
            "intent": "ticket", "verdict": summary + " " + " ".join(fields.operations),
            "clarifying_question": None,
            "checks": [{"text": first, "fragment_id": source.fragment_id if source else None}],
        })
        return reply, fields, entry

    def can_prepare(self, vehicle, diagnosis, history=()):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        if context["signal_quality"] == "bad" or context["status"] != "anomaly" or incompatible_fault(context):
            return False
        return bool(self.checked_draft(vehicle, diagnosis))

    def chat(self, vehicle, diagnosis, question, history):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        if wants_draft(question):
            started = perf_counter()
            fields = self.checked_draft(vehicle, diagnosis)
            elapsed = round((perf_counter() - started) * 1000)
            if fields and len(fields.references) == 1:
                pages = [(ref.manual, ref.pdf_page) for ref in fields.references]
                return *self.checked_reply(question, fields, vehicle, diagnosis), [
                    dict(tool="agent.search", duration_ms=elapsed, status="ok", pages=pages),
                    dict(tool="agent.prepare_ticket", duration_ms=0, status="ok"),
                ]
            if not fields:
                message = blocked_message("Нет подтверждённой процедуры.", context, vehicle["engine_model"])
                return Reply(summary=message, sections=[Section(kind="clarify", items=[message])]), None, None, []
        trace = {}
        try:
            result = self.agent.turn(diagnosis.vehicle_id, diagnosis.session_id, question, history,
                                     trace_out=trace, diagnosis=context)
        except (URLError, TimeoutError):
            if wants_draft(question):
                raise
            result = describe(context)
            result.update(intent="clarify", clarifying_question="Языковая модель недоступна. Уточните узел и режим измерения.")
            if question == "Покажи диагностику":
                started = perf_counter()
                assessment = self.mechanic.assess(BackendDiagnosis.model_validate(diagnosis.model_dump()), vehicle["engine_model"])
                for evidence in assessment.evidence:
                    source = evidence.source
                    result["references"].append(source.model_dump())
                    result["checks"].append(dict(text=evidence.text, fragment_id=source.fragment_id))
                trace["tools"].append(dict(name="search", seconds=perf_counter()-started,
                                           pages=[(e.source.manual, e.source.pdf_page) for e in assessment.evidence]))
        calls = []
        for tool in trace["tools"]:
            calls.append(dict(tool="agent." + tool["name"], duration_ms=round(tool["seconds"] * 1000),
                              status="ok", pages=tool.get("pages", [])))
        for call in trace["calls"]:
            calls.append(dict(tool="ollama.chat", duration_ms=round(call["seconds"] * 1000), status=call.get("status", "ok"),
                              model=call["model"], input_tokens=call["input_tokens"], output_tokens=call["output_tokens"]))
        if result.get("intent") == "ticket" and wants_draft(question):
            if fields := self.checked_draft(vehicle, diagnosis):
                selected = {r["pdf_page"] for r in result["references"]}
                if selected:
                    fields.references.sort(key=lambda r: r.pdf_page not in selected)
                return *self.checked_reply(question, fields, vehicle, diagnosis), calls
        result["ticket_draft"] = None

        references = {
            item["fragment_id"]: Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
        }
        for item in result["references"]:
            manual = BY_NAME[item["manual"]]
            item["url"] = f"/sources/{manual.slug}/{item['pdf_page']}"
        sections = [Section(kind="signs", items=[context["description"]])]
        for field, kind in (("possible_causes", "causes"), ("risk", "risk"), ("checks", "checks")):
            for item in result[field]:
                text = ("Гипотеза: " if kind in ("risk", "causes") else "") + item["text"]
                sections.append(Section(kind=kind, items=[text], references=[references[item["fragment_id"]]]))
        if result["checks"] and not result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=["Применимость к машине требует сверки исполнения узла и условий проверки."]))
        if question_to_clarify := result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=[question_to_clarify],
                                    references=list(references.values()) if result.get("intent") == "movement" else []))

        summary = result.get("explanation") or result["verdict"]
        draft = result["ticket_draft"]
        proposal = None
        if draft:
            proposal = DraftFields.model_validate({key: draft[key] for key in DraftFields.model_fields})
            summary = "Черновик подготовлен. Проверьте поля и подтвердите приоритет."
        return Reply(summary=summary, sections=sections, analysis=result), proposal, history_entry(question, result), calls
