from agent.dialog import Agent, history_entry
from agent.tools.manual import Manuals
from backend.mechanic import Mechanic
from backend.models import Diagnosis as BackendDiagnosis
from backend.admission import AdmissionPolicy, evaluate_admission

from .contracts import DraftFields, Reference, Reply, Section
from .config import MATERIALS
from .demo_agent import wants_draft


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

    def chat(self, vehicle, diagnosis, question, history):
        context = {**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]}
        fallback = self.checked_draft(vehicle, diagnosis)
        if wants_draft(question):
            return (self.checked_reply(question, fallback, vehicle, diagnosis) if fallback
                    else self.blocked_reply(question, vehicle, diagnosis))
        result = self.agent.turn(diagnosis.vehicle_id, diagnosis.session_id, question, history,
                                 diagnosis=context)
        if result.get("intent") == "ticket":
            return (self.checked_reply(question, fallback, vehicle, diagnosis) if fallback
                    else self.blocked_reply(question, vehicle, diagnosis))
        references = {
            item["fragment_id"]: Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
        }
        sections = [Section(kind="signs", items=[diagnosis.description])]
        for field, kind in (("possible_causes", "causes"), ("risk", "risk"), ("checks", "checks")):
            if items := result[field]:
                ids = dict.fromkeys(item["fragment_id"] for item in items)
                sections.append(Section(
                    kind=kind, items=[("Гипотеза: " if kind in {"causes", "risk"} else "") + item["text"] for item in items],
                    references=[references[fragment] for fragment in ids],
                ))
        if result.get("movement_limitations"):
            sections.append(Section(kind="clarify", items=[result["movement_limitations"]]))
        if question_to_clarify := result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=[question_to_clarify],
                                    references=list(references.values()) if not any(section.references for section in sections) else []))

        summary = result.get("explanation") or result["verdict"]
        return Reply(summary=summary, sections=sections), None, history_entry(question, result)
