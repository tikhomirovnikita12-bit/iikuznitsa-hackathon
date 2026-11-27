"""Extractive mechanic assistant grounded in the selected engine's manuals."""

import json
import re
from collections.abc import Callable
from pathlib import Path

from manual_search.corpus import DATA_DIR, load_catalog, verified_fragments
from manual_search.search import ManualSearch

from .models import Assessment, ChatReply, Diagnosis, DraftFields, Evidence, Reference, Source


OIL_PRESSURE = "yamz-236-3902150-b-re-p019-oil-pressure"
LOW_PRESSURE_CAUSES = "yamz-236-3902150-b-re-p053-low-oil-pressure"
VALVE_CLEARANCE = "yamz-236-3902150-b-re-p031-valve-clearance"
CHAIN_NOISE = "zmz-40904-40905-rk-p107-engine-knock"


def load_defect_checks(catalog):
    manuals = {manual["manual_id"]: manual for manual in catalog}
    checks, fragments = {}, []
    data = json.loads((DATA_DIR / "defect-checks.json").read_text(encoding="utf-8"))
    for entry in data:
        model = entry["engine_model"]
        manual = manuals[entry["manual_id"]]
        checks[model] = {}
        fitment = dict.fromkeys(entry.get("check_fitment", ()), entry.get("identification"))
        for code, (page, section, text) in {**entry["checks"], **fitment}.items():
            if model not in manual["applicable_engine_models"] or not 1 <= page <= manual["page_count"]:
                raise ValueError(f"Invalid diagnostic source for {model} / {code}")
            fragment = f"{manual['manual_id']}-p{page:03d}-{model}-{code}"
            fragments.append({
                "fragment_id": fragment,
                "manual_id": manual["manual_id"],
                "manual": manual["manual"],
                "source_file": manual["source_file"],
                "pdf_page": page,
                "section": section,
                "text": text,
                "applicable_engine_models": [model],
            })
            operation = text
            if code in fitment:
                operation = ("Сверить маркировку двигателя и фактическую комплектацию узла с карточкой ТС. "
                             "Уточнить применимость кода ML перед назначением работ по узлу.")
            checks[model][code] = (fragment, operation, code in fitment)
    return checks, fragments


def answer_from_evidence(evidence: list[Evidence], compose: Callable[[list[Evidence]], str | None] | None) -> ChatReply:
    answer = compose(evidence) if compose else None
    return ChatReply(answer=answer or "\n\n".join(item.text for item in evidence), evidence=evidence)


