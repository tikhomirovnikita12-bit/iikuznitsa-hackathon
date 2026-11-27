import json
import os
import re
from contextlib import asynccontextmanager, contextmanager
from time import perf_counter, time
from typing import Annotated
from urllib.error import URLError
from uuid import uuid4

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from backend.admission import evaluate_admission, diagnosis_revision
from backend.mechanic import Mechanic as EvidenceMechanic, conflict_case

from . import demo_agent, generator_check, sources
from agent import case_context
from agent.dialog import incompatible_fault
from .case_agent import CaseAgent
from agent.dialog import history_entry
from agent.tools.ticket import wants_draft
from .config import CORPUS, DEFAULT_ML, DEFAULT_TOIR, MATERIALS, MOCKS, ROOT, WEB, endpoint
from .contracts import (
    Brief, ChatRequest, Diagnosis, DraftFields, DraftUpdate, Identifier, Pair, Reference, Reply, Section, TicketReceipt,
    TicketReconciliation, TicketRequest, TicketSubmit, ToolCall,
)
from .retrieval import SEARCH_QUESTIONS, key, load_retrieval
from .state import UNKNOWN, Store, empty_state, public_state, record_call
from .team_workflow import TeamWorkflow, team_checkout


ML_URL = endpoint("MOCK_ML_URL", DEFAULT_ML)
TOIR_URL = endpoint("MOCK_TOIR_URL", DEFAULT_TOIR)
TEAM_API_URL = endpoint("TEAM_API_URL")
TEAM_AGENT_URL = endpoint("TEAM_AGENT_URL")
MOCK_TIMEOUT = float(os.environ.get("MOCK_API_TIMEOUT", "5"))
TEAM_TIMEOUT = float(os.environ.get("TEAM_API_TIMEOUT", "20"))
LOW_CONFIDENCE = float(os.environ.get("LOW_CONFIDENCE_THRESHOLD", "0.60"))
if not 0 <= LOW_CONFIDENCE <= 1 or not 0 < MOCK_TIMEOUT <= 30 or not 0 < TEAM_TIMEOUT <= 40:
    raise ValueError("Проверьте настройку порога уверенности и тайм-аутов")

CATALOG_PATH = ROOT / os.environ.get("MOCK_DATA_PATH", "data/vehicles.json")
VEHICLES = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["vehicles"]
fault_types = MOCKS / "data" / "fault_types.json"
FAULTS = (
    {row["code"]: row["title"] for row in json.loads(fault_types.read_text(encoding="utf-8"))}
    if fault_types.is_file() else
    {session["anomaly_type"]: session["anomaly_title"] for vehicle in VEHICLES for session in vehicle["sessions"]
     if session["anomaly_type"] and session["anomaly_title"]}
)
VEHICLE_BY_ID = {vehicle["vehicle_id"]: vehicle for vehicle in VEHICLES}
CATALOG = [
    {**{key: value for key, value in vehicle.items() if key != "sessions"},
     "sessions": [{key: session[key] for key in ("session_id", "checked_at")}
                  for session in vehicle["sessions"]]}
    for vehicle in sorted(VEHICLES, key=lambda vehicle: vehicle["vehicle_id"])
]
SESSION_KEYS = {(vehicle["vehicle_id"], session["session_id"])
                for vehicle in VEHICLES for session in vehicle["sessions"]}

checkout = None if TEAM_API_URL else team_checkout()
db_path = ROOT / os.environ.get("MECHANIC_DB", "artifacts/mechanic-assistant.sqlite3")
store = None if TEAM_API_URL else Store(
    db_path,
    send_deadline=MOCK_TIMEOUT * 4 + 10,
)
retrieval = None if TEAM_API_URL else load_retrieval(checkout)
team = TeamWorkflow(checkout, CATALOG_PATH, ML_URL, TOIR_URL, MOCK_TIMEOUT) if checkout else None
case_agent = None
if os.environ.get("CASE_AGENT") == "1" and not (TEAM_API_URL or TEAM_AGENT_URL):
    if not CORPUS.is_file():
        raise ValueError("Сначала подготовьте индекс командой python -m mechanic.prepare.")
    case_agent = CaseAgent(CORPUS, db_path.parent / "agent-turns.jsonl",
                           mechanic=team.workflow.mechanic if team else None)

policy_mechanic = team.workflow.mechanic if team else (None if TEAM_API_URL else EvidenceMechanic(CORPUS, MATERIALS))

MISSING_DRAFT_EVIDENCE = (
    "Для автоматической подготовки заявки пока не найдена подтверждённая руководством проверка. "
    "Уточните узел и наблюдения в чате. Заявка ещё не создана."
)


@asynccontextmanager
async def lifespan(_application):
    try:
        yield
    finally:
        if team:
            team.close()


app = FastAPI(title="Цифровой механик", docs_url="/api/docs", openapi_url="/api/openapi.json",
              redoc_url=None, lifespan=lifespan)
app.include_router(sources.router)
app.mount("/assets", StaticFiles(directory=WEB), name="assets")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, state: dict | None = None, admission=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.state = state
        self.admission = admission


def error_response(error: ApiError):
    body = {"error": {"code": error.code, "message": error.message}}
    if error.admission is not None:
        body["admission"] = error.admission
    if error.state is not None:
        body["state"] = error.state
    return JSONResponse(body, status_code=error.status)


@app.exception_handler(ApiError)
async def handle_api_error(_request, error):
    return error_response(error)


@app.exception_handler(RequestValidationError)
async def handle_validation(_request, _error):
    return error_response(ApiError(422, "invalid_request", "Проверьте идентификаторы и обязательные поля."))


def team_error(error):
    codes = {
        "session_not_found": "invalid_pair", "ml_not_found": "invalid_pair",
        "vehicle_not_found": "unknown_vehicle", "ml_pair_mismatch": "ml_invalid_response",
    }
    return ApiError(error.status, codes.get(error.code, error.code), str(error), admission=getattr(error, "admission", None))


if team:
    @app.exception_handler(team.Problem)
    async def handle_team_error(_request, error):
        return error_response(team_error(error))


