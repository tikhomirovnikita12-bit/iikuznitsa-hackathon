"""Adapt the team's native catalog and ticket workflow to the mechanic screen."""

import os
import sys
from pathlib import Path

import httpx

from .config import CORPUS, MATERIALS, ROOT


TICKET_STATES = {
    "draft": "draft", "sending": "sending", "sent": "created",
    "delivery_failed": "error", "delivery_unknown": "unknown",
}
DRAFT_FIELDS = {"vehicle_id", "session_id", "anomaly_type", "conclusion", "urgency",
                "operations", "spare_parts", "references"}


def team_checkout() -> Path | None:
    configured = os.environ.get("TEAM_WORKFLOW_DIR")
    if configured == "":
        return None
    path = ROOT / (configured or ".")
    if not (path / "backend" / "workflow.py").is_file():
        if configured:
            raise ValueError("TEAM_WORKFLOW_DIR должен указывать на командный бэкенд")
        return None
    return path


class TeamWorkflow:
    def __init__(self, checkout, catalog_path, ml_url, toir_url, timeout):
        sys.path.insert(0, str(checkout.resolve()))
        from backend import models
        from backend.mechanic import Mechanic
        from backend.models import Confirm, DraftPatch, Pair
        from backend.store import Store
        from backend.upstream import Problem
        from backend.workflow import Catalog, Workflow

        database = ROOT / os.environ.get("TEAM_DB_PATH",
                                         os.environ.get("MECHANIC_DB_PATH", "artifacts/mechanic.sqlite3"))
        self.Pair, self.Patch, self.Confirm, self.Problem = Pair, DraftPatch, Confirm, Problem
        self.Reconcile = getattr(models, "Reconcile", None)
        self.ml = httpx.Client(base_url=ml_url, timeout=timeout, trust_env=False)
        self.toir = httpx.Client(base_url=toir_url, timeout=timeout, trust_env=False)
        self.workflow = Workflow(
            Catalog(catalog_path), Store(database), Mechanic(CORPUS, MATERIALS),
            self.ml, self.toir, sending_deadline=max(60, 4 * timeout),
        )

    def close(self):
        self.ml.close()
        self.toir.close()

    def catalog(self, metadata):
        return [
            {**metadata.get(plate, {}), **vehicle.model_dump(),
             "sessions": [session.model_dump() for session in self.workflow.catalog.sessions[plate].values()]}
            for plate, vehicle in sorted(self.workflow.catalog.vehicles.items())
        ]

    def pair(self, pair):
        return self.Pair(**pair.model_dump(include={"vehicle_id", "session_id"}))

    def diagnose(self, pair):
        return self.workflow.diagnose(self.pair(pair)).diagnosis.model_dump()

    def stored_diagnosis(self, pair):
        try:
            with self.workflow.store.connection() as connection:
                return self.workflow.diagnosis(connection, self.pair(pair)).model_dump()
        except self.Problem as error:
            if error.code != "diagnosis_required":
                raise
            return None

    def current_draft(self, pair):
        diagnosis = self.stored_diagnosis(pair)
        if not diagnosis or diagnosis["status"] != "anomaly":
            return None
        drafts = self.workflow.drafts(self.pair(pair))
        return next((draft for draft in reversed(drafts)
                     if draft.anomaly_type == diagnosis["anomaly_type"]), None)

    def prepare(self, pair, fields):
        draft = self.workflow.create_draft(self.pair(pair))
        if draft.state != "draft" or draft.revision != 1:
            return
        try:
            self.workflow.patch_draft(draft.draft_id, self.Patch(revision=draft.revision, **fields.model_dump()))
        except self.Problem as error:
            if error.code not in ("stale_draft", "draft_locked"):
                raise

    def patch(self, draft, request):
        edits = request.edits.model_dump()
        if request.revision is None or request.revision != draft.revision:
            raise self.Problem(409, "stale_draft", "Черновик изменился. Проверьте актуальную версию.", draft=draft)
        if draft.model_dump(include=set(edits)) == edits:
            return draft
        return self.workflow.patch_draft(draft.draft_id, self.Patch(revision=request.revision, **edits))

    def submit(self, draft, request):
        if draft.state in ("sending", "sent", "delivery_unknown"):
            return draft
        draft = self.patch(draft, request)
        try:
            return self.workflow.confirm_draft(
                draft.draft_id, self.Confirm(revision=draft.revision, urgency=request.edits.urgency, confirmed=True),
            )
        except self.Problem as error:
            if error.draft and error.draft.state in ("delivery_failed", "delivery_unknown"):
                return error.draft
            raise

    @property
    def can_reconcile(self):
        return self.Reconcile is not None and hasattr(self.workflow, "reconcile_draft")

    def reconcile(self, draft, request):
        fields = request.model_dump(include={"revision", "ticket_id", "verified"})
        return self.workflow.reconcile_draft(draft.draft_id, self.Reconcile(**fields))

    def populate(self, state, pair, *, restore_diagnosis=False):
        if restore_diagnosis:
            actual = self.stored_diagnosis(pair)
            if actual != state["diagnosis"]:
                previous = state["diagnosis"]
                state.update(diagnosis=actual, brief=None, brief_error=None,
                             draft_origin=None, draft_unavailable_reason=None)
                if previous and (previous["status"], previous["anomaly_type"]) != (
                    (actual["status"], actual["anomaly_type"]) if actual else (None, None)
                ):
                    state.update(messages=[], agent_history=[])
        draft = self.current_draft(pair)
        if draft:
            error = draft.delivery_error
            message = error.message if error else None
            if draft.state == "delivery_unknown":
                message = "Результат отправки неизвестен. " + (message or "Сверьте заявку в ТОиР.")
            state.update(draft=draft.model_dump(include=DRAFT_FIELDS), draft_id=draft.draft_id,
                         draft_revision=draft.revision)
            state["ticket"] = {
                "status": TICKET_STATES[draft.state], "ticket_id": draft.ticket_id,
                "message": message,
            }
        else:
            state.update(draft=None, draft_id=None, draft_revision=None, draft_origin=None)
            state["ticket"] = {"status": "idle", "ticket_id": None, "message": None}
        return state
