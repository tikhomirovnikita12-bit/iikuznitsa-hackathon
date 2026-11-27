"""Track verified follow-up steps for low oil pressure on the YaMZ-236M2."""
import re

from . import oil_pressure

SOURCE = "yamz-236-3902150-b-re-p053-low-oil-pressure"
QUERY = "Какие причины пониженного давления указаны в руководстве?"
OIL = re.compile(r"\bмасл[аоуе]\b")
COOLANT = re.compile(r"охлаждающ|\bохл\s+жидк|антифриз|тосол|\bож\b")
LOW = re.compile(r"\b(?:не\s*достаточно|не хватает|(?<!не )мало|(?<!не )ниже (?:нормы|минимума))\b")
LEVEL = re.compile(
    rf"(?P<low>{LOW.pattern})|(?P<unknown>\bне (?:в норме|нормальн\w*))"
    r"|(?P<ok>\b(?:достаточно|в норме|нормальн\w*|норм|хватает)\b)",
)
REPORT_PREFIX = re.compile(r"^результат проверки(?: по руководству \([^)]*\))?\s*:\s*")
REPORT_AND_QUESTION = re.compile(r"(.+)[,;.!]\s+([^,;.!?]+)\?")
GAUGE_CHECK = "Проверьте исправность манометра."
GAUGE_RESULT = (
    r"(?P<bad>неисправен|не исправен|не в норме|не работает|сломан|вр[её]т)"
    r"|(?P<ok>исправен|в норме|работает(?: нормально)?)"
)
GAUGE_REPORT = re.compile(
    r"\bманометр(?: давления масла)?(?:\s*[,\-–—:]\s*(?:он\s+)?|\s+)"
    r"(?:сейчас |теперь )?(?:" + GAUGE_RESULT + r")\b"
)
GAUGE_SHORT = re.compile(r"(?:проверил[аи]?[, ]+)?(?:да[, ]+)?(?:он )?(?:" + GAUGE_RESULT + ")")
NEXT_QUESTION = re.compile(
    r"(?:(?:ну|а|и|так|теперь)[, ]+)*(?:(?:подскажи|скажи)[, ]+)?(?:"
    r"что(?: из (?:этого|них))? (?:(?:мне|нужно|надо|теперь|дальше|сначала|первым(?: делом)?|в первую очередь) )*"
    r"(?:провер(?:ить|ять)|делать|сделать|посмотреть)"
    r"(?: (?:первым(?: делом)?|сначала|в первую очередь|теперь|дальше))?|"
    r"с чего (?:начать|продолжить|начн[её]м)|с какой гипотезы начать|что дальше|"
    r"(?:какую|какие) гипотез(?:у|ы)(?: (?:сначала|первой|в первую очередь))?"
    r"(?: (?:нужно |лучше |надо )?(?:проверить|проверять|отработать))?"
    r"(?: (?:первой|первым делом|дальше|сначала|в первую очередь))?)"
)


def wants_next_check(question):
    return bool(NEXT_QUESTION.fullmatch(" ".join(question.casefold().split()).strip(" .!?")))


def restore(turns):
    """Recover reported facts even from a turn that predates the guided reply."""
    state = None
    gauge_expected = False
    for question, answer, saved in turns:
        if saved is not None:
            state = saved
        elif (observed := report(question, state, gauge_expected=gauge_expected)) is not None:
            state = observed
        gauge_expected = (bool(state) and state.get("gauge") is None and GAUGE_CHECK in answer)
    return state, gauge_expected