PROXY_ROUTES = {
    ("GET", "/api/catalog"), ("GET", "/api/state"), ("POST", "/api/diagnose"),
    ("POST", "/api/chat"), ("POST", "/api/draft"), ("PUT", "/api/draft"), ("POST", "/api/ticket"),
}


@app.middleware("http")
async def team_gateway(request: Request, call_next):
    path = request.url.path
    reconciliation = request.method == "POST" and re.fullmatch(r"/api/drafts/[^/]+/reconcile", path)
    if TEAM_API_URL and ((request.method, path) in PROXY_ROUTES or reconciliation):
        try:
            async with httpx.AsyncClient(timeout=TEAM_TIMEOUT, trust_env=False) as client:
                upstream = await client.request(
                    request.method, TEAM_API_URL + path, params=request.query_params,
                    content=await request.body(), headers={"Content-Type": "application/json"},
                )
            body = upstream.json()
            if not isinstance(body, dict):
                raise ValueError("Ожидался JSON-объект")
            if path == "/api/catalog" and upstream.is_success:
                body = {"manuals": sources.catalog(), "confidence_threshold": LOW_CONFIDENCE,
                        **body, "mode": "team"}
            response = JSONResponse(body, status_code=upstream.status_code)
        except httpx.TimeoutException:
            response = error_response(ApiError(504, "team_timeout", "Бэкенд команды не ответил вовремя."))
        except (httpx.RequestError, ValueError):
            response = error_response(ApiError(503, "team_unavailable", "Бэкенд команды недоступен."))
    else:
        response = await call_next(request)
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok", "service": "mechanic"}


@app.get("/api/catalog")
def catalog():
    return {
        "vehicles": team.catalog({v["vehicle_id"]: v for v in CATALOG}) if team else CATALOG,
        "manuals": sources.catalog(),
        "fault_types": FAULTS,
        "mode": "hybrid" if TEAM_AGENT_URL or case_agent else "demo",
        "pressure_comparison": case_agent is not None,
        "manual_search": "team" if retrieval else "fixture",
        "backend": "team" if team else "local",
        "confidence_threshold": LOW_CONFIDENCE,
    }


def vehicle_for(pair: Pair) -> dict:
    vehicle = VEHICLE_BY_ID.get(pair.vehicle_id)
    if vehicle is None:
        raise ApiError(404, "unknown_vehicle", "Транспортное средство не найдено. Выберите его из каталога.")
    if pair.key() not in SESSION_KEYS:
        raise ApiError(404, "invalid_pair", "Сессия не принадлежит выбранному ТС. Выберите его сессию.")
    return vehicle


FLEET_CATEGORIES = ("quarantine", "attention", "recheck", "ready")


def fleet_row(vehicle: dict) -> dict:
    """Сводка по машине: последняя проверка, история и состояние заявки.

    Порогов нет в ТЗ v6, поэтому категории — локальное правило стенда:
    высокая оценка аномалии, высокий риск или зарегистрированная заявка
    означают «карантин»; сбой проверки — «повторить запись».
    """
    sessions = sorted(vehicle["sessions"], key=lambda session: session["checked_at"])
    latest = sessions[-1]
    pair = Pair(vehicle_id=vehicle["vehicle_id"], session_id=latest["session_id"])
    state = read_state(pair) if store else (team.populate(empty_state(), pair) if team else empty_state())
    ticket = state["ticket"]
    draft = state["draft"]
    check = generator_check.session_check(vehicle, state)
    brief = generator_check.brief(check["value"]) if check else state["brief"]
    result = state["diagnosis"] or latest
    score = result.get("anomaly_score")

    if ticket["status"] in ("sending", "created", "unknown") or (brief and brief["risk"] == "high") \
            or (score is not None and score >= 0.8):
        category = "quarantine"
        reason = "Высокая оценка отклонения, высокий риск или заявка в работе"
    elif result["status"] == "check_failed":
        category = "recheck"
        reason = "Сбой проверки: нужна повторная запись"
    elif result["status"] == "anomaly":
        category = "attention"
        reason = "Отклонение требует контроля"
    else:
        category = "ready"
        reason = "Значимых отклонений не обнаружено"

    return {
        "vehicle_id": vehicle["vehicle_id"],
        "make": vehicle["make"],
        "model": vehicle["model"],
        "engine_model": vehicle["engine_model"],
        "category": category,
        "reason": reason,
        "urgency": draft["urgency"] if draft else None,
        "ticket": {key: ticket.get(key) for key in ("status", "ticket_id")},
        "latest": {
            "session_id": latest["session_id"],
            "checked_at": latest["checked_at"],
            "status": result["status"],
            "anomaly_type": result.get("anomaly_type"),
            "anomaly_title": FAULTS.get(result.get("anomaly_type")),
            "anomaly_score": score,
        },
        "history": [
            {
                "session_id": session["session_id"],
                "checked_at": session["checked_at"],
                "status": result["status"] if session == latest else session["status"],
                "anomaly_score": score if session == latest else session.get("anomaly_score"),
            }
            for session in sessions[-5:]
        ],
    }


@app.get("/api/fleet")
def fleet():
    rows = [fleet_row(vehicle) for vehicle in VEHICLES]
    return {
        "summary": {**{name: sum(row["category"] == name for row in rows) for name in FLEET_CATEGORIES},
                    "total": len(rows)},
        "vehicles": sorted(rows, key=lambda row: (FLEET_CATEGORIES.index(row["category"]), row["vehicle_id"])),
        "criteria": {
            "quarantine": "оценка аномалии >= 0,8, высокий риск или заявка в работе",
            "recheck": "последняя проверка закончилась сбоем записи",
            "attention": "последняя проверка показала отклонение",
            "ready": "последняя проверка — норма",
        },
    }


