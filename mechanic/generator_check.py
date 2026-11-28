"""Оценка риска для F05 на ЯМЗ-236М2 по показанию механика."""

import re

from backend.admission import diagnosis_revision

from .contracts import Brief, GeneratorReading, Reply, Section
from .sources import YAMZ236


SOURCE = YAMZ236.reference(55)
QUESTION = "Что показывает амперметр на номинальных оборотах?"
RATED_SPEED = "на номинальных оборотах"
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


def reported_reading(message: str) -> tuple[GeneratorReading, bool] | None:
    text = " ".join(message.casefold().split()).strip(" .!")
    if text.startswith("результат проверки") and ":" in text:
        text = text.split(":", 1)[1].strip()
    report = text.removeprefix(RATED_SPEED + " ").removesuffix(" " + RATED_SPEED)
    result = re.fullmatch(
        r"(?:амперметр )?(?:(?P<negative>нет разряда|разряда нет|не показывает разряд)|показывает разряд|есть разряд)",
        report,
    )
    if result:
        return ("no_discharge" if result.group("negative") else "discharge"), report != text
    return None


def awaiting_reading(messages: list[dict]) -> bool:
    if not messages:
        return False
    last = messages[-1]
    return last["role"] == "assistant" and last.get("origin") == "rule" and any(
        section["kind"] == "checks" and QUESTION in section["items"] for section in last.get("sections", [])
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


def reply(value: GeneratorReading | None, *, clarify_regime: bool = False) -> Reply:
    result = brief(value)
    summary = result["rationale"]
    if clarify_regime:
        summary = "Уточните показание амперметра именно на номинальных оборотах."
    elif result["risk"] == "high":
        summary = "Предварительно высокий риск для эксплуатации. " + summary
    elif value in (None, "unknown"):
        summary = "Чтобы оценить риск, укажите фактическое показание в карточке диагностики."
    return Reply(
        summary=summary,
        sections=[Section(kind="checks", items=[QUESTION if clarify_regime else result["first_check"]], references=[SOURCE])],
        analysis={"generator_reading": None if clarify_regime else value, "risk_level": result["risk"]},
    )
