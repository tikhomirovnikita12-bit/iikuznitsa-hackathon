"""Interpret the active check and offer its verified first action."""

import re
import time

from .case_context import diagnostic_observation
from .tools.diagnosis import describe, diagnose


MOVEMENT = re.compile(
    r"перег[но]|своим ходом|(?:можно|могу|можем|разрешено|безопасно|стоит) (?:ли )?"
    r"(?:ехать|поехать|доехать|двигаться|трогаться|ездить|эксплуатировать|выехать|"
    r"продолжать (?:путь|движение|ехать|эксплуатацию))"
    r"|\bдоед(?:у|ем|ет)\b|допуск\w* (?:к движению|к эксплуатации|в рейс)|\bв рейс\b",
    re.I,
)


class DiagnosticAgent:
    def __init__(self, backend, manual_agent, assessment=None):
        self.backend = backend
        self.manual_agent = manual_agent
        self.assessment = assessment

    def observe(self, vehicle_id, session_id, diagnosis, trace):
        started = time.monotonic()
        reply = describe(diagnosis) if diagnosis is not None else diagnose(self.backend, vehicle_id, session_id)
        ml = reply["ml_observations"]
        if (ml["vehicle_id"], ml["session_id"]) != (vehicle_id, session_id):
            raise ValueError("Ответ диагностики относится к другой машине или сессии")
        trace["tools"].append(dict(name="diagnose", agent="diagnostics",
                                   source="session" if diagnosis is not None else "backend",
                                   seconds=time.monotonic() - started))
        return reply

    def first_check(self, reply, trace):
        ml = reply["ml_observations"]
        if ml["status"] != "anomaly":
            return False
        started = time.monotonic()
        engine = ml["engine_model"]
        manuals = self.manual_agent.manuals
        approved = reply.get("admission", {}).get("approved_operations", [])
        check = manuals.fault_checks.get(engine, {}).get(ml["anomaly_type"])
        if approved and approved[0]["fragment_id"] in manuals.searcher.fragments:
            fragment, instruction = approved[0]["fragment_id"], approved[0]["operation"]
        elif check:
            fragment, instruction, _ = check
        elif self.assessment:
            assessment = self.assessment(ml)
            if not assessment.checks or len(assessment.evidence) != 1:
                return False
            fragment = assessment.evidence[0].source.fragment_id
            instruction = assessment.checks[0]
        else:
            return False
        self.manual_agent.cite(reply, "checks", instruction, fragment)
        if ml["anomaly_type"] in {"F03", "F04"} and not reply["clarifying_question"]:
            reply["clarifying_question"] = diagnostic_observation(ml["anomaly_type"])
        ref = reply["references"][-1]
        trace["tools"].append(dict(name="diagnostics.first_check", agent="diagnostics",
                                   seconds=time.monotonic() - started,
                                   pages=[(ref["manual"], ref["pdf_page"])]))
        return True

    def movement(self, reply, trace):
        reply.update(intent="movement",
                     verdict="Возможность движения по результату записи не подтверждена.")
        if not self.first_check(reply, trace):
            reply["clarifying_question"] = "Укажите наблюдаемый симптом и результаты очной проверки машины."
        return reply