def report(text, previous, *, gauge_expected=False):
    """Return new state for a report. An isolated negative answer needs a subject."""
    text = " ".join(text.casefold().split()).strip(" .!")
    text = REPORT_PREFIX.sub("", text)
    text = re.sub(r"\bохл\.\s*(?=жидк)", "охлаждающая ", text)
    if (combined := REPORT_AND_QUESTION.fullmatch(text)) and wants_next_check(combined[2]):
        text = combined[1]
    if "?" in text or re.search(r"\b(?:сколько|когда|почему|как|ли)\b", text):
        return None
    state = dict(previous or {})
    if gauge_expected and re.fullmatch(r"вс[её] в (?:норме|порядке)", text):
        return dict(state, gauge="ok")
    if previous is not None and re.fullmatch(r"вс[её] (?:достаточно|в норме|в порядке)|(?:оба|оба уровня) в норме", text):
        state.pop("unspecified_low", None)
        return dict(state, oil="ok", coolant="ok")

    gauge = None
    if not re.search(r"\b(?:если|должен|возможно|похоже|кажется|вероятно|не уверен)\b", text):
        gauge = GAUGE_REPORT.search(text) or (GAUGE_SHORT.fullmatch(text) if gauge_expected else None)
    if gauge:
        state["gauge"] = "faulty" if gauge.lastgroup == "bad" else "ok"

    reported = False
    for clause in re.split(r"[,;.!]+|\s+а\s+|\s+но\s+", text):
        # Keep a shared predicate ("масла и антифриза достаточно") together.
        levels = {match.lastgroup for match in LEVEL.finditer(clause)}
        parts = re.split(r"\s+и\s+", clause) if len(levels) > 1 else [clause]
        for part in parts:
            levels = {match.lastgroup for match in LEVEL.finditer(part)}
            if re.fullmatch(r"(?:масла|антифриза|охлаждающей жидкости)(?: в картере)? нет|нет (?:масла|антифриза|охлаждающей жидкости)", part.strip()):
                levels = {"low"}
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
    if gauge:
        return state
    if previous is not None and LOW.fullmatch(text):
        return dict(state, unspecified_low=True)
    return None


def next_action(state, pressure):
    """Return source-linked checks and any information still needed from the mechanic."""
    if state.get("unspecified_low"):
        return [], "Чего недостаточно — моторного масла, охлаждающей жидкости или обеих жидкостей?"
    low = [label for name, label in (("oil", "масла"), ("coolant", "охлаждающей жидкости"))
           if state.get(name) == "low"]
    if low:
        return [(SOURCE, f"Уровень {' и '.join(low)} ниже нормы. Сначала выясните причину низкого уровня, "
                        "затем продолжайте проверку давления.")], None
    if state.get("oil") == "ok" and state.get("coolant") == "ok":
        if state.get("gauge") == "ok":
            return pressure_step(pressure)
        if state.get("gauge") == "faulty":
            return [(SOURCE, "Манометр неисправен по результату проверки. Руководство предписывает заменить его "
                             "исправным. Для оценки давления понадобится новое показание исправного прибора.")], None
        return [(SOURCE, f"Оба уровня в норме. {GAUGE_CHECK}")], None
    if state.get("oil") == "ok":
        return [], "Уровень масла в норме. А охлаждающей жидкости достаточно?"
    if state.get("coolant") == "ok":
        return [], "Уровень охлаждающей жидкости в норме. А масла в картере достаточно?"
    return [(SOURCE, "Начните с проверки количества масла в картере и уровня охлаждающей жидкости.")], None


def pressure_step(values):
    comparison, clarification, level = oil_pressure.assess(values)
    if comparison is None:
        return [(oil_pressure.SOURCE, "Уровни в норме, манометр исправен. Сначала проверьте, подтверждается ли "
                                     "снижение давления масла замером в условиях руководства.")], clarification
    checks = [(oil_pressure.SOURCE, comparison)]
    if level == "low":
        checks.append((SOURCE, "Следующая гипотеза для проверки — загрязнение фильтра грубой очистки масла. "
                               "Руководство указывает загрязнение фильтрующего элемента среди причин пониженного давления."))
    elif level == "normal":
        checks.append((oil_pressure.SOURCE, "Если стук или сигнал сохраняется, зафиксируйте режим замера и передайте "
                                           "машину на очную проверку."))
    else:
        checks.append((oil_pressure.SOURCE, "Сверьте условия получения высокого показания и передайте результат "
                                           "на очную проверку."))
    return checks, None
