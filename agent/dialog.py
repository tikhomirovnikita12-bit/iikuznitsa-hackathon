"""Route the active case to diagnostics, measurements or the manuals."""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from backend.admission import evaluate_admission, incompatible_fault
from backend.mechanic import conflict_case

from . import case_context
from .backend import post
from .diagnostic_agent import DiagnosticAgent, MOVEMENT
from .generation import answer_request, read_answer
from .manual_agent import ManualAgent, STRING, schema
from .measurement_agent import MeasurementAgent
from .tools.ticket import wants_draft


TERM = re.compile(r"(?:а )?(?:что такое|что означает|что значит) ([а-яёa-z][а-яёa-z -]{1,55})")
NEXT_STEP = re.compile(
    r"(?:(?:(?:сам )?(?:мне )?(?:скажи|подскажи|расскажи) )?"
    r"что (?:(?:мне(?:-то)?|надо|нужно|теперь|дальше) )*"
    r"(?:делать|сделать|проверить|посмотреть)(?: (?:надо|нужно|дальше|сначала))?"
    r"|скажи как делать|с чего начать|(?:подскажи|посоветуй)(?: мне)?|что дальше)"
)
FOLLOWUP = re.compile(r"(?:подскажи |скажи )?как (?:именно |это |мне )*(?:сделать|проверить|делать|выполнить)")
EXPLAIN_RESULT = re.compile(
    r"покажи (?:результат(?: диагностики)?|диагностику)|"
    r"(?:объясни|поясни) (?:результат(?: диагностики)?|диагностику)|"
    r"что (?:случилось|произошло|не так)(?: с машиной)?"
)
METHOD = re.compile(r"^(?:как (?:проверить|измерить|замерить|сверить|отрегулировать) |на каких оборотах )")
COOLANT_NORM = re.compile(
    r"(?:какая|какой|каков\w*) (?:норма|норматив|температура).*охлаждающ|"
    r"охлаждающ\w*.*(?:температура|норматив)"
)
REPAIR = re.compile(
    r"(?:какие|что) (?:нужно )?(?:работы|заменить|ремонтировать|устранить)|"
    r"как (?:устранить|отремонтировать|заменить)"
)
COOLANT_TEMPERATURE = "yamz-236-3902150-b-re-p004-coolant-temperature"
DRAFT_HELP = re.compile(
    r"^(?:(?:объясни|расскажи|подскажи)(?: мне)?,? )?(?:как |можно ли |не (?:нужно |надо )?)"
    r"(?:созда\w*|оформ\w*|подготов\w*|отправ\w*|сдела\w*)\b.*\b(?:заявк\w*|черновик\w*|наряд\w*)\b"
)
PLAN = schema(
    action={"type": "string", "enum": [
        "diagnosis", "search", "ticket", "movement", "clarify", "off_topic", "next_step", "pause", "repair",
    ]},
    query=STRING,
)

OIL_NORM_QUESTION = re.compile(
    r"^(?:а\s+)?(?:какое\s+(?:должно\s+быть\s+)?давление\s+масла|"
    r"каким\s+должно\s+быть\s+давление\s+масла|какова\s+норма\s+давления\s+масла|"
    r"норма\s+давления\s+масла)\b"
)


def history_entry(question, reply):
    if reply.get("intent") != "conversation":
        return [question, reply["verdict"], reply["clarifying_question"], reply["checks"][:1],
                reply.get("pressure_measurement"), reply.get("fluid_check")]
    return None