@app.get("/api/history")
def vehicle_history(vehicle_id: Annotated[str, Query()]):
    """Последние проверки машины и состояния её заявок — память по vehicle_id."""
    vehicle = VEHICLE_BY_ID.get(vehicle_id)
    if vehicle is None:
        raise ApiError(404, "unknown_vehicle", "Транспортное средство не найдено. Выберите его из каталога.")
    sessions = sorted(vehicle["sessions"], key=lambda session: session["checked_at"])[-5:]
    rows = []
    for session in sessions:
        pair = Pair(vehicle_id=vehicle_id, session_id=session["session_id"])
        state = read_state(pair) if store else (team.populate(empty_state(), pair) if team else empty_state())
        diagnosis = state["diagnosis"] or session
        ticket = state["ticket"]
        rows.append({
            "session_id": session["session_id"],
            "checked_at": session["checked_at"],
            "status": diagnosis["status"],
            "anomaly_type": diagnosis.get("anomaly_type"),
            "anomaly_title": FAULTS.get(diagnosis.get("anomaly_type")),
            "anomaly_score": diagnosis.get("anomaly_score"),
            "ticket": {key: ticket.get(key) for key in ("status", "ticket_id")},
        })
    return {"vehicle_id": vehicle_id, "sessions": rows}


@app.get("/api/state")
def session_state(pair: Annotated[Pair, Query()]):
    vehicle_for(pair)
    return session_response(pair, read_state(pair))


def read_state(pair: Pair):
    state = store.read(pair.key())
    if team:
        team.populate(state, pair, restore_diagnosis=True)
    return state


def present(pair: Pair, state: dict):
    return session_response(pair, team.populate(state, pair) if team else state)


def admission_for(pair: Pair, state: dict, *, fields=None, request=None):
    if team and request is None:
        draft = team.current_draft(pair)
        if draft is not None:
            return draft.admission
    return evaluate_admission(
        VEHICLE_BY_ID[pair.vehicle_id], state.get("diagnosis"),
        fields if fields is not None else state.get("draft"), mechanic=policy_mechanic,
        draft_id=state.get("draft_id"), draft_revision=state.get("draft_revision"),
        diagnosis_revision=(request.diagnosis_revision if request is not None
                            else state.get("draft_diagnosis_revision")),
        warning_acknowledged=bool(request and request.warning_acknowledged),
        applicability_confirmations=(request.applicability_confirmations if request is not None
                                     else state.get("applicability_confirmations", [])),
    )


def admission_error(decision, pair=None, state=None):
    reason = decision["block_reason"] or {"code": "draft_unavailable", "message": "Нет подтверждённой процедуры."}
    return ApiError(409 if reason["code"] == "diagnosis_changed" else 422, reason["code"], reason["message"],
                    session_response(pair, state) if pair is not None and state is not None else None,
                    admission=decision)


def session_response(pair: Pair, state: dict):
    vehicle = VEHICLE_BY_ID[pair.vehicle_id]
    check = generator_check.session_check(vehicle, state)
    if check:
        state = {**state, "brief": generator_check.brief(check["value"]), "brief_error": None}
    decision = admission_for(pair, state)
    available = decision["can_prepare"] and not state["draft"]
    reason = None
    if not state["draft"] and not available:
        block = decision["block_reason"]
        reason = (block["message"] + " " + block["next_action"] if block else state.get("draft_unavailable_reason"))
    return {
        **public_state(state),
        "generator_check": check,
        "messages": chat_history(state),
        "admission": decision,
        "draft_available": available,
        "draft_unavailable_reason": reason,
        "previous_drafts": state.get("previous_drafts", []),
        "reconcile_available": bool(team and team.can_reconcile and state["ticket"]["status"] == "unknown"),
    }


def chat_history(state: dict) -> list[dict]:
    conversation = [message for message in state["messages"] if message["role"] != "diagnosis"][-60:]
    diagnosis = state["diagnosis"]
    if diagnosis is None:
        return conversation

    status = diagnosis["status"]
    summary, next_step = {
        "normal": ("Значимых отклонений не обнаружено", "Продолжайте штатный контроль двигателя."),
        "anomaly": ("Обнаружено отклонение", "Уточните симптомы и фактические измерения для дефектовки."),
        "check_failed": ("Сбой проверки записи", "Повторите запись. По этой записи вывод о двигателе невозможен."),
    }[status]
    brief = state["brief"] or {}
    verdict = {
        "role": "diagnosis",
        "status": status,
        "summary": summary,
        "description": diagnosis["description"],
        "next_step": brief.get("first_check", next_step),
        "references": brief.get("references", []),
        "origin": brief.get("origin"),
    }
    return [verdict, *conversation]


def post(url: str, payload: dict, timeout: float) -> httpx.Response:
    with httpx.Client(timeout=timeout, trust_env=False) as client:
        return client.post(url, json=payload)


def audit(tool: str, started: float, status: str = "ok") -> dict:
    return ToolCall(tool=tool, status=status, duration_ms=round((perf_counter() - started) * 1000)).model_dump()


def add_call(pair: Pair, tool: str, started: float, status: str):
    store.update(pair.key(), lambda state: record_call(state, audit(tool, started, status)))


def status_for(error: ApiError) -> str:
    return {404: "rejected", 422: "rejected", 504: "timeout"}.get(error.status, "error")


def diagnose_with_ml(pair: Pair) -> Diagnosis:
    try:
        response = post(f"{ML_URL}/diagnose", pair.model_dump(), MOCK_TIMEOUT)
        if response.status_code == 404:
            raise ApiError(404, "invalid_pair", "Mock ML не нашёл такую пару ТС и сессии.")
        if response.status_code != 200:
            raise ApiError(503, "ml_unavailable", "Mock ML не вернул результат. Попробуйте ещё раз.")
        diagnosis = Diagnosis.model_validate(response.json())
        if diagnosis.key() != pair.key():
            raise ValueError("Пара ТС и сессии в ответе изменилась")
        if diagnosis.status == "anomaly" and diagnosis.anomaly_type is None:
            raise ValueError("Не указан тип отклонения")
        return diagnosis
    except httpx.TimeoutException as error:
        raise ApiError(504, "ml_timeout", "Mock ML не ответил вовремя. Запустите диагностику ещё раз.") from error
    except httpx.RequestError as error:
        raise ApiError(503, "ml_unavailable", "Mock ML недоступен. Проверьте подключение сервиса.") from error
    except ValueError as error:
        raise ApiError(502, "ml_invalid_response", "Mock ML вернул некорректный результат.") from error


