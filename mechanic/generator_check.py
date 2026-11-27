"""Оценка риска для F05 на ЯМЗ-236М2 по показанию механика."""

import re

from backend.admission import diagnosis_revision

from .contracts import Brief, GeneratorReading, Reply, Section
from .sources import YAMZ236


SOURCE = YAMZ236.reference(55)
QUESTION = "Что показывает амперметр на номинальных оборотах?"
INSPECTION = "При заглушенном двигателе проверьте натяжение ремней привода и состояние подшипников генератора."
REPORTS = {
    "discharge": "На номинальных оборотах амперметр показывает разряд.",
    "no_discharge": "На номинальных оборотах амперметр не показывает разряд.",
    "unknown": "Показание амперметра на номинальных оборотах пока неизвестно.",
}
TOPIC = re.compile(
    r"риск|ехать|в рейс|перегон|своим ходом|разряд|заряд|амперметр|генератор|"
    r"что (?:провер|делать|дальше)|что .{0,25}провер|с чего начать|гипотез|причин.*отклон"
)


def applies(vehicle: dict, diagnosis: dict | None) -> bool:
    return bool(
        diagnosis and diagnosis["status"] == "anomaly" and diagnosis["anomaly_type"] == "F05"
        and diagnosis["signal_quality"] != "bad" and vehicle["engine_model"] in YAMZ236.engines
    )


def session_check(vehicle: dict, state: dict) -> dict | None:
    if not applies(vehicle, state["diagnosis"]):
        return None
    revision = diagnosis_revision(state["diagnosis"])
    saved = state["generator_reading"]
    value = saved["value"] if saved and saved["diagnosis_revision"] == revision else None
    return {"value": value, "diagnosis_revision": revision}


def brief(value: GeneratorReading | None) -> dict:
    risk = "unassessed"
    first_check = QUESTION
    if value == "discharge":
        # Политика стенда: продолжающийся разряд требует дефектовки до решения о выпуске.
        risk = "high"
        rationale = (
            "Механик подтвердил разряд на номинальных оборотах. "
            "При длительной работе с разрядом может не хватить заряда для электросистем и повторного пуска."
        )
        first_check = INSPECTION
    elif value == "no_discharge":
        rationale = (
            "Разряд на номинальных оборотах не наблюдался. "
            "Для оценки риска по шуму нужен осмотр привода генератора."
        )
        first_check = INSPECTION
    else:
        rationale = (
            "Руководство указывает разряд на номинальных оборотах как признак неисправности привода генератора. "
            "Фактическое показание пока неизвестно."
        )
    return {"origin": "rule", **Brief(
        risk=risk, rationale=rationale, first_check=first_check, references=[SOURCE],
    ).model_dump()}


def reply(value: GeneratorReading | None) -> Reply:
    result = brief(value)
    summary = result["rationale"]
    if result["risk"] == "high":
        summary = "Предварительно высокий риск для эксплуатации. " + summary
    elif value in (None, "unknown"):
        summary = "Чтобы оценить риск, укажите фактическое показание в карточке диагностики."
    return Reply(
        summary=summary,
        sections=[Section(kind="checks", items=[result["first_check"]], references=[SOURCE])],
        analysis={"generator_reading": value, "risk_level": result["risk"]},
    )
