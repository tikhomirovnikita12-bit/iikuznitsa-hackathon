from agent.dialog import Agent, history_entry
from agent.tools.manual import Manuals

from .contracts import DraftFields, Reference, Reply, Section


class CaseAgent:
    def __init__(self, corpus, log):
        self.agent = Agent(backend=None, manuals=Manuals(corpus), log=log)

    def chat(self, vehicle, diagnosis, question, history):
        result = self.agent.turn(
            diagnosis.vehicle_id, diagnosis.session_id, question, history,
            diagnosis={**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]},
        )
        references = {
            item["fragment_id"]: Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
        }
        sections = []
        for field, kind in (("possible_causes", "causes"), ("risk", "risk"), ("checks", "checks")):
            if items := result[field]:
                ids = dict.fromkeys(item["fragment_id"] for item in items)
                sections.append(Section(
                    kind=kind, items=[item["text"] for item in items],
                    references=[references[fragment] for fragment in ids],
                ))
        if question_to_clarify := result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=[question_to_clarify],
                                    references=[] if sections else list(references.values())))

        summary = result.get("explanation") or result["verdict"]
        draft = result["ticket_draft"]
        proposal = None
        if draft:
            proposal = DraftFields.model_validate({key: draft[key] for key in DraftFields.model_fields})
            summary = "Черновик подготовлен. Проверьте поля и подтвердите приоритет."
        return Reply(summary=summary, sections=sections), proposal, history_entry(question, result)
