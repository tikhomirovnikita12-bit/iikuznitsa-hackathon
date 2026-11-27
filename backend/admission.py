"""One server-owned decision for preparing and delivering a reviewed operation."""
import hashlib
import json
from pathlib import Path


REGISTRY = Path(__file__).resolve().parents[1] / "data/manuals/approved-operations.json"


def plain(value):
    return value.model_dump() if hasattr(value, "model_dump") else value


def diagnosis_fingerprint(diagnosis):
    return hashlib.sha256(json.dumps(plain(diagnosis), ensure_ascii=False, sort_keys=True).encode()).hexdigest()



diagnosis_revision = diagnosis_fingerprint


def suggested_urgency(diagnosis):
    return "high" if plain(diagnosis).get("anomaly_type") == "F02" else "medium"


def incompatible_fault(vehicle, diagnosis=None):
    card = plain(vehicle)
    result = plain(diagnosis) if diagnosis is not None else card
    engine = card.get("engine_model", "")
    fuel = card.get("fuel") or ("дизель" if engine.startswith("ЯМЗ") else "бензин" if engine.startswith("ЗМЗ") else "")
    diesel = "диз" in fuel.casefold()
    return bool(result.get("anomaly_type") == "F08" and not diesel
                or result.get("anomaly_type") in {"F12", "F13"} and diesel)


def block(code, message, next_action):
    return {"code": code, "message": message, "next_action": next_action}


class AdmissionPolicy:
    def __init__(self, mechanic):
        self.mechanic = mechanic

    def approved_operations(self, diagnosis, engine):
        diagnosis = plain(diagnosis)
        if not REGISTRY.is_file():
            return []
        registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        rows = registry if isinstance(registry, list) else registry.get("operations", [])
        approved = []
        for row in rows:
            if row["engine_model"] != engine or row["anomaly_type"] != diagnosis.get("anomaly_type"):
                continue
            source = self.mechanic.search.fragments.get(row["fragment_id"])
            if not source or engine not in source["applicable_engine_models"]:
                continue
            if source["manual_id"] != row["manual_id"] or source["pdf_page"] != row["pdf_page"]:
                continue
            if not row.get("node") or not row.get("quote") or not row.get("operation"):
                continue
            if " ".join(row["quote"].split()) not in " ".join(source["text"].split()):
                continue
            supporting = [self.mechanic.search.fragments.get(key) for key in row.get("supporting_fragment_ids", [])]
            if any(not item or engine not in item["applicable_engine_models"] for item in supporting):
                continue
            paths = getattr(self.mechanic, "pdf_paths", {})
            if any(not paths.get(item["manual_id"]) or not paths[item["manual_id"]].is_file()
                   for item in [source, *supporting]):
                continue
            references = list({(item["manual"], item["pdf_page"]):
                               {"manual": item["manual"], "pdf_page": item["pdf_page"]}
                               for item in [source, *supporting]}.values())
            evidence = [self.mechanic.evidence(item["fragment_id"], engine).model_dump()
                        for item in [source, *supporting]]
            approved.append({**row, "manual": source["manual"], "references": references, "evidence": evidence,
                             "url": evidence[0]["source"]["url"]})
        return approved