def live_diagnosis(pair: Pair) -> Diagnosis:
    if not team:
        return diagnose_with_ml(pair)
    try:
        return Diagnosis.model_validate(team.diagnose(pair))
    except team.Problem as error:
        raise team_error(error) from error


def agent_call(action: str, vehicle: dict, diagnosis: Diagnosis, state: dict, message: str | None = None):
    if not TEAM_AGENT_URL:
        raise RuntimeError("Не задан TEAM_AGENT_URL")
    context = {key: vehicle[key] for key in ("vehicle_id", "engine_model", "fuel", "make", "model")}
    conversation = [item for item in state["messages"] if item["role"] in ("user", "assistant")]
    payload = {
        "action": action, "vehicle": context, "diagnosis": diagnosis.model_dump(), "message": message,
        "messages": conversation[-12:] if action == "chat" else [],
    }
    if retrieval:
        questions = [message] if message else [diagnosis.description]
        candidates = retrieval.candidates(questions, vehicle["engine_model"], diagnosis.anomaly_type)
        payload["source_candidates"] = candidates
        available = {hit["fragment_id"] for hit in candidates}
    try:
        response = post(f"{TEAM_AGENT_URL}/respond", payload, TEAM_TIMEOUT)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError("Ожидался объект")
        if retrieval:
            resolve_agent_sources(body, vehicle["engine_model"], available)
        calls = [ToolCall.model_validate(item).model_dump() for item in body.get("calls", [])]
        return body, calls
    except httpx.TimeoutException as error:
        raise ApiError(504, "agent_timeout", "Агент команды не ответил вовремя. Повторите запрос.") from error
    except httpx.HTTPError as error:
        raise ApiError(503, "agent_unavailable", "Агент команды недоступен.") from error
    except (ValueError, TypeError) as error:
        raise ApiError(502, "agent_invalid_response", "Агент вернул некорректный ответ.") from error


def resolve_agent_sources(body: dict, engine: str, available: set):
    def fill(item):
        ids = item.pop("source_ids", None)
        if ids is None:
            if item.get("references"):
                raise ValueError("Нужны ID найденных фрагментов")
            return
        if not isinstance(ids, list) or not all(isinstance(fragment, str) and fragment in available for fragment in ids):
            raise ValueError("Указан неизвестный фрагмент")
        item["references"] = [retrieval.resolve(fragment, engine).model_dump() for fragment in ids]

    for item in (body.get("brief"), body.get("draft")):
        if isinstance(item, dict):
            fill(item)
    if isinstance(body.get("reply"), dict):
        for section in body["reply"].get("sections", []):
            if isinstance(section, dict):
                fill(section)


def verify_references(references, vehicle: dict, required: bool = False):
    if (required and not references) or any(
        not sources.valid_reference(reference, vehicle["engine_model"]) for reference in references
    ):
        raise ApiError(502, "source_not_applicable",
                       "Указанная страница не подтверждена для этой модификации двигателя.")


def bind_fixture_sources(items, vehicle, diagnosis, question=None):
    if not retrieval:
        return []
    references = [ref for item in items for ref in item.references if key(ref) in SEARCH_QUESTIONS]
    if not references:
        return []
    started = perf_counter()
    try:
        confirmed = retrieval.validate_fixture(references, vehicle["engine_model"], diagnosis.anomaly_type, question)
    except ValueError as error:
        raise ApiError(502, "manual_search_no_match", str(error)) from error
    for item in items:
        item.references = [confirmed.get(key(ref), ref) for ref in item.references]
    return [audit("manuals.search", started)]


def local_agent_turn(vehicle, diagnosis, question, history):
    try:
        return case_agent.chat(vehicle, diagnosis, question, history)
    except TimeoutError as error:
        raise ApiError(504, "agent_timeout", "Агент не ответил вовремя. Повторите запрос.") from error
    except URLError as error:
        raise ApiError(503, "agent_unavailable", "Ollama или выбранная модель недоступны.") from error
    except (KeyError, TypeError, ValueError) as error:
        raise ApiError(502, "agent_invalid_response", "Агент вернул некорректный ответ.") from error


def overview(vehicle: dict, diagnosis: Diagnosis):
    if generator_check.applies(vehicle, diagnosis.model_dump()):
        return generator_check.brief(None), [], "agent.generator_check"
    if case_agent:
        reply, _, _, calls = local_agent_turn(vehicle, diagnosis, "Покажи диагностику", [])
        result = reply.analysis
        references = [Reference(manual=item["manual"], pdf_page=item["pdf_page"]) for item in result["references"]]
        verify_references(references, vehicle)
        first_item = next(iter(result["checks"]), None)
        first = first_item["text"] if first_item else None
        first_references = [
            Reference(manual=item["manual"], pdf_page=item["pdf_page"])
            for item in result["references"]
            if first_item and item.get("fragment_id") == first_item.get("fragment_id")
        ]
        brief = Brief(risk=result.get("risk_level") or "unassessed",
                      rationale=" ".join(item["text"] for item in result["risk"]) or "Уровень риска по имеющимся данным не установлен.",
                      first_check=first or result["clarifying_question"] or reply.summary,
                      references=first_references or references)
        return {"origin": "team", **brief.model_dump(), "analysis": result}, calls, "agent.overview"
    case = demo_agent.case_for(vehicle, diagnosis)
    if not TEAM_AGENT_URL or case == "mismatch":
        brief = demo_agent.brief(vehicle, diagnosis)
        decision = evaluate_admission(vehicle, diagnosis, mechanic=policy_mechanic)
        if decision["approved_operations"]:
            approved = decision["approved_operations"]
            refs = {(ref.manual, ref.pdf_page): ref for ref in brief.references}
            for operation in approved:
                for source in operation["references"]:
                    ref = Reference.model_validate(source)
                    refs[(ref.manual, ref.pdf_page)] = ref
            brief = Brief(risk="unassessed",
                rationale="Акустический признак — гипотеза. Риск оценивается по фактическим измерениям и очной проверке.",
                first_check=approved[0]["operation"] if decision["can_prepare"] else decision["block_reason"]["next_action"],
                references=list(refs.values()))
        calls = bind_fixture_sources([brief], vehicle, diagnosis)
        payload = {"origin": "fixture", **brief.model_dump()}
        if analysis := demo_agent.analysis(vehicle, diagnosis):
            payload["analysis"] = analysis
        return payload, calls, "agent.fixture"
    body, calls = agent_call("overview", vehicle, diagnosis, empty_state())
    brief = Brief.model_validate(body.get("brief"))
    verify_references(brief.references, vehicle, required=brief.risk != "unassessed")
    return {"origin": "team", **brief.model_dump()}, calls, "agent.overview"


