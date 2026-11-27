"""Route the mechanic's chat to the available workflow tools."""

from agent import case_context
from agent.tools import oil_pressure
from agent.dialog import incompatible_fault

import logging
import re
from typing import Annotated, Literal

import httpx
from pydantic import Field

from .models import ChatReply, ChatRequest, Input, Pair, Text
from .workflow import Workflow


log = logging.getLogger(__name__)
Tool = Literal["diagnose", "manual_search", "create_draft"]
COMMANDS = {
    "diagnose": re.compile(
        r"(?:запусти(?:те|ть)?|повтори(?:те|ть)?|сделай(?:те)?) (?:проверку|диагностику)"
        r"|получи(?:те|ть)? (?:результат )?диагностики"
        r"|проверь(?:те)? (?:двигатель|запись)"
    ),
    "create_draft": re.compile(
        r"(?:создай(?:те|ть)?|подготовь(?:те)?|подготовить|оформи(?:те|ть)?)"
        r" (?:черновик(?: заявки)?|заявку)(?: на дефектовку| в тоир)?"
        r"|(?:давай(?:те)? )?(?:создадим|оформим) заявку(?: на дефектовку)?"
    ),
}
ACTION_REQUESTS = {
    "diagnose": re.compile(
        r"(?:запусти|повтори|сделай|проведи|получи|проверь|нужно (?:запустить|повторить|провести|получить))"
        r".*\b(?:диагностик\w*|проверк\w*|двигател\w*|запись)\b"
    ),
    "create_draft": re.compile(
        r"(?:созда[йт]|подготов[ьи]|оформ[и]|давай\w* (?:создадим|оформим)|нужно (?:создать|оформить|подготовить))"
        r".*\b(?:черновик\w*|заявк\w*)\b"
    ),
}
FOLLOW_UP = re.compile(
    r"^(?:а\s+)?(?:"
    r"(?:при каких (?:условиях|оборотах)|при каком прогреве)"
    r"(?: (?:проверять|замерять|измерять))?[?.! ]*$|"
    r"(?:при )?(?:минимальных|номинальных|средних|холостых)\b|"
    r"на (?:прогретом|холодном|холостых|каких оборотах)\b|"
    r"(?:что|как|чем|когда|почему)\b.*\b(?:это|этот|его|её|ее|их|этого)\b|"
    r"если (?:так|ниже|выше)\b|какие (?:возможны )?(?:допуски|причины)[?.! ]*$)"
)
SYSTEM = """Ты диспетчер чата механика. Выбери один инструмент.
diagnose — явная команда запустить или повторить диагностику.
create_draft — явная команда создать черновик заявки. Отправка в ТОиР выполняется только через форму.
manual_search — вопрос, уточнение, обсуждение диагноза, запрос технических данных.
Пара машины и сессии уже выбрана. Для manual_search на уточняющем вопросе раскрой предмет вопроса
по предыдущим репликам в короткую поисковую фразу на русском. Сохрани технический смысл текущего
вопроса. Для самостоятельного вопроса верни null в query. Верни только выбор и запрос по схеме."""


class Plan(Input):
    tool: Tool
    query: Annotated[Text, Field(max_length=320)] | None


def command(question: str) -> Tool | None:
    normalized = normalize_command(question)
    return next((name for name, pattern in COMMANDS.items() if pattern.fullmatch(normalized)), None)


def normalize_command(question: str) -> str:
    return question.casefold().strip(" .!").removeprefix("пожалуйста, ")


