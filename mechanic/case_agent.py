from time import perf_counter
from urllib.error import URLError

from agent.tools.diagnosis import describe
from agent.dialog import Agent, history_entry
from agent.tools.manual import Manuals
from backend.mechanic import Mechanic
from backend.models import Diagnosis as BackendDiagnosis
from backend.admission import AdmissionPolicy, evaluate_admission

from .sources import BY_NAME
from .contracts import DraftFields, Reference, Reply, Section
from .config import MATERIALS
from agent.tools.ticket import wants_draft


class CaseAgent:
    def __init__(self, corpus, log, *, mechanic=None):
        self.mechanic = mechanic if mechanic is not None else Mechanic(corpus, MATERIALS)
        self.agent = Agent(backend=None, manuals=Manuals(corpus), log=log, mechanic=self.mechanic)

    def checked_draft(self, vehicle, diagnosis):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        admission = evaluate_admission(vehicle, context, mechanic=self.mechanic)
        if not admission["can_prepare"]:
            return None
        backend_diagnosis = BackendDiagnosis.model_validate(diagnosis.model_dump())
        fields = self.mechanic.suggest_draft(backend_diagnosis, vehicle["engine_model"])
        if not fields.operations or not fields.references or any(
            not self.mechanic.reference_applies(reference, vehicle["engine_model"]) for reference in fields.references
        ):
            return None
        evidence = [self.mechanic.evidence(row["fragment_id"], vehicle["engine_model"])
                    for row in AdmissionPolicy(self.mechanic).approved_operations(backend_diagnosis, vehicle["engine_model"])]
        if not evidence or any(not self.mechanic.pdf_paths[
            self.mechanic.search.fragments[item.source.fragment_id]["manual_id"]
        ].is_file() for item in evidence):
            return None
        return DraftFields.model_validate({**fields.model_dump(), "urgency": fields.urgency or "medium"})

    def checked_reply(self, question, fields, vehicle, diagnosis):
        summary = "Черновик подготовлен по проверенной операции руководства. Проверьте применимость и приоритет."
        reply = Reply(summary=summary, sections=[Section(kind="signs", items=[diagnosis.description]),
            Section(kind="checks", items=fields.operations, references=fields.references)])
        evidence = [self.mechanic.evidence(row["fragment_id"], vehicle["engine_model"])
                    for row in AdmissionPolicy(self.mechanic).approved_operations(diagnosis.model_dump(), vehicle["engine_model"])]
        first = fields.operations[0]
        source = evidence[0].source if evidence else None
        entry = history_entry(question, {
            "intent": "ticket", "verdict": summary + " " + " ".join(fields.operations),
            "clarifying_question": None,
            "checks": [{"text": first, "fragment_id": source.fragment_id if source else None}],
        })
        return reply, fields, entry

    def can_prepare(self, vehicle, diagnosis, history=()):
        return bool(self.checked_draft(vehicle, diagnosis))

    def blocked_reply(self, question, vehicle, diagnosis):
        admission = evaluate_admission(vehicle, diagnosis.model_dump(), mechanic=self.mechanic)
        reason = admission.get("block_reason") or {}
        summary = reason.get("message", "Для заявки пока нет подтверждённой процедуры.")
        next_action = reason.get("next_action", "Передайте узел на очную дефектовку и уточните применимую процедуру.")
        reply = Reply(summary=summary, sections=[
            Section(kind="signs", items=[diagnosis.description]),
            Section(kind="clarify", items=[next_action]),
        ])
        entry = history_entry(question, {"intent": "clarify", "verdict": summary,
                                        "clarifying_question": next_action, "checks": []})
        return reply, None, entry

    def draft_reply(self, question, vehicle, diagnosis):
        fields = self.checked_draft(vehicle, diagnosis)
        if fields:
            return self.checked_reply(question, fields, vehicle, diagnosis)
        return self.blocked_reply(question, vehicle, diagnosis)

    def chat(self, vehicle, diagnosis, question, history):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        if wants_draft(question):
            started = perf_counter()
            reply, fields, entry = self.draft_reply(question, vehicle, diagnosis)
            elapsed = round((perf_counter() - started) * 1000)
            calls = []
            if fields:
                pages = [(ref.manual, ref.pdf_page) for ref in fields.references]
                calls = [
                    dict(tool="agent.search", duration_ms=elapsed, status="ok", pages=pages),
                    dict(tool="agent.prepare_ticket", duration_ms=0, status="ok"),
                ]
            return reply, fields, entry, calls
        trace = {}
        try:
            result = self.agent.turn(diagnosis.vehicle_id, diagnosis.session_id, question, history,
                                     trace_out=trace, diagnosis=context)
        except (URLError, TimeoutError):
            if question != "Покажи диагностику":
                raise
            result = describe(context)
            result.update(intent="clarify", clarifying_question="Языковая модель недоступна. Уточните узел и режим измерения.")
            started = perf_counter()
            assessment = self.mechanic.assess(BackendDiagnosis.model_validate(diagnosis.model_dump()), vehicle["engine_model"])
            for evidence in assessment.evidence:
                source = evidence.source
                result["references"].append(source.model_dump())
                result["checks"].append(dict(text=evidence.text, fragment_id=source.fragment_id))
            trace.setdefault("tools", []).append(dict(name="search", seconds=perf_counter() - started,
                                                     pages=[(e.source.manual, e.source.pdf_page) for e in assessment.evidence]))
        calls = []
        for tool in trace["tools"]:
            calls.append(dict(tool="agent." + tool["name"], duration_ms=round(tool.get("seconds", 0) * 1000),
                              status="ok", pages=tool.get("pages", [])))
        for call in trace.get("calls", ()):
            calls.append(dict(tool="ollama.chat", duration_ms=round(call["seconds"] * 1000), status=call.get("status", "ok"),
                              model=call["model"], input_tokens=call["input_tokens"], output_tokens=call["output_tokens"]))
        result["ticket_draft"] = None
        references = {
            item["fragment_id"]: Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
        }
        for item in result["references"]:
            manual = BY_NAME[item["manual"]]
            item["url"] = f"/sources/{manual.slug}/{item['pdf_page']}"
        sections = [Section(kind="signs", items=[diagnosis.description])]
        for field, kind in (("possible_causes", "causes"), ("risk", "risk"), ("checks", "checks")):
            prefix = "Гипотеза: " if kind in {"causes", "risk"} else ""
            for item in result[field]:
                sections.append(Section(kind=kind, items=[prefix + item["text"]],
                                        references=[references[item["fragment_id"]]]))
        if result.get("movement_limitations"):
            sections.append(Section(kind="clarify", items=[result["movement_limitations"]]))
        if question_to_clarify := result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=[question_to_clarify],
                                    references=list(references.values()) if not any(section.references for section in sections) else []))
        if result["checks"] and not result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=["Применимость к машине требует сверки исполнения узла и условий проверки."]))

        summary = result.get("explanation")
        if not summary and result.get("intent") not in {"diagnosis", "movement"}:
            lead = next((item["text"] for field in ("checks", "possible_causes", "risk") for item in result[field]), None)
            if diagnosis.status != "check_failed":
                summary = lead or result["clarifying_question"]
        summary = summary or result["verdict"]
        return Reply(summary=summary, sections=sections, analysis=result), None, history_entry(question, result), calls