@app.post("/api/diagnose")
def diagnose(pair: Pair):
    vehicle = vehicle_for(pair)
    requested = time()
    started = perf_counter()
    try:
        diagnosis = live_diagnosis(pair)
    except ApiError as error:
        add_call(pair, "mock_ml.diagnose", started, status_for(error))
        raise
    calls = [audit("mock_ml.diagnose", started)]
    brief, brief_error = None, None
    if diagnosis.status == "anomaly" or case_agent:
        started = perf_counter()
        tool = "agent.overview" if TEAM_AGENT_URL or case_agent else "agent.fixture"
        try:
            brief, related_calls, tool = overview(vehicle, diagnosis)
            calls.extend([audit(tool, started), *related_calls])
        except (ApiError, ValidationError) as error:
            brief_error = error.message if isinstance(error, ApiError) else "Ответ агента не по схеме."
            calls.append(audit(tool, started, status_for(error) if isinstance(error, ApiError) else "error"))

    def save(state):
        for call in calls:
            record_call(state, call)
        if requested < state.get("diagnosis_requested", 0):
            return
        saved_diagnosis = state["diagnosis"]
        diagnosis_data = diagnosis.model_dump()
        if saved_diagnosis != diagnosis_data:
            state["generator_reading"] = None
        if saved_diagnosis and (saved_diagnosis["status"], saved_diagnosis["anomaly_type"]) != (
            diagnosis.status, diagnosis.anomaly_type
        ):
            state["agent_history"] = []
            state["messages"] = []
            if team or state["ticket"]["status"] in ("idle", "draft", "error"):
                state.update(draft=None, draft_id=None, draft_revision=None, draft_origin=None,
                             ticket=empty_state()["ticket"], draft_diagnosis_revision=None,
                             applicability_confirmations=[], confirmed_urgency=None)
        if not team and saved_diagnosis and saved_diagnosis != diagnosis_data and state["draft"]:
            state.update(draft_revision=state["draft_revision"] + 1,
                         draft_diagnosis_revision=diagnosis_revision(diagnosis_data),
                         applicability_confirmations=[], confirmed_urgency=None)
        if brief and brief.get("analysis") and not state["agent_history"]:
            analysis = brief["analysis"]
            if {"verdict", "clarifying_question", "checks"} <= analysis.keys():
                if entry := history_entry("Покажи диагностику", analysis):
                    state["agent_history"] = [entry]
        state.update(diagnosis=diagnosis_data, brief=brief, brief_error=brief_error,
                     diagnosis_requested=requested, draft_unavailable_reason=None)
        state["messages"] = chat_history(state)

    return present(pair, store.update(pair.key(), save))


def response_to_chat(vehicle, diagnosis, state, question):
    draft_requested = wants_draft(question)
    decision = admission_for(Pair(vehicle_id=diagnosis.vehicle_id, session_id=diagnosis.session_id), state)
    if draft_requested and not decision["can_prepare"]:
        reason = decision["block_reason"]
        return Reply(summary=reason["message"], sections=[Section(kind="clarify", items=[reason["next_action"]])]), None, "fixture", [], None
    if draft_requested and diagnosis.status != "anomaly":
        return Reply(summary="Черновик дефектовки доступен для результата с отклонением."), None, "fixture", [], None
    if draft_requested and state["draft"]:
        return Reply(summary="Черновик уже открыт. Проверьте поля и подтвердите приоритет."), None, "fixture", [], None

    check = generator_check.session_check(vehicle, state)
    if check and not draft_requested and generator_check.TOPIC.search(question.casefold()):
        return generator_check.reply(check["value"]), None, "rule", [], None

    demo_case = demo_agent.case_for(vehicle, diagnosis)
    mismatch = demo_case == "mismatch"
    if case_agent:
        reply, proposal, entry, calls = local_agent_turn(vehicle, diagnosis, question, state["agent_history"])
        if proposal is not None and (not draft_requested or diagnosis.status != "anomaly" or mismatch):
            raise ApiError(502, "agent_invalid_draft", "Черновик возможен только для совместимого отклонения.")
        return reply, proposal, "team", calls, entry

    if TEAM_AGENT_URL and diagnosis.status != "check_failed" and not mismatch:
        body, calls = agent_call("chat", vehicle, diagnosis, state, question)
        reply = Reply.model_validate(body.get("reply"))
        proposal = DraftFields.model_validate(body["draft"]) if body.get("draft") is not None else None
        if proposal is not None and (not draft_requested or diagnosis.status != "anomaly"):
            raise ApiError(502, "agent_invalid_draft", "Черновик возможен по явному запросу для результата с отклонением.")
        return reply, proposal, "team", calls, None

    if draft_requested:
        proposal = demo_agent.draft(vehicle, diagnosis)
        if proposal is None and decision["can_prepare"]:
            from backend.models import Diagnosis as BackendDiagnosis
            checked = policy_mechanic.suggest_draft(BackendDiagnosis.model_validate(diagnosis.model_dump()), vehicle["engine_model"])
            proposal = DraftFields.model_validate({**checked.model_dump(), "urgency": decision["suggested_urgency"]})
        if proposal:
            return Reply(summary="Черновик подготовлен. Проверьте поля и подтвердите приоритет."), proposal, "fixture", [], None
        message = demo_agent.mismatch(vehicle, diagnosis) if mismatch else (
            "Для черновика нужна подтверждённая страница по этому двигателю. "
            "Уточните узел и результаты проверки перед дефектовкой."
        )
        return Reply(summary=message), None, "fixture", [], None
    data = dict(diagnosis.model_dump(), fuel=vehicle["fuel"], engine_model=vehicle["engine_model"])
    if incompatible_fault(data) or data["signal_quality"] == "bad" or diagnosis.status == "check_failed":
        reason = (decision.get("block_reason") or {}).get("message", "")
        message = case_context.blocked_message(reason, data, vehicle["engine_model"])
        return Reply(summary=message), None, "fixture", [], None
    if demo_case == "oil" and (reply := demo_agent.oil_followup_reply(question, state["messages"])):
        return reply, None, "fixture", [], None
    if retrieval:
        topic = case_context.subject(question, [m["text"] for m in state["messages"] if m["role"] == "user"])
        index = team.workflow.mechanic.search if team else retrieval.index
        matched = case_context.reviewed_oil_pressure(index, vehicle["engine_model"], topic)
        if matched is not None:
            if not matched:
                return Reply(summary="В индексе отсутствует страница для сверки нормы. Уточните условия замера."), None, "team", [], None
            sections = [Section(kind="checks", items=[excerpt], references=[Reference.model_validate(index.resolve_source(fid, vehicle["engine_model"]))])
                        for fid, excerpt in matched]
            return Reply(summary=diagnosis.description, sections=sections), None, "team", [], None
        if topic != question and conflict_case(topic, vehicle["engine_model"]):
            return demo_agent.answer(vehicle, diagnosis, topic), None, "fixture", [], None
    if decision["can_prepare"] and demo_agent.FIRST.search(question.casefold()):
        operations = decision["approved_operations"]
        refs = [Reference(**ref) for operation in operations for ref in operation["references"]]
        checks = [operation["operation"] for operation in operations]
        conditions = [condition["text"] for operation in operations for condition in operation["conditions"]]
        reply = Reply(summary="Проверка по руководству выбранного двигателя.", sections=[
            Section(kind="signs", items=[diagnosis.description]),
            Section(kind="checks", items=checks, references=refs),
            Section(kind="clarify", items=["Ограничения и условия: " + " ".join(conditions)]),
        ], analysis={"fluid_check": {}} if demo_case == "oil" else None)
        return reply, None, "fixture", [], None
    return demo_agent.answer(vehicle, diagnosis, question), None, "fixture", [], None


