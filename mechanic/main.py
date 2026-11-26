import json
import os
from contextlib import asynccontextmanager
from time import perf_counter, time
from typing import Annotated

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import demo_agent, sources
from .config import DEFAULT_ML, DEFAULT_TOIR, MOCKS, ROOT, WEB, endpoint
from .contracts import (
    Brief, ChatRequest, Diagnosis, DraftFields, DraftUpdate, Pair, Reply, TicketReceipt,
    TicketRequest, TicketSubmit, ToolCall,
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
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def error_response(error: ApiError):
    return JSONResponse({"error": {"code": error.code, "message": error.message}}, status_code=error.status)


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
    return ApiError(error.status, codes.get(error.code, error.code), str(error))


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
    if TEAM_API_URL and (request.method, path) in PROXY_ROUTES:
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
        "mode": "hybrid" if TEAM_AGENT_URL else "demo",
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


def session_response(pair: Pair, state: dict):
    return {**public_state(state), "draft_available": draft_available(VEHICLE_BY_ID[pair.vehicle_id], state)}


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
    payload = {
        "action": action, "vehicle": context, "diagnosis": diagnosis.model_dump(), "message": message,
        "messages": state["messages"][-12:] if action == "chat" else [],
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


def overview(vehicle: dict, diagnosis: Diagnosis):
    if not TEAM_AGENT_URL or demo_agent.case_for(vehicle, diagnosis) == "mismatch":
        brief = demo_agent.brief(vehicle, diagnosis)
        calls = bind_fixture_sources([brief], vehicle, diagnosis)
        return {"origin": "fixture", **brief.model_dump()}, calls, "agent.fixture"
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
    if diagnosis.status == "anomaly":
        started = perf_counter()
        tool = "agent.overview" if TEAM_AGENT_URL else "agent.fixture"
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
        if (saved_diagnosis and state["ticket"]["status"] in ("idle", "draft", "error") and
            (saved_diagnosis["status"], saved_diagnosis["anomaly_type"]) !=
            (diagnosis.status, diagnosis.anomaly_type)):
            state.update(draft=None, draft_origin=None, messages=[], ticket=empty_state()["ticket"])
        state.update(diagnosis=diagnosis.model_dump(), brief=brief, brief_error=brief_error,
                     diagnosis_requested=requested)

    return present(pair, store.update(pair.key(), save))


def response_to_chat(vehicle, diagnosis, state, question):
    draft_requested = demo_agent.wants_draft(question)
    if draft_requested and diagnosis.status != "anomaly":
        return Reply(summary="Черновик дефектовки доступен для результата с отклонением."), None, "fixture", []
    if draft_requested and state["draft"]:
        return Reply(summary="Черновик уже открыт. Проверьте поля и подтвердите приоритет."), None, "fixture", []

    mismatch = demo_agent.case_for(vehicle, diagnosis) == "mismatch"
    if TEAM_AGENT_URL and diagnosis.status != "check_failed" and not mismatch:
        body, calls = agent_call("chat", vehicle, diagnosis, state, question)
        reply = Reply.model_validate(body.get("reply"))
        proposal = DraftFields.model_validate(body["draft"]) if body.get("draft") is not None else None
        if proposal is not None and diagnosis.status != "anomaly":
            raise ApiError(502, "agent_invalid_draft", "Черновик возможен только для результата с отклонением.")
        return reply, proposal, "team", calls

    if draft_requested:
        proposal = demo_agent.draft(vehicle, diagnosis)
        if proposal:
            return Reply(summary="Черновик подготовлен. Проверьте поля и подтвердите приоритет."), proposal, "fixture", []
        message = demo_agent.mismatch(vehicle) if mismatch else (
            "Для черновика нужна подтверждённая страница по этому двигателю. "
            "Уточните узел и результаты проверки перед дефектовкой."
        )
        return Reply(summary=message), None, "fixture", []
    return demo_agent.answer(vehicle, diagnosis, question), None, "fixture", []


def draft_available(vehicle: dict, state: dict) -> bool:
    if not state["diagnosis"] or state["draft"] or state["brief_error"]:
        return False
    diagnosis = Diagnosis.model_validate(state["diagnosis"])
    if diagnosis.status != "anomaly" or demo_agent.case_for(vehicle, diagnosis) == "mismatch":
        return False
    if TEAM_AGENT_URL:
        return bool(state["brief"] and state["brief"].get("references"))
    proposal = demo_agent.draft(vehicle, diagnosis)
    return bool(proposal and all(
        sources.valid_reference(reference, vehicle["engine_model"]) for reference in proposal.references
    ))


def save_proposal(state: dict, pair: Pair, diagnosis: Diagnosis, draft: DraftFields, origin: str):
    if state["draft"] is not None:
        return
    if team:
        draft_started = perf_counter()
        team.prepare(pair, draft)
        record_call(state, audit("backend.drafts.create", draft_started))
    else:
        state["draft"] = TicketRequest(
            **diagnosis.model_dump(include={"vehicle_id", "session_id", "anomaly_type"}),
            **draft.model_dump(),
        ).model_dump()
        state["ticket"]["status"] = "draft"
    state["draft_origin"] = origin


@app.post("/api/draft")
def prepare_draft(pair: Pair):
    vehicle = vehicle_for(pair)
    snapshot = read_state(pair)
    if snapshot["draft"]:
        return session_response(pair, snapshot)
    if not draft_available(vehicle, snapshot):
        raise ApiError(409, "draft_unavailable", "Для черновика пока недостаточно подтверждённых данных.")

    diagnosis = Diagnosis.model_validate(snapshot["diagnosis"])
    started = perf_counter()
    tool = "agent.chat" if TEAM_AGENT_URL else "agent.fixture"
    try:
        reply, draft, origin, calls = response_to_chat(vehicle, diagnosis, snapshot, "Подготовь заявку")
        if draft is None:
            raise ApiError(422, "draft_unavailable", reply.summary)
        verify_references(draft.references, vehicle, required=True)
        if origin == "fixture":
            calls.extend(bind_fixture_sources([draft], vehicle, diagnosis))
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
        record_call(state, audit(tool, started))
        for call in calls:
            record_call(state, call)
        save_proposal(state, pair, diagnosis, draft, origin)

    return present(pair, store.update(pair.key(), save))


@app.post("/api/chat")
def chat(request: ChatRequest):
    vehicle = vehicle_for(request)
    snapshot = read_state(request)
    if not snapshot["diagnosis"]:
        raise ApiError(409, "diagnosis_needed", "Сначала получите результат диагностики этой сессии.")
    diagnosis = Diagnosis.model_validate(snapshot["diagnosis"])
    started = perf_counter()
    tool = "agent.chat" if TEAM_AGENT_URL else "agent.fixture"
    try:
        reply, draft, origin, calls = response_to_chat(vehicle, diagnosis, snapshot, request.message)
        tool = "agent.chat" if origin == "team" else "agent.fixture"
        for section in reply.sections:
            verify_references(section.references, vehicle, section.kind in ("causes", "checks", "work"))
        if draft is not None:
            verify_references(draft.references, vehicle, required=True)
        if origin == "fixture":
            items = [*reply.sections, draft] if draft else reply.sections
            calls.extend(bind_fixture_sources(items, vehicle, diagnosis, request.message))
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
        state["messages"].extend([
            {"role": "user", "text": request.message},
            {"role": "assistant", "origin": origin, **reply.model_dump()},
        ])
        state["messages"] = state["messages"][-60:]
        record_call(state, audit(tool, started))
        for call in calls:
            record_call(state, call)
        if draft is not None:
            save_proposal(state, request, diagnosis, draft, origin)

    return present(request, store.update(request.key(), save))


def edit_draft(state: dict, request: DraftUpdate, vehicle: dict):
    if not state["draft"] or state["ticket"]["status"] not in ("draft", "error"):
        raise ApiError(409, "draft_locked", "Черновик отсутствует или его статус изменился.")
    draft = TicketRequest.model_validate({**state["draft"], **request.edits.model_dump()})
    diagnosis = Diagnosis.model_validate(state["diagnosis"])
    if diagnosis.status != "anomaly" or diagnosis.anomaly_type != draft.anomaly_type:
        raise ApiError(409, "diagnosis_changed", "Код или статус диагностики изменился. Обновите черновик.")
    if demo_agent.case_for(vehicle, diagnosis) == "mismatch":
        raise ApiError(409, "engine_mismatch", demo_agent.mismatch(vehicle))
    verify_references(draft.references, vehicle, required=True)
    state["draft"] = draft.model_dump()


@app.put("/api/draft")
def save_draft(request: DraftUpdate):
    vehicle = vehicle_for(request)
    if team:
        draft = team.current_draft(request)
        if not draft:
            raise ApiError(409, "draft_locked", "Черновик отсутствует или его статус изменился.")
        team.patch(draft, request)
        return session_response(request, read_state(request))
    return session_response(request, store.update(request.key(), lambda state: edit_draft(state, request, vehicle)))


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
        draft = team.current_draft(request)
        if not draft:
            raise ApiError(409, "draft_locked", "Черновик отсутствует или его статус изменился.")
        state = read_state(request)
        if draft.state in ("draft", "delivery_failed"):
            diagnosis = Diagnosis.model_validate(state["diagnosis"])
            if demo_agent.case_for(vehicle, diagnosis) == "mismatch":
                raise ApiError(409, "engine_mismatch", demo_agent.mismatch(vehicle))
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
            return
        edit_draft(state, request, vehicle)
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
