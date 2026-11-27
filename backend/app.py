"""HTTP entry point for the Digital Mechanic backend."""

import math
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .agent import Agent
from .mechanic import Mechanic
from .models import (
    ChatReply, ChatRequest, Confirm, DiagnosticResult, DraftPatch, DraftView, Identifier, Pair,
    Reconcile, Session, Vehicle,
)
from .store import Store
from .upstream import Problem
from .workflow import Catalog, Workflow


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    vehicles: Path = ROOT / "data/vehicles.json"
    corpus: Path = ROOT / "artifacts/manual-corpus.json"
    manuals: Path = ROOT / "artifacts/manuals"
    database: Path = ROOT / "artifacts/mechanic.sqlite3"
    ml_url: str = "http://127.0.0.1:8001"
    toir_url: str = "http://127.0.0.1:8002"
    timeout: float = 5
    frontend_origin: str = ""
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    ollama_timeout: float = 30
    ollama_generation_model: str = ""

    @classmethod
    def from_environment(cls):
        return cls(
            vehicles=ROOT / os.getenv("VEHICLES_PATH", str(cls.vehicles)),
            corpus=ROOT / os.getenv("MANUAL_CORPUS_PATH", str(cls.corpus)),
            manuals=ROOT / os.getenv("MANUALS_DIR", str(cls.manuals)),
            database=ROOT / os.getenv("MECHANIC_DB_PATH", str(cls.database)),
            ml_url=os.getenv("MOCK_ML_URL", cls.ml_url),
            toir_url=os.getenv("MOCK_TOIR_URL", cls.toir_url),
            timeout=float(os.getenv("UPSTREAM_TIMEOUT_SECONDS", str(cls.timeout))),
            frontend_origin=os.getenv("FRONTEND_ORIGIN", ""),
            ollama_url=os.getenv("OLLAMA_URL", cls.ollama_url),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            ollama_timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", str(cls.ollama_timeout))),
            ollama_generation_model=os.getenv("OLLAMA_GENERATION_MODEL", cls.ollama_generation_model).strip(),
        )


def get_workflow(request: Request) -> Workflow:
    return request.app.state.workflow


Context = Annotated[Workflow, Depends(get_workflow)]


