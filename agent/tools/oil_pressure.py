"""Compare operator readings with the verified YaMZ-236M2 operating manual."""
import re
from decimal import Decimal

QUERY = "При каких условиях проверять давление масла?"
SOURCE = "yamz-236-3902150-b-re-p019-oil-pressure"
INTEGER = r"(?:\d{1,3}(?: \d{3})+|\d+)"
NUMBER = rf"{INTEGER}(?:[.,]\d+)?"
PRESSURE_UNIT = r"(?:кпа|kpa|мпа|mpa|бар(?:а|ов)?|bars?|кгс\s*/\s*см\s*(?:²|\^?\s*2))"
SPEED_UNIT = r"(?:об\.?\s*/\s*мин|оборот(?:ов|ах)|rpm)"
BETWEEN = r"\s*(?:[-–—−/…]|\.\.\.|,\s+| (?:до|или|и) )\s*"
PRESSURE = re.compile(rf"(?<![\w.,])([+-−]?{NUMBER})\s*({PRESSURE_UNIT})(?!\w)", re.I)
PRESSURE_RANGE = re.compile(
    rf"(?<![\w.,]){NUMBER}(?:\s*{PRESSURE_UNIT})?{BETWEEN}{NUMBER}\s*{PRESSURE_UNIT}(?!\w)", re.I,
)
PRESSURE_ESTIMATE = re.compile(
    rf"(?:\b(?:около|примерно|порядка|ниже|выше|больше|меньше|до|от)\s+|[~≈≥≤<>]\s*)"
    rf"{NUMBER}\s*{PRESSURE_UNIT}(?!\w)", re.I,
)
BARE_NUMBER = re.compile(rf"({NUMBER})\s*[.!?]?")
SPEED = re.compile(rf"(?<![\w.,])({INTEGER})\s*{SPEED_UNIT}\b", re.I)
SPEED_RANGE = re.compile(
    rf"(?<![\w.,]){INTEGER}(?:\s*{SPEED_UNIT})?{BETWEEN}{INTEGER}\s*{SPEED_UNIT}\b", re.I,
)
CONFIRM_UNIT = re.compile(rf"(?:да(?:,?\s*({PRESSURE_UNIT}))?|({PRESSURE_UNIT}))[.!]?")
OBSERVATION = re.compile(r"\b(?:измерил\w*|замерил\w*|показал\w*|показывает|у меня)\b", re.I)


def kilopascals(amount, unit):
    value = Decimal(amount.replace(" ", "").replace(",", ".").replace("−", "-"))
    # Compare kgf/cm² using the rounded equivalents printed alongside kPa on page 19.
    factors = {"кпа": 1, "kpa": 1, "мпа": 1000, "mpa": 1000}
    return value * factors.get(unit.casefold(), 100)


def decimal_text(value):
    return format(value.normalize(), "f")


def reports_value(question):
    text = " ".join(question.split())
    if "?" in text and not (PRESSURE.match(text) or OBSERVATION.search(text)):
        return False
    return bool(PRESSURE.search(text) or BARE_NUMBER.fullmatch(text))