def draft_available(vehicle: dict, state: dict) -> bool:
    if not state["diagnosis"] or state["draft"]:
        return False
    pair = Pair(vehicle_id=state["diagnosis"]["vehicle_id"], session_id=state["diagnosis"]["session_id"])
    return admission_for(pair, state)["can_prepare"]


def save_proposal(state: dict, pair: Pair, diagnosis: Diagnosis, draft, origin: str):
    if not team and state["draft"] is not None:
        return
    decision = admission_for(pair, state)
    if not decision["can_prepare"]:
        raise admission_error(decision, pair, state)
    approved = decision["approved_operations"]
    draft = DraftFields.model_validate({**draft.model_dump(),
        "urgency": decision["suggested_urgency"],
        "operations": [item["operation"] for item in approved],
        "references": list({(reference["manual"], reference["pdf_page"]): reference
                            for item in approved for reference in item.get("references", [{"manual": item["manual"], "pdf_page": item["pdf_page"]}])}.values()),
    })
    if team:
        draft_started = perf_counter()
        current = team.prepare(pair, diagnosis, draft)
        state["draft_id"] = current.draft_id
        record_call(state, audit("backend.drafts.create", draft_started))
    else:
        state["draft"] = TicketRequest(
            **diagnosis.model_dump(include={"vehicle_id", "session_id", "anomaly_type"}),
            **draft.model_dump(),
        ).model_dump()
        state.update(draft_id=uuid4().hex, draft_revision=1,
                     draft_diagnosis_revision=decision["diagnosis_revision"], applicability_confirmations=[])
        state["ticket"]["status"] = "draft"
    state["draft_origin"] = origin
    state["draft_unavailable_reason"] = None


@app.post("/api/draft")
def prepare_draft(pair: Pair):
    vehicle = vehicle_for(pair)
    snapshot = read_state(pair)
    if snapshot["draft"]:
        return session_response(pair, snapshot)
    decision = admission_for(pair, snapshot)
    if not decision["can_prepare"]:
        raise admission_error(decision, pair, snapshot)
    available = draft_available(vehicle, snapshot)
    if not available and (not case_agent or not snapshot["diagnosis"] or snapshot["brief_error"]):
        raise ApiError(409, "draft_unavailable", "Для черновика пока недостаточно подтверждённых данных.")

    diagnosis = Diagnosis.model_validate(snapshot["diagnosis"])
    if diagnosis.status != "anomaly" or demo_agent.case_for(vehicle, diagnosis) == "mismatch":
        raise ApiError(409, "draft_unavailable", "Для черновика пока недостаточно подтверждённых данных.")
    started = perf_counter()
    tool = "agent.chat" if TEAM_AGENT_URL or case_agent else "agent.fixture"
    try:
        if available:
            reply, draft, origin, calls, entry = response_to_chat(vehicle, diagnosis, snapshot, "Подготовь заявку")
        else:
            reason = case_context.blocked_message(snapshot.get("draft_unavailable_reason") or MISSING_DRAFT_EVIDENCE,
                                                  diagnosis.model_dump(), vehicle["engine_model"])
            reply = Reply(summary="Черновик пока не подготовлен.",
                          sections=[Section(kind="clarify", items=[reason])])
            draft, origin, calls, entry = None, "team", [], None
        for section in reply.sections:
            verify_references(section.references, vehicle, section.kind in ("causes", "checks", "work", "risk"))
        if draft is not None:
            verify_references(draft.references, vehicle, required=True)
            if origin == "fixture":
                calls.extend(bind_fixture_sources([draft], vehicle, diagnosis))
        else:
            reason = " ".join(item for section in reply.sections if section.kind == "clarify" for item in section.items)
            reason = reason or MISSING_DRAFT_EVIDENCE
    except ApiError as error:
        add_call(pair, tool, started, status_for(error))
        raise
    except ValidationError as error:
        add_call(pair, tool, started, "error")
        raise ApiError(502, "agent_invalid_response", "Ответ агента не соответствует схеме.") from error

    def save(state):
        current = team.stored_diagnosis(pair) if team else state["diagnosis"]
        if current != snapshot["diagnosis"]:
            raise ApiError(409, "diagnosis_changed", "Результат проверки изменился. Обновите сессию.")
        if team:
            team.restore_diagnosis(state, current)
        if (state["draft"] is not None or state["messages"] != snapshot["messages"]
                or state["agent_history"] != snapshot["agent_history"]
                or state["draft_origin"] != snapshot["draft_origin"]):
            record_call(state, audit(tool, started, "rejected"))
            for call in calls:
                record_call(state, call)
            return
        record_call(state, audit(tool, started, "ok" if draft is not None else "rejected"))
        for call in calls:
            record_call(state, call)
        if draft is not None:
            save_proposal(state, pair, diagnosis, draft, origin)
            if entry is not None:
                state["agent_history"] = [*state["agent_history"][-5:], entry]
        else:
            state["draft_unavailable_reason"] = reason
            state["messages"].append({"role": "assistant", "origin": origin, **reply.model_dump()})
            state["messages"] = chat_history(state)
            if entry is not None:
                state["agent_history"] = [*state["agent_history"][-5:], entry]

    return present(pair, store.update(pair.key(), save))