class Ollama:
    def ask(self, prompt, format_, trace):
        model = os.getenv("AGENT_MODEL") or os.getenv("OLLAMA_MODEL") or "gemma2:2b"
        body = dict(model=model, stream=False, format=format_, options={"temperature": 0},
                    messages=[dict(role="user", content=prompt)])
        return json.loads(self.request(body, trace)["message"]["content"])

    def summarize(self, model, question, query, engine, excerpts, trace):
        try:
            return read_answer(self.request(answer_request(model, question, query, engine, excerpts), trace))
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def request(self, body, trace):
        model = body["model"]
        base = os.getenv("OLLAMA_HOST_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434"
        start = time.monotonic()
        try:
            result = post(base.rstrip("/") + "/api/chat", body, timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")))
        except (OSError, ValueError):
            trace["calls"].append(dict(model=model, seconds=time.monotonic()-start, status="error",
                                       input_tokens=None, output_tokens=None))
            raise
        trace["calls"].append(dict(model=result["model"], seconds=time.monotonic()-start,
                                   input_tokens=result.get("prompt_eval_count"),
                                   output_tokens=result.get("eval_count")))
        return result

class Agent:
    def __init__(self, backend, manuals, provider=None, log="artifacts/agent-turns.jsonl", *, mechanic=None):
        self.backend = backend
        self.manuals = manuals
        self.mechanic = mechanic
        self.manual_agent = ManualAgent(manuals)
        self.diagnostics = DiagnosticAgent(backend, self.manual_agent, self.assess)
        self.measurements = MeasurementAgent(self.manual_agent)
        self.provider = provider or Ollama()
        self.log = Path(log)

    def merge_assessment(self, reply, assessment):
        """Attach reviewed risk, causes and checks without replacing agent evidence."""
        if not assessment or not assessment.evidence:
            return
        source = assessment.evidence[0].source
        reference = source.model_dump()
        for field, values in (
            ("risk", [assessment.risk] if assessment.risk else []),
            ("possible_causes", assessment.possible_causes),
            ("checks", assessment.checks),
            ("repair_actions", assessment.repair_actions),
        ):
            reply.setdefault(field, [])
            for text in values:
                if not text or any(item.get("text") == text for item in reply[field]):
                    continue
                reply[field].append(dict(text=text, fragment_id=source.fragment_id))
        if reference not in reply["references"]:
            reply["references"].append(reference)
        reply["risk_level"] = assessment.risk_level

    def turn(self, vehicle_id, session_id, question, history=(), *, diagnosis=None, trace_out=None):
        start = time.monotonic()
        trace = dict(id=uuid4().hex, vehicle_id=vehicle_id, session_id=session_id,
                     question=question, tools=[], calls=[])
        try:
            reply = self.diagnostics.observe(vehicle_id, session_id, diagnosis, trace)
            ml = reply["ml_observations"]
            admission_started = time.monotonic()
            decision = self.admission(ml)
            trace["tools"].append(dict(name="admission", seconds=time.monotonic() - admission_started,
                                       can_prepare=decision["can_prepare"],
                                       block_code=(decision.get("block_reason") or {}).get("code")))
            reply["admission"] = decision
            if ml["status"] == "check_failed" or ml["signal_quality"] == "bad" or incompatible_fault(ml):
                reason = decision["block_reason"]
                message = case_context.blocked_message(reason["message"], ml, ml["engine_model"])
                reply.update(explanation=message, clarifying_question=reason["next_action"])
                return reply
            reply = self.respond(reply, question, history, trace)
            self.summarize_manual_search(reply, question, history, trace)
            if self.mechanic and ml["status"] == "anomaly" and ml["anomaly_type"] == "F03":
                self.merge_assessment(reply, self.assess(ml))
            return reply
        except Exception as exc:
            trace["error"] = type(exc).__name__
            raise
        finally:
            trace["seconds"] = time.monotonic() - start
            if trace_out is not None:
                trace_out.update(trace)
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(trace, ensure_ascii=False) + "\n")

    def summarize_manual_search(self, reply, question, history, trace):
        model = os.getenv("OLLAMA_GENERATION_MODEL", "").strip()
        if (not model or not isinstance(self.provider, Ollama) or reply["intent"] != "search"
                or reply["clarifying_question"] or reply.get("explanation")
                or reply.get("pressure_measurement") or reply.get("fluid_check")):
            return
        cited = {source["fragment_id"] for source in reply["references"]}
        excerpts = [{"text": check["text"]} for check in reply["checks"] if check.get("fragment_id") in cited]
        if not excerpts:
            return
        query = case_context.subject(question, [turn[0] for turn in history])
        summary = self.provider.summarize(model, question, query, reply["ml_observations"]["engine_model"], excerpts, trace)
        if summary:
            reply["explanation"] = summary

    def respond(self, reply, question, history, trace):
        ml = reply["ml_observations"]
        wording = " ".join(question.casefold().split()).strip(" .!?")
        short = re.sub(r"^(?:(?:ну|так|а|ладно|лад|окей|ок|давай)[ ,]+)+", "", wording)
        short = re.sub(r"\bч[её]\b", "что", short)
        previous = next((row[3][0] for row in reversed(history) if len(row) > 3 and row[3]), None)

        if COOLANT_NORM.search(short):
            hits = self.manual_agent.search("температура охлаждающей жидкости норма", ml, trace)
            source = next((hit for hit in hits if hit["fragment_id"] == COOLANT_TEMPERATURE), None)
            if source:
                reply["intent"] = "search"
                self.manual_agent.cite(reply, "checks", source["text"], source["fragment_id"])
                return reply

        if wording in {"привет", "здравствуйте", "добрый день", "спасибо", "понятно"}:
            reply.update(intent="conversation", explanation=(
                "Пожалуйста." if wording in {"спасибо", "понятно"} else
                "Привет! Могу подсказать проверку, разобрать замер или найти раздел руководства."
            ))
            return reply

        next_step = bool(NEXT_STEP.fullmatch(short)) or (
            short.startswith("не ") and bool(DRAFT_HELP.match(short))
            and bool(re.search(r"[,;] (?:скажи,? |подскажи,? )?что проверить(?: сначала)?$", short))
        )
        if REPAIR.search(short) and ml["anomaly_type"] == "F03" and self.mechanic:
            self.merge_assessment(reply, self.assess(ml))
            reply.update(intent="search", explanation="Возможные работы по руководству.")
            return reply
        movement = bool(MOVEMENT.search(wording))
        if wants_draft(question):
            return self.reviewed_ticket(reply, trace)
        if self.measurements.respond(
            reply, question, short, history, trace, movement=movement, next_step=next_step,
        ):
            return reply
        if movement:
            return self.diagnostics.movement(reply, trace)
        if short in {"достаточно", "хватит", "стоп", "на этом всё", "на этом все"}:
            reply.update(intent="conversation", explanation="Хорошо, остановимся.")
            return reply
        if re.fullmatch(r"ты (?:что )?(?:охуел|ахуел|охренел|издеваешься)|ответь нормально", short):
            reply.update(intent="conversation", explanation="Предыдущий ответ запутал диалог. Можем вернуться к открытой проверке.")
            return reply

        topic = case_context.subject(question, [turn[0] for turn in history])
        if (self.conflict_reply(reply, topic, trace) or self.reviewed_oil_pressure(reply, topic, trace)
                or self.reviewed_norm(reply, topic, trace)):
            return reply

        followup = bool(FOLLOWUP.fullmatch(short))
        if followup and not previous:
            reply.update(intent="clarify", clarifying_question="Какую проверку нужно объяснить?")
            return reply

        alternate = ""
        guidance_query = " ".join(filter(None, [ml["anomaly_title"], ml["description"]]))
        if next_step:
            action, query = "search", guidance_query
        elif EXPLAIN_RESULT.fullmatch(short):
            action, query = "diagnosis", guidance_query
        elif DRAFT_HELP.match(short):
            action, query = "ticket", ""
        elif term := TERM.fullmatch(wording):
            action, query = "search", term[1]
        elif followup:
            action, query = "search", previous["text"]
        elif METHOD.match(short):
            action, query = "search", question
        else:
            plan = self.plan(ml, history, question, trace)
            action, query = plan["action"], plan["query"] or question
            if action == "search" and len(question.split()) >= 5:
                query, alternate = question, plan["query"]
            if action == "next_step":
                next_step = True
                action, query = "search", guidance_query
                if self.measurements.respond(reply, question, short, history, trace, next_step=True):
                    return reply
            if action == "movement":
                if self.measurements.respond(reply, question, short, history, trace, movement=True):
                    return reply
                return self.diagnostics.movement(reply, trace)
            if action == "clarify":
                reply.update(intent="clarify", clarifying_question=(
                    "Какой узел, наблюдаемый симптом или режим работы нужно проверить?"
                ))
                return reply

        if action == "ticket":
            reply.update(intent="conversation", explanation=(
                "Кнопка «Подготовить заявку» открывает черновик по текущему отклонению. "
                "Работы и приоритет можно проверить в форме перед отправкой."
            ))
            return reply
        if action in {"off_topic", "pause", "repair"}:
            explanation = {
                "off_topic": "Могу помочь с проверкой этой машины, замером или поиском по руководству.",
                "pause": "Хорошо, остановимся.",
                "repair": "Можем вернуться к текущей проверке. Что нужно уточнить?",
            }[action]
            reply.update(intent="conversation", explanation=explanation)
            return reply

        reply["intent"] = action
        if ml["status"] == "normal" and (action == "diagnosis" or next_step):
            if next_step:
                reply["clarifying_question"] = "Какой симптом или узел нужно проверить дополнительно?"
            return reply

        guided = action == "diagnosis" or next_step
        if guided and (self.measurements.first_check(reply, trace, history) or self.diagnostics.first_check(reply, trace)):
            reply["intent"] = action
        else:
            coolant = ml["anomaly_type"] == "F03" and guided
            if coolant:
                query = "Проверять уровень охлаждающей жидкости в расширительном бачке"
            if followup and "уровень охлаждающей жидкости" in previous["text"].lower():
                query = "Уровень жидкости в расширительном бачке должен соответствовать указанному в руководстве по эксплуатации автомобиля"
            self.manual_agent.answer(
                reply, query, question, self.provider, trace, alternate=alternate,
                coolant=coolant, next_step=next_step, previous=previous if followup else None,
            )

        if not reply["references"]:
            reply["clarifying_question"] = (
                "В руководстве этого двигателя нет подтверждения. Уточните узел и условия проверки. "
                "Если данных недостаточно, передайте машину на дефектовку."
            )
        return reply

    def plan(self, ml, history, question, trace):
        context = [ml["engine_model"], ml["anomaly_title"], [row[0] for row in history[-2:]]]
        return self.provider.ask(
            "Классифицируй только новую реплику механика. Используй контекст, чтобы понять краткое уточнение. "
            "Повседневный разговор, фильмы, знаменитости, погода и фразы о владельце автомобиля — off_topic, "
            "даже если названа марка. «У Киану Ривза Ford Mustang» — off_topic. «Ford Mustang стучит» — search. "
            "search — технический вопрос, query — запрос к руководству; diagnosis — объяснить результат; "
            "next_step — что делать с текущей проверкой; movement — можно ли ехать; clarify — не хватает узла или режима; "
            "ticket — вопрос о заявке; pause — завершить разговор; repair — недовольство ответом. "
            "Контекст: " + json.dumps(context, ensure_ascii=False) +
            "\nНовая реплика: " + json.dumps(question, ensure_ascii=False),
            PLAN, trace,
        )



    def admission(self, ml):
        if self.mechanic is None:
            from backend.mechanic import Mechanic
            root = Path(__file__).resolve().parents[1]
            materials = getattr(self.backend, "materials", root / "artifacts/manuals")
            self.mechanic = Mechanic(root / "artifacts/manual-corpus.json", materials)
        return evaluate_admission(ml, ml, mechanic=self.mechanic)

    def reviewed_ticket(self, reply, trace):
        from backend.models import Diagnosis

        ml = reply["ml_observations"]
        admission = reply["admission"]
        if not admission["can_prepare"]:
            reason = admission["block_reason"]
            reply.update(intent="clarify", explanation=reason["message"], clarifying_question=reason["next_action"])
            return reply
        started = time.monotonic()
        fields = self.mechanic.suggest_draft(Diagnosis.model_validate(ml), ml["engine_model"])
        operations = admission["approved_operations"]
        fragments = dict.fromkeys(fragment for item in operations
                                  for fragment in [item["fragment_id"], *item.get("supporting_fragment_ids", [])])
        reply.update(intent="ticket", checks=[dict(text=item["operation"], fragment_id=item["fragment_id"])
            for item in operations], references=[self.manuals.reference(fragment, ml["engine_model"])
                                                for fragment in fragments],
            ticket_draft=dict(vehicle_id=ml["vehicle_id"], session_id=ml["session_id"],
                              anomaly_type=ml["anomaly_type"], **fields.model_dump()))
        reply["clarifying_question"] = "Проверьте условия применимости работ и подтвердите приоритет в основном экране."
        trace["tools"].append(dict(name="prepare_ticket", seconds=time.monotonic() - started,
                                   operation_ids=[item["id"] for item in operations]))
        return reply

    def conflict_reply(self, reply, question, trace):
        started = time.monotonic()
        ml = reply["ml_observations"]
        case = conflict_case(question, ml["engine_model"])
        if not case:
            return False
        fragments = self.manuals.searcher.fragments
        if any(fragment not in fragments for fragment in case["fragment_ids"]):
            reply.update(intent="clarify", clarifying_question="Сверенный набор источников неполон. Уточните процедуру перед выполнением работ.")
            return True
        references = [self.manuals.reference(fragment, ml["engine_model"]) for fragment in case["fragment_ids"]]
        reply.update(intent="search", explanation=case["explanation"],
                     checks=[dict(text=fragments[fragment]["text"], fragment_id=fragment)
                             for fragment in case["fragment_ids"]], references=references,
                     clarifying_question=("Ограничения: " + case["limitations"]) if case.get("limitations") else None)
        trace["tools"].append(dict(name="reviewed_conflict", query=question, conflict_id=case["id"],
            seconds=time.monotonic() - started,
            pages=[(item["manual"], item["pdf_page"]) for item in references]))
        return True

    def reviewed_oil_pressure(self, reply, topic, trace):
        ml = reply["ml_observations"]
        started = time.monotonic()
        index = getattr(self.manuals, "searcher", None)
        matched = case_context.reviewed_oil_pressure(index, ml["engine_model"], topic) if index else None
        if matched is None:
            return False
        reply["intent"] = "search"
        for fragment, excerpt in matched:
            reply["checks"].append(dict(text=excerpt, fragment_id=fragment))
            reply["references"].append(self.manuals.reference(fragment, ml["engine_model"]))
        if not matched:
            reply["clarifying_question"] = "В индексе отсутствует страница для сверки нормы. Уточните условия замера."
        trace["tools"].append(dict(name="search", seconds=time.monotonic() - started, query=topic,
                                   pages=[(item["manual"], item["pdf_page"]) for item in reply["references"]]))
        return True

    def assess(self, observations):
        from backend.models import Diagnosis

        return self.mechanic.assess(Diagnosis.model_validate(observations), observations["engine_model"])

    def reviewed_norm(self, reply, topic, trace):
        if not OIL_NORM_QUESTION.match(topic.casefold()):
            return False
        ml = reply["ml_observations"]
        hits = self.manual_agent.search(topic, ml, trace)
        source = next((hit for hit in hits if hit.get("verified")
                       and hit["fragment_id"].endswith("-oil-pressure")), None)
        if not source:
            return False
        reply["intent"] = "search"
        self.manual_agent.cite(reply, "checks", source["text"], source["fragment_id"])
        for fragment_id in source.get("supporting_fragments", ()):
            related = self.manuals.searcher.fragments.get(fragment_id)
            if related and ml["engine_model"] in related["applicable_engine_models"]:
                self.manual_agent.cite(reply, "checks", related["text"], fragment_id)
        return True