def readings(question, previous):
    values = dict(previous or {})
    text = " ".join(question.casefold().split())
    answer = text.strip(" .!?")
    if values.get("pressure") is not None and answer in {"да", "нет"}:
        if values.get("warm") is None:
            values["warm"] = answer == "да"
        elif values["warm"] and values.get("regime") and values.get("extended") is None:
            values["extended"] = answer == "да"

    pressure = {kilopascals(amount, unit) for amount, unit in PRESSURE.findall(text)}
    bare = BARE_NUMBER.fullmatch(text)
    if PRESSURE_RANGE.search(text) or PRESSURE_ESTIMATE.search(text) or len(pressure) > 1 or any(amount < 0 for amount in pressure):
        values.pop("pressure", None)
        values.pop("unconfirmed_pressure", None)
        values["ambiguous_pressure"] = True
    elif pressure:
        values.pop("ambiguous_pressure", None)
        values.pop("unconfirmed_pressure", None)
        values["pressure"] = decimal_text(next(iter(pressure)))
    elif bare:
        values.pop("ambiguous_pressure", None)
        values.pop("pressure", None)
        values["unconfirmed_pressure"] = decimal_text(kilopascals(bare[1], "кпа"))
    elif values.get("unconfirmed_pressure") and (confirmation := CONFIRM_UNIT.fullmatch(text)):
        unit = confirmation[1] or confirmation[2] or "кпа"
        values["pressure"] = decimal_text(kilopascals(values.pop("unconfirmed_pressure"), unit))

    if text.endswith("?") and not pressure:
        return values

    speed = {amount.replace(" ", "") for amount in SPEED.findall(text)}
    if speed or "холост" in text:
        if SPEED_RANGE.search(text) or len(speed) > 1:
            values["regime"] = None
        elif "холост" in text:
            minimal = re.search(r"минимальн\w+.*холост|холост\w+.*минимальн", text)
            values["regime"] = "idle" if minimal and "2100" not in speed else None
        else:
            values["regime"] = "rated" if speed == {"2100"} else None
    if re.search(r"холодн|не\s*прогрет|не грел", text):
        values["warm"] = False
    elif re.search(r"прогрет|на горячую|(?:двигатель|мотор) (?:был )?горяч", text):
        values["warm"] = True
    if "допуск неизвестен" in text:
        values["extended"] = None
    elif re.search(r"без допуска|допуск\s+не\s+применим|не\s+после длительной эксплуатации", text):
        values["extended"] = False
    elif re.search(r"после длительной эксплуатации|допуск\s+применим", text):
        values["extended"] = True
    return values


def assess(values):
    """Return the comparison, the next clarification, and the assessed range."""
    if values.get("ambiguous_pressure"):
        return None, "Укажите одно точное значение давления масла и его единицу.", None
    if unconfirmed := values.get("unconfirmed_pressure"):
        return None, f"{unconfirmed.replace('.', ',')} — это давление масла в кПа?", None
    if values.get("pressure") is None:
        return None, "Какое давление масла показал прибор? Укажите значение и единицу.", None
    if values.get("warm") is False:
        return None, "Этот замер сделан на холодном двигателе. Есть показание, снятое после прогрева?", None
    pressure = Decimal(values["pressure"])
    displayed = str(pressure).replace(".", ",")
    if not values.get("warm") and not values.get("regime"):
        return None, f"Записал {displayed} кПа. Двигатель был прогрет? На каких оборотах измеряли?", None
    if not values.get("warm"):
        return None, f"{displayed} кПа. Двигатель был прогрет, когда снимали показание?", None
    if not values.get("regime"):
        return None, f"При каких оборотах получили {displayed} кПа — на 2100 об/мин или на минимальном холостом ходу?", None
    if values.get("extended") is None:
        return None, "Для этого двигателя применяется допуск после длительной эксплуатации?", None

    # 236-3902150-Б РЭ, стр. 19. Для длительной эксплуатации указан только минимум.
    lower, upper = {
        ("rated", False): (400, 700),
        ("idle", False): (100, None),
        ("rated", True): (300, None),
        ("idle", True): (50, None),
    }[values["regime"], values["extended"]]
    limit = f"{lower}–{upper} кПа" if upper else f"не менее {lower} кПа"
    if pressure < lower:
        return f"{displayed} кПа — ниже норматива ({limit}).", None, "low"
    if upper and pressure > upper:
        return f"{displayed} кПа — выше норматива ({limit}).", None, "high"
    if values["extended"] and values["regime"] == "rated" and pressure > 700:
        return (f"{displayed} кПа — выше указанного рабочего диапазона 400–700 кПа. "
                "Допуск после длительной эксплуатации задаёт только нижнюю границу."), None, "high"
    return f"{displayed} кПа соответствует указанному нормативу для этого замера ({limit}).", None, "normal"


def compare(values):
    comparison, clarification, _ = assess(values)
    return comparison, clarification