@app.post("/api/chat")
def chat(request: ChatRequest):
    vehicle = vehicle_for(request)
    snapshot = read_state(request)
    if not snapshot["diagnosis"]:
        raise ApiError(409, "diagnosis_needed", "Сначала получите результат диагностики этой сессии.")
    diagnosis = Diagnosis.model_validate(snapshot["diagnosis"])
    reading = request.generator_reading
    message = request.message
    started = perf_counter()
    tool = "agent.chat" if TEAM_AGENT_URL or case_agent else "agent.fixture"
    try:
        if reading:
            tool = "agent.generator_check"
            check = generator_check.session_check(vehicle, snapshot)
            if not check or check["diagnosis_revision"] != reading.diagnosis_revision:
                raise ApiError(409, "diagnosis_changed", "Проверка изменилась. Сверьте актуальный результат.",
                               session_response(request, snapshot))
            message = generator_check.REPORTS[reading.value]
            reply, draft, origin, calls, agent_entry = generator_check.reply(reading.value), None, "rule", [], None
        else:
            reply, draft, origin, calls, agent_entry = response_to_chat(vehicle, diagnosis, snapshot, message)
            tool = {"team": "agent.chat", "fixture": "agent.fixture", "rule": "agent.generator_check"}[origin]
        for section in reply.sections:
            verify_references(section.references, vehicle, section.kind in ("causes", "checks", "work", "risk"))
        if draft is not None:
            verify_references(draft.references, vehicle, required=True)
        if origin == "fixture":
            items = [*reply.sections, draft] if draft else reply.sections
            calls.extend(bind_fixture_sources(items, vehicle, diagnosis, message))
    except ApiError as error:
        add_call(request, tool, started, status_for(error))
        raise
    except ValidationError as error:
        add_call(request, tool, started, "error")
        raise ApiError(502, "agent_invalid_response", "Ответ агента не соответствует схеме.") from error

    def save(state):
        current = team.stored_diagnosis(request) if team else state["diagnosis"]
        if current != snapshot["diagnosis"]:
            raise ApiError(409, "diagnosis_changed", "Результат изменился. Задайте вопрос по обновлённой проверке.")
        if team:
            team.restore_diagnosis(state, current)
        if reading:
            report = reading.model_dump()
            if state["generator_reading"] == report:
                return
            state["generator_reading"] = report
        elif any(state[key] != snapshot[key] for key in ("messages", "agent_history", "draft_origin")):
            record_call(state, audit(tool, started, "rejected"))
            for call in calls:
                record_call(state, call)
            return
        state["messages"].extend([
            {"role": "user", "text": message},
            {"role": "assistant", "origin": origin, **reply.model_dump()},
        ])
        if agent_entry is not None:
            state["agent_history"] = [*state["agent_history"][-5:], agent_entry]
        state["messages"] = chat_history(state)
        record_call(state, audit(tool, started))
        for call in calls:
            record_call(state, call)
        if draft is not None:
            save_proposal(state, request, diagnosis, draft, origin)

    return present(request, store.update(request.key(), save))


def stale_draft(pair: Pair, state: dict | None = None) -> ApiError:
    latest = read_state(pair) if state is None else state
    return ApiError(409, "stale_draft", "Черновик изменился. Проверьте сохранённую версию.",
                    session_response(pair, latest))


def require_draft_version(state: dict, request: DraftUpdate):
    if state["draft_id"] != request.draft_id or state["draft_revision"] != request.revision:
        raise stale_draft(request, state)


def same_submission(request: TicketSubmit, draft_id: str, revision: int, fields: dict) -> bool:
    edits = request.edits.model_dump()
    return (request.draft_id == draft_id and request.revision <= revision and
            all(fields[key] == value for key, value in edits.items()))


@contextmanager
def selected_team_draft(pair: Pair, draft_id: str, revision: int, *, submission: TicketSubmit | None = None,
                        require_current: bool = False):
    try:
        draft = team.draft_for_pair(pair, draft_id)
    except team.Problem as error:
        if error.code == "draft_not_found":
            raise stale_draft(pair) from error
        raise
    if require_current or (submission is not None and draft.state in ("draft", "delivery_failed")):
        diagnosis = team.stored_diagnosis(pair)
        if not diagnosis or diagnosis["status"] != "anomaly" or diagnosis["anomaly_type"] != draft.anomaly_type:
            raise stale_draft(pair)
    if submission is not None and draft.state in ("sending", "sent", "delivery_unknown"):
        matches = same_submission(submission, draft.draft_id, draft.revision, draft.model_dump())
    else:
        matches = (draft.draft_id, draft.revision) == (draft_id, revision)
    if not matches:
        raise stale_draft(pair)
    try:
        yield draft
    except team.Problem as error:
        if error.code in ("stale_draft", "draft_locked", "draft_not_found"):
            raise stale_draft(pair) from error
        raise


