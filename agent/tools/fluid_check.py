"""Track the preliminary fluid checks specified for low oil pressure."""
import re

SOURCE = "yamz-236-3902150-b-re-p053-low-oil-pressure"
QUERY = "Какие причины пониженного давления указаны в руководстве?"
OIL = re.compile(r"\bмасл[аоуе]\b")
COOLANT = re.compile(r"охлаждающ|антифриз|тосол|\bож\b")
LOW = re.compile(r"\b(?:не\s*достаточно|не хватает|(?<!не )мало|(?<!не )ниже (?:нормы|минимума))\b")
LEVEL = re.compile(
    rf"(?P<low>{LOW.pattern})|(?P<unknown>\bне (?:в норме|нормальн\w*))"
    r"|(?P<ok>\b(?:достаточно|в норме|нормальн\w*|норм|хватает)\b)",
)


def report(text, previous):
    """Return new state for a report. An isolated negative answer needs a subject."""
    text = " ".join(text.casefold().split()).strip(" .!")
    if "?" in text or re.search(r"\b(?:сколько|когда|почему|как|ли)\b", text):
        return None
    state = dict(previous or {})
    if previous is not None and re.fullmatch(r"вс[её] (?:достаточно|в норме)|(?:оба|оба уровня) в норме", text):
        return {"oil": "ok", "coolant": "ok"}

    reported = False
    for clause in re.split(r"[,;.!]+|\s+а\s+|\s+но\s+", text):
        # Keep a shared predicate ("масла и антифриза достаточно") together.
        levels = {match.lastgroup for match in LEVEL.finditer(clause)}
        parts = re.split(r"\s+и\s+", clause) if len(levels) > 1 else [clause]
        for part in parts:
            levels = {match.lastgroup for match in LEVEL.finditer(part)}
            if len(levels) != 1:
                continue
            level = next(iter(levels))
            subjects = []
            if OIL.search(part) and ("уров" in part or "давлен" not in part):
                subjects.append("oil")
            if COOLANT.search(part):
                subjects.append("coolant")
            for name in subjects:
                if level == "unknown":
                    state.pop(name, None)
                else:
                    state[name] = level
            reported |= bool(subjects)

    if reported:
        state.pop("unspecified_low", None)
        return state
    if previous is not None and LOW.fullmatch(text):
        return dict(state, unspecified_low=True)
    return None


def next_action(state):
    if state.get("unspecified_low"):
        return None, "Чего недостаточно — моторного масла, охлаждающей жидкости или обеих жидкостей?"
    low = [label for name, label in (("oil", "масла"), ("coolant", "охлаждающей жидкости"))
           if state.get(name) == "low"]
    if low:
        return (f"Уровень {' и '.join(low)} ниже нормы. Сначала выясните причину низкого уровня, "
                "затем продолжайте проверку давления.", None)
    if state.get("oil") == "ok" and state.get("coolant") == "ok":
        return ("Оба уровня в норме. Проверьте исправность манометра. Руководство указывает, что неисправный манометр нужно заменить исправным.", None)
    if state.get("oil") == "ok":
        return None, "Уровень масла в норме. А охлаждающей жидкости достаточно?"
    if state.get("coolant") == "ok":
        return None, "Уровень охлаждающей жидкости в норме. А масла в картере достаточно?"
    return None, "Уровни масла и охлаждающей жидкости уже проверили?"
