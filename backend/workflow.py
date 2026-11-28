"""Diagnosis and draft state transitions shared by the HTTP routes."""

import json
import time
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from .admission import diagnosis_revision, evaluate_admission, suggested_urgency
from .mechanic import Mechanic
from .models import (
    Confirm, DeliveryError, Diagnosis, DiagnosticResult, DraftFields, DraftPatch,
    DraftView, Pair, Reconcile, Reference, Session, Ticket, TicketReceipt, Vehicle,
)
from .store import Store
from .upstream import Problem, post


class Catalog:
    def __init__(self, path: Path):
        data = json.loads(path.read_text(encoding="utf-8"))
        self.fuels = {item["vehicle_id"]: item.get("fuel", "") for item in data["vehicles"]}
        self.vehicles = {}
        self.sessions = {}
        for item in data["vehicles"]:
            vehicle = Vehicle.model_validate(item)
            if vehicle.vehicle_id in self.vehicles:
                raise ValueError(f"Duplicate vehicle: {vehicle.vehicle_id}")
            self.vehicles[vehicle.vehicle_id] = vehicle
            sessions = {}
            for source in item["sessions"]:
                session = Session.model_validate(source)
                if source["vehicle_id"] != vehicle.vehicle_id or session.session_id in sessions:
                    raise ValueError(f"Invalid catalog session: {session.session_id}")
                sessions[session.session_id] = session
            self.sessions[vehicle.vehicle_id] = sessions

    def vehicle(self, vehicle_id: str) -> Vehicle:
        vehicle = self.vehicles.get(vehicle_id)
        if vehicle is None:
            raise Problem(404, "vehicle_not_found", "Транспортное средство не найдено.")
        return vehicle

    def validate_pair(self, pair: Pair) -> Vehicle:
        vehicle = self.vehicle(pair.vehicle_id)
        if pair.session_id not in self.sessions[pair.vehicle_id]:
            raise Problem(404, "session_not_found", "Сессия не принадлежит выбранному транспортному средству.")
        return vehicle


