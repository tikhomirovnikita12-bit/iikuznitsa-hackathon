"""Compare operator readings with the verified YaMZ-236M2 operating manual."""
import re
from decimal import Decimal

QUERY = "При каких условиях проверять давление масла?"
SOURCE = "yamz-236-3902150-b-re-p019-oil-pressure"
PRESSURE = re.compile(r"(?<![\d.,-])(\d+(?:[.,]\d+)?)\s*(?:кпа|kpa)\b", re.I)
SPEED = re.compile(r"(?<![\d.,-])(\d+)\s*(?:об\s*/\s*мин|оборотов|rpm)\b", re.I)


def readings(question, previous):
    values = dict(previous)
    text = " ".join(question.casefold().split())
    pressure = set(PRESSURE.findall(text))
    speed = set(SPEED.findall(text))
    if pressure:
        values["pressure"] = next(iter(pressure)).replace(",", ".") if len(pressure) == 1 else None
    if speed or "холост" in text:
        if "холост" in text:
            values["regime"] = "idle" if not speed and re.search(r"минимальн\w+.*холост|холост\w+.*минимальн", text) else None
        else:
            values["regime"] = "rated" if speed == {"2100"} else None
    if re.search(r"холодн|не\s*прогрет", text):
        values["warm"] = False
    elif "прогрет" in text:
        values["warm"] = True
    if re.search(r"без допуска|допуск\s+не\s+применим|не\s+после длительной эксплуатации", text):
        values["extended"] = False
    elif re.search(r"после длительной эксплуатации|допуск\s+применим", text):
        values["extended"] = True
    return values


def compare(values):
    missing = []
    if values.get("pressure") is None:
        missing.append("одно показание давления масла в кПа")
    if not values.get("warm"):
        missing.append("замер на прогретом двигателе")
    if not values.get("regime"):
        missing.append("режим замера — 2100 об/мин или минимальные обороты холостого хода")
    if values.get("extended") is None:
        missing.append("применимость допуска после длительной эксплуатации")
    if missing:
        return None, "Для сравнения уточните: " + ", ".join(missing) + "."

    # 236-3902150-Б РЭ, стр. 19. Для длительной эксплуатации указан только минимум.
    lower, upper = {
        ("rated", False): (400, 700),
        ("idle", False): (100, None),
        ("rated", True): (300, None),
        ("idle", True): (50, None),
    }[values["regime"], values["extended"]]
    pressure = Decimal(values["pressure"])
    displayed = str(pressure).replace(".", ",")
    limit = f"{lower}–{upper} кПа" if upper else f"не менее {lower} кПа"
    if pressure < lower:
        return f"{displayed} кПа — ниже норматива ({limit}).", None
    if upper and pressure > upper:
        return f"{displayed} кПа — выше норматива ({limit}).", None
    if values["extended"] and values["regime"] == "rated" and pressure > 700:
        return f"{displayed} кПа — выше указанного рабочего диапазона 400–700 кПа. Допуск после длительной эксплуатации задаёт только нижнюю границу.", None
    return f"{displayed} кПа соответствует указанному нормативу для этого замера ({limit}).", None