class Agent:
    def __init__(self, workflow: Workflow, client: httpx.Client, model: str):
        self.workflow = workflow
        self.client = client
        self.model = model

    def plan(self, request: ChatRequest, engine: str) -> Plan | None:
        if not self.model:
            return None
        messages = [{"role": "system", "content": SYSTEM + "\nДвигатель: " + engine}]
        for turn in request.history[-4:]:
            messages.extend((
                {"role": "user", "content": turn.question[:400]},
                {"role": "assistant", "content": turn.answer[:600]},
            ))
        messages.append({"role": "user", "content": request.question})
        try:
            response = self.client.post("/api/chat", json={
                "model": self.model, "messages": messages, "stream": False,
                "format": Plan.model_json_schema(),
                "options": {"temperature": 0, "num_predict": 120},
            })
            response.raise_for_status()
            return Plan.model_validate_json(response.json()["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            log.warning("Ollama routing unavailable (%s)", type(error).__name__)
            return None

    def reply(self, request: ChatRequest) -> ChatReply:
        vehicle = self.workflow.catalog.validate_pair(request)
        pair = Pair(vehicle_id=request.vehicle_id, session_id=request.session_id)
        plan = self.plan(request, vehicle.engine_model)
        tools = {
            "diagnose": lambda: self.diagnose(pair),
            "create_draft": lambda: self.create_draft(pair),
            "manual_search": lambda: self.manual_search(pair, request, vehicle.engine_model, plan),
        }
        normalized = normalize_command(request.question)
        if plan and plan.tool in ACTION_REQUESTS and ACTION_REQUESTS[plan.tool].match(normalized):
            return tools[plan.tool]()
        return tools[command(request.question) or "manual_search"]()

    def diagnose(self, pair: Pair) -> ChatReply:
        result = self.workflow.diagnose(pair)
        assessment = result.assessment
        answer = " ".join(part for part in (assessment.conclusion, assessment.next_step) if part)
        return ChatReply(answer=answer, evidence=assessment.evidence, diagnostic_result=result)

    def create_draft(self, pair: Pair) -> ChatReply:
        with self.workflow.store.connection() as conn:
            diagnosis = self.workflow.diagnosis(conn, pair)
        vehicle = self.workflow.catalog.vehicle(pair.vehicle_id)
        blocked = self.blocked(diagnosis, vehicle)
        if blocked:
            return ChatReply(answer=blocked, needs_clarification=True)
        fields = self.workflow.mechanic.suggest_draft(diagnosis, vehicle.engine_model)
        if diagnosis.status != "anomaly" or not fields.operations or not fields.references:
            return ChatReply(answer=case_context.blocked_message("Нет подтверждённой процедуры.",
                             diagnosis.model_dump(), vehicle.engine_model), needs_clarification=True)
        draft = self.workflow.create_draft(pair)
        if draft.ticket_id:
            answer = f"По этому отклонению уже зарегистрирована заявка {draft.ticket_id}."
        elif draft.state in ("draft", "delivery_failed"):
            answer = "Черновик готов. Проверьте работы и источники и укажите приоритет в форме."
        else:
            answer = "Текущее состояние заявки показано в карточке ТОиР."
        return ChatReply(answer=answer, draft=draft)

    def blocked(self, diagnosis, vehicle):
        fuel = self.workflow.catalog.fuels[vehicle.vehicle_id]
        data = dict(diagnosis.model_dump(), fuel=fuel)
        if data["signal_quality"] == "bad" or data["status"] == "check_failed":
            return case_context.blocked_message("", data, vehicle.engine_model)
        if fuel and incompatible_fault(data):
            reason = f"Код {diagnosis.anomaly_type} «{diagnosis.anomaly_title}» несовместим с топливом «{fuel}»."
            return case_context.blocked_message(reason, data, vehicle.engine_model)
        return None

    def manual_search(self, pair: Pair, request: ChatRequest, engine: str, plan: Plan | None) -> ChatReply:
        with self.workflow.store.connection() as conn:
            diagnosis = self.workflow.diagnosis(conn, pair)
        question = request.question
        mechanic = self.workflow.mechanic
        if blocked := self.blocked(diagnosis, self.workflow.catalog.vehicle(pair.vehicle_id)):
            return ChatReply(answer=blocked, needs_clarification=True)
        topic = case_context.subject(question, [turn.question for turn in request.history])
        reviewed = case_context.reviewed(mechanic.search, engine, topic)
        if reviewed is not None:
            if not reviewed:
                return ChatReply(answer="Уточните исполнение узла и операцию: в индексе отсутствует одна из страниц сопоставления.", needs_clarification=True)
            evidence = [mechanic.evidence(fid, engine).model_copy(update={"text": excerpt}) for fid, excerpt in reviewed]
            return ChatReply(answer="\n".join(f"{item.text} [{item.source.manual}, стр. {item.source.pdf_page}]" for item in evidence), evidence=evidence)
        if case_context.MOVE.search(question):
            readings = {}
            if engine == "ЯМЗ-236М2" and diagnosis.anomaly_type == "F02":
                for utterance in [turn.question for turn in request.history] + [question]:
                    readings = oil_pressure.readings(utterance, readings)
                source = mechanic.evidence(oil_pressure.SOURCE, engine)
                comparison, missing = oil_pressure.compare(readings)
                measured = readings.get("pressure")
                facts = f"Со слов механика: {measured} кПа." if measured else "Фактическое давление не сообщено."
                return ChatReply(answer=" ".join(filter(None, (facts, comparison, missing, source.text,
                    "Рекомендация: очная дефектовка системы смазки. Прямой запрет перегона этой страницей не установлен; допуск к движению не подтверждён."))),
                    needs_clarification=True, evidence=[source])
            return ChatReply(answer=f"Наблюдения ML: {diagnosis.description}. Сообщите показания приборов, режим двигателя и наличие аварийной сигнализации. "
                             "Рекомендуется очная дефектовка. Прямой запрет для этих условий источником не подтверждён.", needs_clarification=True)
        query = None
        if request.history and FOLLOW_UP.search(question.casefold()):
            topic = next((
                turn.question for turn in reversed(request.history)
                if not command(turn.question) and not FOLLOW_UP.search(turn.question.casefold())
            ), None)
            query = topic
            if not topic:
                return ChatReply(answer="Какой именно узел и какой режим проверки вы имеете в виду?", needs_clarification=True)
            # Standalone wording like «какие причины?» refers to the conversation here.
            if query and re.match(r"^(?:а\s+)?какие (?:возможны )?причины", question.casefold()):
                question = "Какие причины описаны для этого признака?"
        return self.workflow.mechanic.answer(question, diagnosis, engine, search_query=query)
