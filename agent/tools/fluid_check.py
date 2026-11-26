"""Track the preliminary fluid checks specified for low oil pressure."""
import re

SOURCE = "yamz-236-3902150-b-re-p053-low-oil-pressure"
QUERY = "Какие причины пониженного давления указаны в руководстве?"
OIL = re.compile(r"\bмасл[аоуе]\b")
COOLANT = re.compile(r"охлаждающ|антифриз|тосол|\bож\b")
LOW = re.compile(r"недостаточно|не хватает|мало|ниже (?:нормы|минимума)")
OK = re.compile(r"\bдостаточно\b|в норме|нормальн|\bхватает\b")


def report(text, previous):
    """Return new state for a report. An isolated negative answer needs a subject."""
    state = dict(previous)
    if re.fullmatch(r"вс[её] (?:достаточно|в норме)|(?:оба|оба уровня) в норме", text):
        return {"oil": "ok", "coolant": "ok"}

    reported = False
    for clause in re.split(r"[,;.!]+|\s+а\s+|\s+но\s+", text):
        subjects = [name for name, pattern in (("oil", OIL), ("coolant", COOLANT)) if pattern.search(clause)]
        level = "low" if LOW.search(clause) else "ok" if OK.search(clause) else None
        if level and subjects:
            for name in subjects:
                state[name] = level
            reported = True

    if reported:
        state.pop("unspecified_low", None)
        return state
    if LOW.fullmatch(text):
        return dict(state, unspecified_low=True)
    return None


def next_action(state):
    if state.get("unspecified_low"):
        return None, "Чего недостаточно — моторного масла, охлаждающей жидкости или обеих жидкостей?"
    if state.get("oil") == "low" or state.get("coolant") == "low":
        return ("Отмечаю, что уровень ниже нормы. Сначала нужно разобраться с низким уровнем, прежде чем искать причину пониженного давления.", None)
    if state.get("oil") == "ok" and state.get("coolant") == "ok":
        return ("Оба уровня в норме. Проверьте исправность манометра. Руководство указывает, что неисправный манометр нужно заменить исправным.", None)
    if state.get("oil") == "ok":
        return None, "Уровень масла в норме. А охлаждающей жидкости достаточно?"
    if state.get("coolant") == "ok":
        return None, "Уровень охлаждающей жидкости в норме. А масла в картере достаточно?"
    return None, "Уровни масла и охлаждающей жидкости уже проверили?"
