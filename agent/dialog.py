"""Route the active case to diagnostics, measurements or the manuals."""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from .backend import post
from .diagnostic_agent import DiagnosticAgent, MOVEMENT
from .manual_agent import ManualAgent, STRING, schema
from .measurement_agent import MeasurementAgent
from .tools.ticket import prepare, wants_draft


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


def history_entry(question, reply):
    if reply.get("intent") != "conversation":
        return [question, reply["verdict"], reply["clarifying_question"], reply["checks"][:1],
                reply.get("pressure_measurement"), reply.get("fluid_check")]
    return None


class Ollama:
    def ask(self, prompt, format_, trace):
        model = os.getenv("AGENT_MODEL") or os.getenv("OLLAMA_MODEL") or "gemma2:2b"
        url = (os.getenv("OLLAMA_HOST_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/")
        start = time.monotonic()
        result = post(
            f"{url}/api/chat",
            dict(model=model, stream=False, format=format_, options={"temperature": 0},
                 messages=[dict(role="user", content=prompt)]),
            timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")),
        )
        trace["calls"].append(dict(model=result["model"], seconds=time.monotonic() - start,
                                   input_tokens=result.get("prompt_eval_count"),
                                   output_tokens=result.get("eval_count")))
        return json.loads(result["message"]["content"])


class Agent:
    def __init__(self, backend, manuals, provider=None, log="artifacts/agent-turns.jsonl", *, assessment=None):
        self.backend = backend
        self.manuals = manuals
        self.manual_agent = ManualAgent(manuals)
        self.diagnostics = DiagnosticAgent(backend, self.manual_agent, assessment)
        self.measurements = MeasurementAgent(self.manual_agent)
        self.provider = provider or Ollama()
        self.log = Path(log)

    def turn(self, vehicle_id, session_id, question, history=(), *, diagnosis=None, trace_out=None):
        start = time.monotonic()
        trace = dict(id=uuid4().hex, vehicle_id=vehicle_id, session_id=session_id,
                     question=question, tools=[], calls=[])
        try:
            reply = self.diagnostics.observe(vehicle_id, session_id, diagnosis, trace)
            if self.diagnostics.needs_new_check(reply):
                return reply
            return self.respond(reply, question, history, trace)
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

    def respond(self, reply, question, history, trace):
        ml = reply["ml_observations"]
        wording = " ".join(question.casefold().split()).strip(" .!?")
        short = re.sub(r"^(?:(?:ну|так|а|ладно|лад|окей|ок|давай)[ ,]+)+", "", wording)
        short = re.sub(r"\bч[её]\b", "что", short)
        previous = next((row[3][0] for row in reversed(history) if len(row) > 3 and row[3]), None)

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
        movement = bool(MOVEMENT.search(wording))
        explicit_draft = wants_draft(question)
        if not explicit_draft and self.measurements.respond(
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

        followup = bool(FOLLOWUP.fullmatch(short))
        if followup and not previous:
            reply.update(intent="clarify", clarifying_question="Какую проверку нужно объяснить?")
            return reply

        alternate = ""
        guidance_query = " ".join(filter(None, [ml["anomaly_title"], ml["description"]]))
        if explicit_draft:
            action, query = "ticket", guidance_query
        elif next_step:
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

        if action == "ticket" and not explicit_draft:
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
        if ml["status"] == "normal" and (action == "diagnosis" or next_step or explicit_draft):
            if next_step or explicit_draft:
                reply["clarifying_question"] = "Какой симптом или узел нужно проверить дополнительно?"
            return reply

        guided = action in {"diagnosis", "ticket"} or next_step
        if guided and (self.measurements.first_check(reply, trace, history) or self.diagnostics.first_check(reply, trace)):
            reply["intent"] = action
        elif action != "ticket":
            coolant = ml["anomaly_type"] == "F03" and guided
            if coolant:
                query = "Проверять уровень охлаждающей жидкости в расширительном бачке"
            if followup and "уровень охлаждающей жидкости" in previous["text"].lower():
                query = "Уровень жидкости в расширительном бачке должен соответствовать указанному в руководстве по эксплуатации автомобиля"
            self.manual_agent.answer(
                reply, query, question, self.provider, trace, alternate=alternate,
                coolant=coolant, next_step=next_step, previous=previous if followup else None,
            )

        if action == "ticket":
            started = time.monotonic()
            reply["ticket_draft"] = prepare(reply)
            trace["tools"].append(dict(name="prepare_ticket", agent="draft",
                                       seconds=time.monotonic() - started))
            if not reply["ticket_draft"]:
                reply["clarifying_question"] = (
                    "Для черновика нужна подтверждённая руководством проверка. Уточните узел и наблюдаемые симптомы."
                )
        elif not reply["references"]:
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

