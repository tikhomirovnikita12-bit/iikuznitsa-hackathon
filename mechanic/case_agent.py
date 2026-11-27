from agent.dialog import Agent, history_entry
from agent.tools.manual import Manuals

from .sources import BY_NAME
from .contracts import DraftFields, Reference, Reply, Section


class CaseAgent:
    def __init__(self, corpus, log, *, assessment=None):
        self.agent = Agent(backend=None, manuals=Manuals(corpus), log=log, assessment=assessment)

    def chat(self, vehicle, diagnosis, question, history):
        trace = {}
        result = self.agent.turn(
            diagnosis.vehicle_id, diagnosis.session_id, question, history, trace_out=trace,
            diagnosis={**diagnosis.model_dump(), "engine_model": vehicle["engine_model"], "fuel": vehicle["fuel"]},
        )
        references = {
            item["fragment_id"]: Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
        }
        for item in result["references"]:
            manual = BY_NAME[item["manual"]]
            item["url"] = f"/sources/{manual.slug}/{item['pdf_page']}"
        sections = []
        for field, kind in (("possible_causes", "causes"), ("risk", "risk"), ("checks", "checks")):
            for item in result[field]:
                sections.append(Section(kind=kind, items=[item["text"]], references=[references[item["fragment_id"]]]))
        if question_to_clarify := result["clarifying_question"]:
            sections.append(Section(kind="clarify", items=[question_to_clarify],
                                    references=[] if sections else list(references.values())))

        summary = result.get("explanation")
        if not summary and result.get("intent") not in {"diagnosis", "movement"}:
            lead = next((item["text"] for field in ("checks", "possible_causes", "risk") for item in result[field]), None)
            if diagnosis.status != "check_failed":
                summary = lead or result["clarifying_question"]
        summary = summary or result["verdict"]
        draft = result["ticket_draft"]
        proposal = None
        if draft:
            proposal = DraftFields.model_validate({key: draft[key] for key in DraftFields.model_fields})
            summary = "Черновик подготовлен. Проверьте поля и подтвердите приоритет."
        calls = []
        for tool in trace["tools"]:
            calls.append(dict(tool="agent." + tool["name"], duration_ms=round(tool["seconds"] * 1000),
                              status="ok", pages=tool.get("pages", [])))
        for call in trace["calls"]:
            calls.append(dict(tool="ollama.chat", duration_ms=round(call["seconds"] * 1000), status="ok",
                              model=call["model"], input_tokens=call["input_tokens"], output_tokens=call["output_tokens"]))
        return Reply(summary=summary, sections=sections, analysis=result), proposal, history_entry(question, result), calls