def edit_draft(state: dict, request: DraftUpdate, vehicle: dict):
    if not state["draft"] or state["ticket"]["status"] not in ("draft", "error"):
        raise ApiError(409, "draft_locked", "Черновик отсутствует или его статус изменился.")
    draft = TicketRequest.model_validate({**state["draft"], **request.edits.model_dump()})
    diagnosis = Diagnosis.model_validate(state["diagnosis"])
    if diagnosis.status != "anomaly" or diagnosis.anomaly_type != draft.anomaly_type:
        raise ApiError(409, "diagnosis_changed", "Код или статус диагностики изменился. Обновите черновик.")
    if demo_agent.case_for(vehicle, diagnosis) == "mismatch":
        raise ApiError(409, "engine_mismatch", demo_agent.mismatch(vehicle, diagnosis))
    verify_references(draft.references, vehicle, required=True)
    decision = evaluate_admission(vehicle, state["diagnosis"], draft.model_dump(), mechanic=policy_mechanic,
                                  diagnosis_revision=state.get("draft_diagnosis_revision"),
                                  applicability_confirmations=request.applicability_confirmations)
    if decision["block_reason"] and decision["block_reason"]["code"] not in ("warning_ack_required", "applicability_required"):
        raise admission_error(decision, request, state)
    condition_changed = state.get("applicability_confirmations", []) != request.applicability_confirmations
    state["applicability_confirmations"] = request.applicability_confirmations
    updated = draft.model_dump()
    if updated != state["draft"] or condition_changed:
        state.update(draft=updated, draft_revision=state["draft_revision"] + 1, confirmed_urgency=None)


@app.put("/api/draft")
def save_draft(request: DraftUpdate):
    vehicle = vehicle_for(request)
    if team:
        with selected_team_draft(request, request.draft_id, request.revision, require_current=True) as draft:
            team.patch(draft, request)
        return session_response(request, read_state(request))

    def save(state):
        require_draft_version(state, request)
        edit_draft(state, request, vehicle)

    return session_response(request, store.update(request.key(), save))


@app.post("/api/drafts/{draft_id}/reconcile")
def reconcile_draft(draft_id: Identifier, request: TicketReconciliation):
    vehicle_for(request)
    if not team or not team.can_reconcile:
        raise ApiError(409, "reconcile_unavailable", "На этом бэкенде сверка номера ещё недоступна.")
    started = perf_counter()
    with selected_team_draft(request, draft_id, request.revision) as draft:
        team.reconcile(draft, request)
    add_call(request, "backend.drafts.reconcile", started, "ok")
    return session_response(request, read_state(request))


def send_toir(draft: dict) -> dict:
    failed = {"status": "error", "ticket_id": None, "message": None}
    unknown = {"status": "unknown", "ticket_id": None, "message": UNKNOWN}
    try:
        response = post(f"{TOIR_URL}/tickets", draft, MOCK_TIMEOUT)
        if response.status_code == 201:
            receipt = TicketReceipt.model_validate(response.json())
            return {**receipt.model_dump(), "message": None}
        if response.status_code in (400, 404, 422):
            return {**failed, "message": f"ТОиР отклонил заявку (HTTP {response.status_code}). "
                    "Проверьте данные и повторите отправку вручную."}
        return unknown
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return {**failed, "message": "Связь с ТОиР не установлена. Черновик сохранён. "
                "После восстановления связи можно отправить его вручную."}
    except (httpx.RequestError, ValueError):
        return unknown


@app.post("/api/ticket")
def submit(request: TicketSubmit):
    vehicle = vehicle_for(request)
    if not request.priority_confirmed:
        raise ApiError(422, "priority_unconfirmed", "Подтвердите выбранный приоритет.")
    if team:
        with selected_team_draft(request, request.draft_id, request.revision, submission=request) as draft:
            if draft.state in ("draft", "delivery_failed"):
                stored = team.stored_diagnosis(request)
                if not stored:
                    raise ApiError(409, "diagnosis_changed", "Запустите проверку этой сессии.")
                diagnosis = Diagnosis.model_validate(stored)
                if demo_agent.case_for(vehicle, diagnosis) == "mismatch":
                    raise ApiError(409, "engine_mismatch", demo_agent.mismatch(vehicle, diagnosis))
            started = perf_counter()
            result = team.submit(draft, request)
        if draft.state in ("draft", "delivery_failed"):
            status = {"sent": "ok", "delivery_unknown": "unknown"}.get(result.state, "error")
            add_call(request, "mock_toir.tickets", started, status)
        return session_response(request, read_state(request))
    claimed = False

    def claim(state):
        nonlocal claimed
        if state["ticket"]["status"] in ("sending", "created", "unknown"):
            if not same_submission(request, state["draft_id"], state["draft_revision"], state["draft"]):
                raise stale_draft(request, state)
            return
        require_draft_version(state, request)
        edit_draft(state, request, vehicle)
        decision = admission_for(request, state, request=request)
        if not decision["can_submit"]:
            raise admission_error(decision, request, state)
        state["confirmed_urgency"] = request.edits.urgency
        state["ticket"].update(status="sending", ticket_id=None, message=None, started_at=time())
        claimed = True

    state = store.update(request.key(), claim)
    if not claimed:
        return session_response(request, state)

    started = perf_counter()
    result = send_toir(state["draft"])

    def finish(latest):
        latest["ticket"] = result
        status = {"created": "ok", "error": "error", "unknown": "unknown"}[result["status"]]
        record_call(latest, audit("mock_toir.tickets", started, status))

    return session_response(request, store.update(request.key(), finish))