def evaluate_admission(vehicle, diagnosis, fields=None, *, mechanic, draft_id=None, draft_revision=None,
                       diagnosis_revision=None, warning_acknowledged=False, applicability_confirmations=()):
    card, diagnosis = plain(vehicle), plain(diagnosis)
    if diagnosis is None:
        return {"can_prepare": False, "can_submit": False,
                "block_reason": block("diagnosis_required", "Для этой записи ещё нет результата диагностики.", "Выберите запись и дождитесь результата диагностики."),
                "warning_ack_required": False, "diagnosis_revision": None,
                "draft_id": draft_id, "draft_revision": draft_revision,
                "suggested_urgency": None, "approved_operations": []}
    fingerprint = diagnosis_fingerprint(diagnosis)
    operations = AdmissionPolicy(mechanic).approved_operations(diagnosis, card["engine_model"])
    warning = diagnosis["signal_quality"] == "warning"
    reason = None
    if diagnosis["status"] == "check_failed" or diagnosis["signal_quality"] == "bad":
        reason = block("recording_invalid", "Запись непригодна. Заключение о состоянии двигателя и отправка недоступны.",
                       "Повторите запись на посту диагностики и выберите новую сессию.")
    elif diagnosis["status"] != "anomaly":
        reason = block("anomaly_required", "В выбранной записи нет отклонения для заявки.", "Продолжайте штатный контроль двигателя.")
    elif diagnosis["confidence"] is None or diagnosis["confidence"] < 0.50:
        reason = block("confidence_too_low", "Уверенность результата ниже 0,50. Отправка заявки заблокирована.",
                       "Повторите запись и контрольную проверку; при сохранении симптомов передайте узел на очную дефектовку.")
    elif incompatible_fault(card, diagnosis):
        reason = block("fault_engine_mismatch", f"Код {diagnosis['anomaly_type']} «{diagnosis['anomaly_title']}» не соответствует двигателю {card['engine_model']} в карточке ТС.",
                       "Сверьте маркировку двигателя и принадлежность записи. Повторите проверку или передайте наблюдаемый узел на очную дефектовку.")
    elif not operations:
        reason = block("procedure_unavailable", "Не найдена проверенная применимая процедура для формирования работ заявки.",
                       f"Передайте на очную дефектовку узел по признаку «{diagnosis['anomaly_title']}». Сверьте исполнение и порядок проверки с руководством; текст рекомендации сам по себе не создаёт заявку.")
    can_prepare = reason is None
    if reason is None and ((diagnosis_revision is not None and diagnosis_revision != fingerprint)
                           or (warning and fields is not None and diagnosis_revision != fingerprint)):
        reason = block("diagnosis_changed", "Результат, качество или уверенность диагностики изменились либо подтверждение не привязано к актуальному отчёту.",
                       "Откройте актуальный отчёт и черновик, заново проверьте приоритет и подтвердите предупреждение.")
    selected = []
    if reason is None and fields is not None:
        fields = plain(fields)
        selected = [row for row in operations if row["operation"] in fields.get("operations", [])]
        if not fields.get("operations"):
            reason = block("operations_required", "Для отправки не выбрана проверенная работа.", "Выберите применимую работу из проверенных вариантов.")
        elif set(fields["operations"]) != {row["operation"] for row in selected}:
            reason = block("operation_not_approved", "Работа не связана с проверенным фрагментом для этого двигателя и узла.", "Выберите работу из проверенных вариантов. Заключение и приоритет можно изменить.")
        else:
            sources = {(reference["manual"], reference["pdf_page"])
                       for row in selected for reference in row["references"]}
            actual = {(item["manual"], item["pdf_page"]) for item in fields.get("references", [])}
            if not actual or actual != sources:
                reason = block("operation_source_mismatch", "Источник не подтверждает выбранную работу для этого узла.", "Используйте источник, закреплённый за выбранной проверенной работой.")
        if reason is None and not fields.get("conclusion"):
            reason = block("conclusion_required", "Для заявки нужно заключение механика.", "Проверьте и заполните заключение.")
    required = {condition["id"] for row in (selected or operations) for condition in row.get("conditions", [])
                if condition.get("requires_confirmation")}
    if reason is None and fields is None:
        reason = block("draft_required", "Сначала подготовьте проверенный черновик заявки.", "Подготовьте черновик и проверьте его содержимое.")
    if reason is None and required - set(applicability_confirmations):
        reason = block("applicability_required", "Условия применения выбранной процедуры ещё не подтверждены.",
                       "Подтвердите фактические условия, указанные рядом с проверенной работой. Если они не выполнены, передайте узел на очную дефектовку.")
    if reason is None and warning and not warning_acknowledged:
        reason = block("warning_ack_required", "Запись с помехами. Нужно отдельное подтверждение предупреждения.",
                       "Прочитайте предупреждение о качестве записи и явно подтвердите продолжение.")
    return {"can_prepare": can_prepare, "can_submit": reason is None, "block_reason": reason,
            "warning_ack_required": warning, "diagnosis_revision": fingerprint,
            "draft_id": draft_id, "draft_revision": draft_revision,
            "suggested_urgency": suggested_urgency(diagnosis), "approved_operations": operations}