class Workflow:
    def __init__(self, catalog: Catalog, store: Store, mechanic: Mechanic,
                 ml: httpx.Client, toir: httpx.Client, sending_deadline: float):
        self.catalog = catalog
        self.store = store
        self.mechanic = mechanic
        self.ml = ml
        self.toir = toir
        self.sending_deadline = sending_deadline

    def diagnose(self, pair: Pair) -> DiagnosticResult:
        vehicle = self.catalog.validate_pair(pair)
        key = (pair.vehicle_id, pair.session_id)
        with self.store.connection(write=True) as conn:
            conn.execute("""
                INSERT INTO diagnosis_requests (vehicle_id, session_id, started_version) VALUES (?, ?, 1)
                ON CONFLICT (vehicle_id, session_id) DO UPDATE SET started_version = started_version + 1
            """, key)
            version = conn.execute("""
                SELECT started_version FROM diagnosis_requests WHERE vehicle_id = ? AND session_id = ?
            """, key).fetchone()[0]
        diagnosis = post(self.ml, "ml", "/diagnose", pair, Diagnosis, 200)
        if (diagnosis.vehicle_id, diagnosis.session_id) != (pair.vehicle_id, pair.session_id):
            raise Problem(502, "ml_pair_mismatch", "ML-сервис вернул результат другой пары ТС и сессии.")
        result = self.result(diagnosis, vehicle)
        with self.store.connection(write=True) as conn:
            is_latest = conn.execute("""
                UPDATE diagnosis_requests SET saved_version = ?
                WHERE vehicle_id = ? AND session_id = ? AND saved_version < ?
            """, (version, *key, version)).rowcount == 1
            if is_latest:
                conn.execute("""
                    INSERT INTO diagnoses (vehicle_id, session_id, payload) VALUES (?, ?, ?)
                    ON CONFLICT (vehicle_id, session_id) DO UPDATE SET payload = excluded.payload
                """, (*key, diagnosis.model_dump_json()))
                # Changes to quality, confidence or the model invalidate human confirmations.
                fingerprint = diagnosis_revision(diagnosis)
                conn.execute("""
                    UPDATE drafts SET diagnosis_revision = ?, revision = revision + 1,
                        applicability_confirmations = '[]', confirmed_urgency = NULL
                    WHERE vehicle_id = ? AND session_id = ? AND anomaly_type = ?
                        AND state IN ('draft', 'delivery_failed')
                        AND diagnosis_revision IS NOT NULL AND diagnosis_revision IS NOT ?
                """, (fingerprint, *key, diagnosis.anomaly_type, fingerprint))
            else:
                diagnosis = self.diagnosis(conn, pair)
        return result if is_latest else self.result(diagnosis, vehicle)

    def admission_for(self, pair, fields=None, **kwargs):
        vehicle = self.catalog.validate_pair(pair)
        with self.store.connection() as conn:
            diagnosis = self.diagnosis(conn, pair)
        return evaluate_admission(vehicle, diagnosis, fields, mechanic=self.mechanic, **kwargs)

    @staticmethod
    def require_admission(admission, *, prepare=False, draft=None):
        if admission["can_prepare" if prepare else "can_submit"]:
            return
        reason = admission["block_reason"]
        raise Problem(422, reason["code"], reason["message"] + " " + reason["next_action"],
                      draft=draft, admission=admission)

    def result(self, diagnosis: Diagnosis, vehicle: Vehicle) -> DiagnosticResult:
        return DiagnosticResult(engine_model=vehicle.engine_model, diagnosis=diagnosis,
                                assessment=self.mechanic.assess(diagnosis, vehicle.engine_model),
                                admission=evaluate_admission(vehicle, diagnosis, mechanic=self.mechanic))

    def diagnosis(self, conn, pair: Pair) -> Diagnosis:
        row = conn.execute(
            "SELECT payload FROM diagnoses WHERE vehicle_id = ? AND session_id = ?",
            (pair.vehicle_id, pair.session_id),
        ).fetchone()
        if row is None:
            raise Problem(409, "diagnosis_required", "Сначала запросите диагностику для этой сессии.")
        return Diagnosis.model_validate_json(row["payload"])

    def view(self, row) -> DraftView:
        error = None
        if row["error_code"]:
            error = DeliveryError(code=row["error_code"], message=row["error_message"])
        draft = DraftView(
            **json.loads(row["fields"]),
            draft_id=row["draft_id"], vehicle_id=row["vehicle_id"], session_id=row["session_id"],
            anomaly_type=row["anomaly_type"], revision=row["revision"], state=row["state"],
            ticket_id=row["ticket_id"], delivery_error=error,
            diagnosis_revision=row["diagnosis_revision"], suggested_urgency=row["suggested_urgency"],
            confirmed_urgency=row["confirmed_urgency"],
            applicability_confirmations=json.loads(row["applicability_confirmations"]),
        )
        draft.admission = self.admission_for(
            draft, DraftFields.model_validate(json.loads(row["fields"])), draft_id=draft.draft_id,
            draft_revision=draft.revision, diagnosis_revision=draft.diagnosis_revision,
            applicability_confirmations=draft.applicability_confirmations,
        )
        return draft

    @staticmethod
    def row(conn, draft_id: str):
        row = conn.execute("""
            SELECT drafts.*, submitted_tickets.ticket_id
            FROM drafts LEFT JOIN submitted_tickets USING (draft_id)
            WHERE drafts.draft_id = ?
        """, (draft_id,)).fetchone()
        if row is None:
            raise Problem(404, "draft_not_found", "Черновик не найден.")
        return row

    def create_draft(self, pair: Pair, *, expected_diagnosis: Diagnosis | None = None) -> DraftView:
        vehicle = self.catalog.validate_pair(pair)
        with self.store.connection(write=True) as conn:
            diagnosis = self.diagnosis(conn, pair)
            if expected_diagnosis is not None and diagnosis != expected_diagnosis:
                raise Problem(409, "diagnosis_changed", "Диагностика изменилась. Обновите результат проверки.")
            admission = evaluate_admission(vehicle, diagnosis, mechanic=self.mechanic)
            self.require_admission(admission, prepare=True)
            fields = self.mechanic.suggest_draft(diagnosis, vehicle.engine_model)
            fields.operations = [operation["operation"] for operation in admission["approved_operations"]]
            fields.references = self.operation_references(admission["approved_operations"])
            conn.execute("""
                INSERT INTO drafts (draft_id, vehicle_id, session_id, anomaly_type, fields,
                    diagnosis_revision, suggested_urgency)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (vehicle_id, session_id, anomaly_type) DO NOTHING
            """, (uuid4().hex, pair.vehicle_id, pair.session_id, diagnosis.anomaly_type, fields.model_dump_json(),
                  diagnosis_revision(diagnosis), suggested_urgency(diagnosis)))
            draft_id = conn.execute("""
                SELECT draft_id FROM drafts WHERE vehicle_id = ? AND session_id = ? AND anomaly_type = ?
            """, (pair.vehicle_id, pair.session_id, diagnosis.anomaly_type)).fetchone()["draft_id"]
        return self.get_draft(draft_id)

    def refresh_delivery(self, draft_id: str) -> None:
        with self.store.connection() as conn:
            row = self.row(conn, draft_id)
        cutoff = time.time() - self.sending_deadline
        if row["state"] == "sending" and row["started_at"] < cutoff:
            with self.store.connection(write=True) as conn:
                conn.execute("""
                    UPDATE drafts SET state = 'delivery_unknown',
                        error_code = 'delivery_interrupted', error_message = ?
                    WHERE draft_id = ? AND state = 'sending' AND started_at < ?
                """, ("Нет подтверждения от ТОиР. Найдите заявку в ТОиР и внесите её номер.", draft_id, cutoff))

    @staticmethod
    def operation_references(operations):
        return [Reference(**reference) for reference in {
            (reference["manual"], reference["pdf_page"]): reference
            for operation in operations for reference in operation["references"]
        }.values()]

    def get_draft(self, draft_id: str) -> DraftView:
        self.refresh_delivery(draft_id)
        with self.store.connection(write=True) as conn:
            row = self.row(conn, draft_id)
            if row["diagnosis_revision"] is None and row["state"] in ("draft", "delivery_failed"):
                pair = Pair(vehicle_id=row["vehicle_id"], session_id=row["session_id"])
                diagnosis = self.diagnosis(conn, pair)
                vehicle = self.catalog.validate_pair(pair)
                admission = evaluate_admission(vehicle, diagnosis, mechanic=self.mechanic)
                if admission["can_prepare"] and row["anomaly_type"] == diagnosis.anomaly_type:
                    fields = DraftFields.model_validate_json(row["fields"])
                    fields.operations = [operation["operation"] for operation in admission["approved_operations"]]
                    fields.references = self.operation_references(admission["approved_operations"])
                    conn.execute("""
                        UPDATE drafts SET fields = ?, diagnosis_revision = ?, suggested_urgency = ?,
                            revision = revision + 1, applicability_confirmations = '[]', confirmed_urgency = NULL
                        WHERE draft_id = ?
                    """, (fields.model_dump_json(), diagnosis_revision(diagnosis), suggested_urgency(diagnosis), draft_id))
                    row = self.row(conn, draft_id)
            return self.view(row)

    def drafts(self, pair: Pair) -> list[DraftView]:
        self.catalog.validate_pair(pair)
        with self.store.connection() as conn:
            ids = conn.execute("""
                SELECT draft_id FROM drafts WHERE vehicle_id = ? AND session_id = ? ORDER BY rowid
            """, (pair.vehicle_id, pair.session_id)).fetchall()
        return [self.get_draft(row["draft_id"]) for row in ids]

    def check_sources(self, fields: DraftFields | Ticket, vehicle: Vehicle):
        for source in fields.references:
            if not self.mechanic.reference_applies(source, vehicle.engine_model):
                raise Problem(422, "invalid_reference",
                              "Указанная страница отсутствует в индексе для двигателя выбранного ТС.")

    def patch_draft(self, draft_id: str, patch: DraftPatch) -> DraftView:
        self.refresh_delivery(draft_id)
        with self.store.connection(write=True) as conn:
            row = self.row(conn, draft_id)
            draft = self.view(row)
            if draft.state not in ("draft", "delivery_failed"):
                raise Problem(409, "draft_locked", "Черновик закрыт для правок.", draft=draft)
            if draft.revision != patch.revision:
                raise Problem(409, "stale_draft", "Черновик изменился. Загрузите актуальную версию.", draft=draft)
            updates = patch.model_dump(exclude_unset=True, exclude={"revision", "applicability_confirmations"})
            fields = DraftFields.model_validate({**json.loads(row["fields"]), **updates})
            vehicle = self.catalog.vehicle(draft.vehicle_id)
            self.check_sources(fields, vehicle)
            confirmations = (patch.applicability_confirmations if "applicability_confirmations" in patch.model_fields_set
                             else draft.applicability_confirmations)
            diagnosis = self.diagnosis(conn, draft)
            admission = evaluate_admission(vehicle, diagnosis, fields, mechanic=self.mechanic,
                draft_id=draft.draft_id, draft_revision=draft.revision,
                diagnosis_revision=draft.diagnosis_revision, applicability_confirmations=confirmations)
            if admission["block_reason"] and admission["block_reason"]["code"] in {
                "operation_not_approved", "operation_source_mismatch", "spare_part_not_approved", "diagnosis_changed",
            }:
                self.require_admission(admission, draft=draft)
            conn.execute("""
                UPDATE drafts SET fields = ?, revision = revision + 1, state = 'draft',
                    applicability_confirmations = ?, confirmed_urgency = NULL,
                    error_code = NULL, error_message = NULL WHERE draft_id = ?
            """, (fields.model_dump_json(), json.dumps(confirmations), draft_id))
            return self.view(self.row(conn, draft_id))

    def confirm_draft(self, draft_id: str, confirmation: Confirm) -> DraftView:
        if not confirmation.confirmed:
            raise Problem(422, "confirmation_required", "Механик должен подтвердить черновик и приоритет.")
        self.refresh_delivery(draft_id)
        attempt_id = uuid4().hex
        with self.store.connection(write=True) as conn:
            row = self.row(conn, draft_id)
            draft = self.view(row)
            if draft.state in ("sent", "sending"):
                return draft
            if draft.state == "delivery_unknown":
                raise Problem(409, "delivery_unknown",
                              "Результат отправки неизвестен. Сверьте его с ТОиР.", draft=draft)
            if draft.revision != confirmation.revision:
                raise Problem(409, "stale_draft", "Черновик изменился. Загрузите актуальную версию.", draft=draft)

            pair = Pair(vehicle_id=draft.vehicle_id, session_id=draft.session_id)
            vehicle = self.catalog.validate_pair(pair)
            diagnosis = self.diagnosis(conn, pair)
            if diagnosis.status != "anomaly" or draft.anomaly_type != diagnosis.anomaly_type:
                raise Problem(409, "diagnosis_changed", "Диагностика изменилась. Проверьте результат этой сессии.", draft=draft)
            fields = DraftFields.model_validate({**json.loads(row["fields"]), "urgency": confirmation.urgency})
            admission = evaluate_admission(vehicle, diagnosis, fields, mechanic=self.mechanic,
                draft_id=draft.draft_id, draft_revision=draft.revision,
                diagnosis_revision=confirmation.diagnosis_revision or draft.diagnosis_revision,
                warning_acknowledged=confirmation.warning_acknowledged,
                applicability_confirmations=confirmation.applicability_confirmations or draft.applicability_confirmations)
            if diagnosis.signal_quality == "warning" and confirmation.diagnosis_revision != admission["diagnosis_revision"]:
                raise Problem(409, "diagnosis_changed", "Откройте актуальный отчёт и заново подтвердите предупреждение.",
                              draft=draft, admission=admission)
            self.require_admission(admission, draft=draft)
            try:
                ticket = Ticket(**pair.model_dump(), anomaly_type=draft.anomaly_type, **fields.model_dump())
            except ValidationError as error:
                missing = ", ".join(dict.fromkeys(str(issue["loc"][0]) for issue in error.errors()))
                raise Problem(422, "invalid_ticket", f"Проверьте обязательные поля заявки: {missing}.",
                              draft=draft) from error
            self.check_sources(ticket, vehicle)
            conn.execute("""
                UPDATE drafts SET fields = ?, state = 'sending', started_at = ?, attempt_id = ?,
                    confirmed_urgency = ?, applicability_confirmations = ?,
                    error_code = NULL, error_message = NULL WHERE draft_id = ?
            """, (fields.model_dump_json(), time.time(), attempt_id, confirmation.urgency,
                  json.dumps(confirmation.applicability_confirmations or draft.applicability_confirmations), draft_id))

        try:
            receipt = post(self.toir, "toir", "/tickets", ticket, TicketReceipt, 201)
        except Problem as problem:
            state = "delivery_unknown" if problem.uncertain else "delivery_failed"
            message = str(problem)
            if problem.uncertain:
                message += " Заявка могла быть создана. Сверьте результат с ТОиР."
            failure = Problem(problem.status, problem.code, message)
            failure.draft = self.finish_send(draft_id, attempt_id, state, error=failure)
            raise failure from problem

        return self.finish_send(draft_id, attempt_id, "sent", receipt=receipt, payload=ticket)

    def reconcile_draft(self, draft_id: str, reconciliation: Reconcile) -> DraftView:
        if not reconciliation.verified:
            raise Problem(422, "verification_required", "Подтвердите, что номер сверили в ТОиР.")
        self.refresh_delivery(draft_id)
        with self.store.connection(write=True) as conn:
            row = self.row(conn, draft_id)
            draft = self.view(row)
            if draft.state == "sent" and draft.ticket_id == reconciliation.ticket_id:
                return draft
            if draft.state != "delivery_unknown":
                raise Problem(409, "draft_not_reconcilable", "Внести номер можно только после неизвестного результата отправки.",
                              draft=draft)
            if draft.revision != reconciliation.revision:
                raise Problem(409, "stale_draft", "Черновик изменился. Загрузите актуальную версию.", draft=draft)

            ticket = Ticket(
                vehicle_id=draft.vehicle_id, session_id=draft.session_id,
                anomaly_type=draft.anomaly_type, **json.loads(row["fields"]),
            )
            conn.execute("""
                UPDATE drafts SET state = 'sent', revision = revision + 1,
                    error_code = NULL, error_message = NULL WHERE draft_id = ?
            """, (draft_id,))
            self.record_ticket(conn, draft_id, reconciliation.ticket_id, ticket)
            return self.view(self.row(conn, draft_id))

    @staticmethod
    def record_ticket(conn, draft_id: str, ticket_id: str, payload: Ticket) -> None:
        conn.execute("""
            INSERT INTO submitted_tickets (draft_id, ticket_id, payload) VALUES (?, ?, ?)
        """, (draft_id, ticket_id, payload.model_dump_json()))

    def finish_send(self, draft_id, attempt_id, state, *, receipt=None, payload=None, error=None) -> DraftView:
        with self.store.connection(write=True) as conn:
            updated = conn.execute("""
                UPDATE drafts SET state = ?, error_code = ?, error_message = ?
                WHERE draft_id = ? AND attempt_id = ? AND state IN ('sending', 'delivery_unknown')
            """, (state, error.code if error else None, str(error) if error else None, draft_id, attempt_id))
            if updated.rowcount and receipt is not None:
                self.record_ticket(conn, draft_id, receipt.ticket_id, payload)
            return self.view(self.row(conn, draft_id))
