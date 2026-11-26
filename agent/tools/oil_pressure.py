"""Compare operator readings with the verified YaMZ-236M2 operating manual."""
import re
from decimal import Decimal

QUERY = "При каких условиях проверять давление масла?"
SOURCE = "yamz-236-3902150-b-re-p019-oil-pressure"
PRESSURE = re.compile(r"(?<![\d.,-])(\d+(?:[.,]\d+)?)\s*(?:кпа|kpa)\b", re.I)
BARE_NUMBER = re.compile(r"(\d+(?:[.,]\d+)?)\s*[.!?]?")
SPEED = re.compile(r"(?<![\d.,-])(\d+)\s*(?:об\s*/\s*мин|оборотов|rpm)\b", re.I)


def readings(question, previous):
    values = dict(previous or {})
    text = " ".join(question.casefold().split())
    answer = text.strip(" .!?")
    if values.get("pressure") is not None and answer in {"да", "нет"}:
        if values.get("warm") is not True:
            values["warm"] = answer == "да"
        elif values.get("regime") and values.get("extended") is None:
            values["extended"] = answer == "да"
    pressure = set(PRESSURE.findall(text))
    speed = set(SPEED.findall(text))
    bare = BARE_NUMBER.fullmatch(text)
    if previous is not None and bare:
        values["unconfirmed_pressure"] = bare[1].replace(",", ".")
        values.pop("pressure", None)
    elif values.get("unconfirmed_pressure") and re.fullmatch(r"(?:да(?:,? кпа)?|кпа|kpa)[.!]?", text):
        values["pressure"] = values.pop("unconfirmed_pressure")
    if pressure:
        values.pop("unconfirmed_pressure", None)
        values["pressure"] = next(iter(pressure)).replace(",", ".") if len(pressure) == 1 else None
    if speed or "холост" in text:
        if "холост" in text:
            values["regime"] = "idle" if not speed and re.search(r"минимальн\w+.*холост|холост\w+.*минимальн", text) else None
        else:
            values["regime"] = "rated" if speed == {"2100"} else None
    if re.search(r"холодн|не\s*прогрет|не грел", text):
        values["warm"] = False
    elif re.search(r"прогрет|на горячую|(?:двигатель|мотор) (?:был )?горяч", text):
        values["warm"] = True
    if re.search(r"без допуска|допуск\s+не\s+применим|не\s+после длительной эксплуатации", text):
        values["extended"] = False
    elif re.search(r"после длительной эксплуатации|допуск\s+применим", text):
        values["extended"] = True
    return values


def compare(values):
    if unconfirmed := values.get("unconfirmed_pressure"):
        return None, f"{unconfirmed.replace('.', ',')} — это давление масла в кПа?"
    if values.get("pressure") is None:
        return None, "Какое давление масла показал прибор? Укажите значение и единицу."
    if values.get("warm") is False:
        return None, "Этот замер сделан на холодном двигателе. Есть показание, снятое после прогрева?"
    pressure = Decimal(values["pressure"])
    displayed = str(pressure).replace(".", ",")
    if not values.get("warm") and not values.get("regime"):
        return None, f"Записал {displayed} кПа. Двигатель был прогрет? На каких оборотах измеряли?"
    if not values.get("warm"):
        return None, f"{displayed} кПа. Двигатель был прогрет, когда снимали показание?"
    if not values.get("regime"):
        return None, f"При каких оборотах получили {displayed} кПа — на 2100 об/мин или на минимальном холостом ходу?"
    if values.get("extended") is None:
        return None, "Для этого двигателя применяется допуск после длительной эксплуатации?"

    # 236-3902150-Б РЭ, стр. 19. Для длительной эксплуатации указан только минимум.
    lower, upper = {
        ("rated", False): (400, 700),
        ("idle", False): (100, None),
        ("rated", True): (300, None),
        ("idle", True): (50, None),
    }[values["regime"], values["extended"]]
    limit = f"{lower}–{upper} кПа" if upper else f"не менее {lower} кПа"
    if pressure < lower:
        return f"{displayed} кПа — ниже норматива ({limit}).", None
    if upper and pressure > upper:
        return f"{displayed} кПа — выше норматива ({limit}).", None
    if values["extended"] and values["regime"] == "rated" and pressure > 700:
        return f"{displayed} кПа — выше указанного рабочего диапазона 400–700 кПа. Допуск после длительной эксплуатации задаёт только нижнюю границу.", None
    return f"{displayed} кПа соответствует указанному нормативу для этого замера ({limit}).", None