class Mechanic:
    def __init__(self, index_path: Path, pdf_directory: Path):
        catalog = load_catalog()
        self.defect_checks, checks = load_defect_checks(catalog)
        curated = [*verified_fragments(catalog), *checks]
        curated_ids = {entry["fragment_id"] for entry in curated}
        extracted = []
        if index_path.is_file():
            corpus = json.loads(index_path.read_text(encoding="utf-8"))
            extracted = [entry for entry in corpus["fragments"] if entry["fragment_id"] not in curated_ids]
        self.search = ManualSearch({"fragments": [*extracted, *curated]})
        self.pdf_paths = {
            manual_id: pdf_directory / manual["source_file"]
            for manual_id, manual in self.search.catalog.items()
        }
        self.urls = {
            manual_id: f"/manuals/{manual_id}.pdf"
            for manual_id, path in self.pdf_paths.items() if path.is_file()
        }
        self.pages_by_engine = {
            (entry["manual"], entry["pdf_page"], model)
            for entry in self.search.fragments.values()
            for model in entry["applicable_engine_models"]
        }

    def reference_applies(self, reference: Reference, engine_model: str) -> bool:
        return (reference.manual, reference.pdf_page, engine_model) in self.pages_by_engine

    def evidence(self, fragment_id: str, engine_model: str) -> Evidence:
        entry = self.search.fragments[fragment_id]
        reference = self.search.resolve_source(fragment_id, engine_model, self.urls)
        return Evidence(section=entry["section"], text=entry["text"],
                        source=Source(fragment_id=fragment_id, **reference))

    def assess(self, diagnosis: Diagnosis, engine_model: str) -> Assessment:
        if diagnosis.status == "check_failed":
            return Assessment(conclusion=None, next_step="Повторить запись двигателя.")
        if diagnosis.status == "normal":
            return Assessment(conclusion="Значимых отклонений в записи не обнаружено.",
                              next_step="При наличии жалоб сопоставить запись с наблюдениями механика.")

        conclusion = f"Предполагаемое отклонение: {diagnosis.anomaly_title}. Требуется подтверждение измерениями."
        if engine_model == "ЯМЗ-236М2" and diagnosis.anomaly_type == "F02":
            return Assessment(
                conclusion=conclusion,
                next_step="Уточнить фактическое давление масла и условия его измерения.",
                risk="Высокий риск, если измеренное давление ниже применимого допуска. "
                     "Для оценки нужны фактическое давление и режим работы (стр. 19).",
                possible_causes=["Неисправность манометра, разжижение масла топливом, загрязнение фильтра и другие причины на стр. 53."],
                checks=["Сверить уровень масла и охлаждающей жидкости (стр. 53).",
                        "Проверить давление на прогретом двигателе в режимах, указанных на стр. 19."],
                repair_actions=["Выбрать способ устранения из таблицы на стр. 53 после подтверждения причины."],
                evidence=[self.evidence(fragment, engine_model) for fragment in (OIL_PRESSURE, LOW_PRESSURE_CAUSES)],
            )
        if engine_model == "ЯМЗ-236М2" and diagnosis.anomaly_type == "F01":
            return Assessment(
                conclusion=conclusion,
                next_step="Сопоставить признаки с фактическими тепловыми зазорами.",
                risk="Степень риска зависит от величины отклонения и состояния клапанного механизма.",
                possible_causes=["Отклонение теплового зазора от допуска требует проверки."],
                checks=["Измерить тепловые зазоры впускных и выпускных клапанов по условиям на стр. 31."],
                repair_actions=["При подтверждении измерениями отрегулировать зазоры по руководству (стр. 31)."],
                evidence=[self.evidence(VALVE_CLEARANCE, engine_model)],
            )
        if engine_model in {"ЗМЗ-40904.10", "ЗМЗ-40905.10"} and diagnosis.anomaly_type == "F14":
            return Assessment(
                conclusion=conclusion,
                next_step="Проверить, совпадает ли наблюдаемый шум с описанием в руководстве.",
                risk="Для оценки риска нужно установить причину шума и состояние привода ГРМ.",
                possible_causes=["Повышенное удлинение цепи. Неисправность гидронатяжителя указана отдельной причиной."],
                checks=["Проверить увеличение длины цепей (стр. 107)."],
                repair_actions=["Руководство предусматривает замену цепей со звёздочками комплектом при необходимости (стр. 107)."],
                evidence=[self.evidence(CHAIN_NOISE, engine_model)],
            )

        check = self.defect_checks.get(engine_model, {}).get(diagnosis.anomaly_type)
        if check:
            fragment, operation, check_fitment = check
            return Assessment(
                conclusion=(f"ML отметил отклонение «{diagnosis.anomaly_title}». "
                            "Применимость кода к комплектации требует сверки." if check_fitment else conclusion),
                next_step=operation,
                risk="Степень риска по одной акустической проверке определить нельзя.",
                checks=[operation],
                evidence=[self.evidence(fragment, engine_model)],
            )

        hits = self.search.search(diagnosis.anomaly_title, engine_model, diagnosis.anomaly_type, limit=2)
        evidence = [self.evidence(hit["fragment_id"], engine_model) for hit in hits]
        return Assessment(
            conclusion=conclusion,
            next_step=("Механику нужно сверить признаки и применимость найденных разделов."
                       if evidence else "По имеющимся материалам данных для рекомендации недостаточно."),
            risk="Степень риска по одной акустической проверке определить нельзя.",
            evidence=evidence,
        )

    def suggest_draft(self, diagnosis: Diagnosis, engine_model: str) -> DraftFields:
        assessment = self.assess(diagnosis, engine_model)
        return DraftFields(
            conclusion=assessment.conclusion,
            operations=assessment.checks,
            references=[
                Reference(manual=item.source.manual, pdf_page=item.source.pdf_page)
                for item in assessment.evidence
            ],
        )

    def answer(self, question: str, diagnosis: Diagnosis, engine_model: str, *,
               search_query: str | None = None,
               compose: Callable[[list[Evidence]], str | None] | None = None) -> ChatReply:
        normalized = question.casefold().replace("ё", "е")
        assess_pressure = re.search(r"\d+[\s,]*(кпа|мпа|бар|кгс)", normalized)
        travel = re.search(r"перегн|своим ходом|можно ли (ехать|доехать|продолжать)", normalized)
        if travel or (engine_model == "ЯМЗ-236М2" and diagnosis.anomaly_type == "F02" and assess_pressure):
            if engine_model == "ЯМЗ-236М2" and diagnosis.anomaly_type == "F02":
                return ChatReply(
                    answer="Для оценки нужны фактическое давление, обороты, степень прогрева и сведения "
                           "о применимости допуска после длительной эксплуатации. Сверьте условия со стр. 19. "
                           "Разрешение на эксплуатацию по этим данным дать нельзя.",
                    needs_clarification=True,
                    evidence=[self.evidence(OIL_PRESSURE, engine_model)],
                )
            return ChatReply(answer="Для оценки возможности эксплуатации нужна очная проверка механиком.",
                             needs_clarification=True)

        if (search_query and engine_model == "ЯМЗ-236М2"
                and re.search(r"давлени\w* масла", search_query.casefold())
                and re.search(r"холост|оборот|прогрев|температур|услови", normalized)):
            source = self.evidence(OIL_PRESSURE, engine_model)
            return answer_from_evidence([source], compose)

        assessment = self.assess(diagnosis, engine_model)
        if re.fullmatch(r"(почему (это )?опасно|чем (это )?грозит|какой риск)[?.! ]*", normalized):
            return ChatReply(answer=assessment.risk or assessment.next_step,
                             needs_clarification=not assessment.evidence, evidence=assessment.evidence)
        if re.fullmatch(r"(что проверить( сначала)?|с чего начать|что делать дальше)[?.! ]*", normalized):
            return ChatReply(answer=" ".join(assessment.checks) or assessment.next_step,
                             needs_clarification=not assessment.checks, evidence=assessment.evidence)
        if re.fullmatch(r"(какие (возможны )?причины|что могло это вызвать)[?.! ]*", normalized):
            if assessment.possible_causes:
                return ChatReply(answer=" ".join(assessment.possible_causes), evidence=assessment.evidence)
            search_query = search_query or diagnosis.anomaly_title
        hits = self.search.search(search_query or question, engine_model, diagnosis.anomaly_type, limit=1)
        if not hits:
            return ChatReply(
                answer="В доступных руководствах для выбранного двигателя ответа нет. "
                       "Уточни узел, наблюдаемый признак или условия проверки.",
                needs_clarification=True,
            )
        evidence = [self.evidence(hits[0]["fragment_id"], engine_model)]
        for fragment_id in hits[0].get("continues_from", [])[:1]:
            previous = self.search.fragments.get(fragment_id)
            if previous and engine_model in previous["applicable_engine_models"]:
                evidence.insert(0, self.evidence(fragment_id, engine_model))
        return answer_from_evidence(evidence, compose)