def create_app(settings: Settings | None = None, *, ml_transport=None, toir_transport=None,
               ollama_transport=None) -> FastAPI:
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if not math.isfinite(settings.timeout) or settings.timeout <= 0:
            raise ValueError("UPSTREAM_TIMEOUT_SECONDS must be positive")
        if not math.isfinite(settings.ollama_timeout) or settings.ollama_timeout <= 0:
            raise ValueError("OLLAMA_TIMEOUT_SECONDS must be positive")
        catalog = Catalog(settings.vehicles)
        mechanic = Mechanic(settings.corpus, settings.manuals)
        store = Store(settings.database)
        with (
            httpx.Client(base_url=settings.ml_url, timeout=settings.timeout, transport=ml_transport, trust_env=False) as ml,
            httpx.Client(base_url=settings.toir_url, timeout=settings.timeout, transport=toir_transport, trust_env=False) as toir,
            httpx.Client(base_url=settings.ollama_url, timeout=settings.ollama_timeout,
                         transport=ollama_transport, trust_env=False) as ollama,
        ):
            application.state.workflow = Workflow(catalog, store, mechanic, ml, toir,
                                                  sending_deadline=max(60, 4 * settings.timeout))
            application.state.agent = Agent(application.state.workflow, ollama, settings.ollama_model,
                                            generation_model=settings.ollama_generation_model)
            yield

    application = FastAPI(title="Цифровой механик", version="0.1.0", lifespan=lifespan)
    if settings.frontend_origin:
        application.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin],
                                   allow_methods=["GET", "POST", "PATCH"], allow_headers=["Content-Type"])

    @application.exception_handler(Problem)
    async def problem_response(_request: Request, error: Problem):
        body = {"error": {"code": error.code, "message": str(error)}}
        if error.admission is not None:
            body["admission"] = error.admission
        if error.draft is not None:
            body["draft"] = error.draft.model_dump(mode="json")
        return JSONResponse(status_code=error.status, content=body)

    @application.exception_handler(RequestValidationError)
    async def validation_response(_request: Request, error: RequestValidationError):
        return JSONResponse(status_code=422, content={
            "error": {"code": "invalid_request", "message": "Проверьте поля запроса."},
            "issues": [{"path": list(item["loc"]), "message": item["msg"]} for item in error.errors()],
        })

    @application.get("/health")
    def health(workflow: Context):
        return {"status": "ok", "service": "digital-mechanic",
                "vehicles": len(workflow.catalog.vehicles), "manuals": len(workflow.mechanic.urls)}

    @application.get("/api/vehicles", response_model=list[Vehicle])
    def vehicles(workflow: Context):
        return list(workflow.catalog.vehicles.values())

    @application.get("/api/vehicles/{vehicle_id}/sessions", response_model=list[Session])
    def sessions(vehicle_id: str, workflow: Context):
        workflow.catalog.vehicle(vehicle_id)
        return list(workflow.catalog.sessions[vehicle_id].values())

    @application.post("/api/diagnose", response_model=DiagnosticResult)
    def diagnose(pair: Pair, workflow: Context):
        return workflow.diagnose(pair)

    @application.get("/api/diagnosis", response_model=DiagnosticResult)
    def stored_diagnosis(vehicle_id: Identifier, session_id: Identifier, workflow: Context):
        pair = Pair(vehicle_id=vehicle_id, session_id=session_id)
        vehicle = workflow.catalog.validate_pair(pair)
        with workflow.store.connection() as conn:
            return workflow.result(workflow.diagnosis(conn, pair), vehicle)

    @application.get("/api/admission")
    def admission(vehicle_id: Identifier, session_id: Identifier, workflow: Context):
        return workflow.admission_for(Pair(vehicle_id=vehicle_id, session_id=session_id))

    @application.post("/api/chat", response_model=ChatReply)
    def chat(message: ChatRequest, request: Request):
        return request.app.state.agent.reply(message)

    @application.post("/api/drafts", response_model=DraftView)
    def create_draft(pair: Pair, workflow: Context):
        return workflow.create_draft(pair)

    @application.get("/api/drafts", response_model=list[DraftView])
    def draft_list(vehicle_id: Identifier, session_id: Identifier, workflow: Context):
        return workflow.drafts(Pair(vehicle_id=vehicle_id, session_id=session_id))

    @application.get("/api/drafts/{draft_id}", response_model=DraftView)
    def get_draft(draft_id: str, workflow: Context):
        return workflow.get_draft(draft_id)

    @application.patch("/api/drafts/{draft_id}", response_model=DraftView)
    def edit_draft(draft_id: str, patch: DraftPatch, workflow: Context):
        return workflow.patch_draft(draft_id, patch)

    @application.post("/api/drafts/{draft_id}/confirm", response_model=DraftView)
    def confirm_draft(draft_id: str, confirmation: Confirm, workflow: Context):
        return workflow.confirm_draft(draft_id, confirmation)

    @application.post("/api/drafts/{draft_id}/reconcile", response_model=DraftView)
    def reconcile_draft(draft_id: str, reconciliation: Reconcile, workflow: Context):
        return workflow.reconcile_draft(draft_id, reconciliation)

    @application.get("/api/manuals")
    def manuals(workflow: Context):
        library = workflow.mechanic
        return [
            {
                "manual_id": manual_id,
                "manual": entry["manual"],
                "page_count": entry["page_count"],
                "applicable_engine_models": entry["applicable_engine_models"],
                "url": library.urls.get(manual_id),
            }
            for manual_id, entry in library.search.catalog.items()
        ]

    @application.get("/manuals/{manual_id}.pdf")
    def manual(manual_id: str, workflow: Context):
        path = workflow.mechanic.pdf_paths.get(manual_id)
        if path is None or not path.is_file():
            raise Problem(404, "manual_not_found", "PDF руководства отсутствует.")
        return FileResponse(path, media_type="application/pdf")

    return application


app = create_app()
