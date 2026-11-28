"""Advance diagnosis using the mechanic’s findings and applicable evidence."""

import re

from agent.case_context import FIRST_CHECK
from agent.tools.fluid_check import REPORT_PREFIX, wants_next_check

from manual_search.search import normalized_words, tokens

from .contracts import Reference, Reply, Section
from .sources import BY_SLUG, YAMZ236, ZMZ409, ZMZ5234


YAMZ536 = BY_SLUG["yamz536-re"]
NORMAL = re.compile(
    r"\b(?:в (?:норме|номре|порядке)|нормальн\w*|исправ\w*|без отклонений|"
    r"отклонений нет|замечаний нет|люфта нет|не люфтит|цел(?:а|ы|ое)?|ничего не обнаружил[аи]?)$"
)
SHORT = re.compile(
    r"(?:(?:да|проверил[аи]?)(?:[, .]+|$))*(?:(?:вс[её]|они|оба|он|она)(?: +|$))?"
)
UNCERTAIN = re.compile(r"\b(?:если|вроде|кажется|не уверен\w*|возможно|пока не|не в|не нормальн\w*|не исправ\w*)\b")
# Each entry matches the check already performed and names a different hypothesis.
STEPS = {
    ("ЯМЗ-536", "F04"): (
        (r"рем[е]?н", "износ натяжителя ремня",
         "Проверьте натяжитель ремня на посторонний шум и люфт по регламенту ТО‑2.", YAMZ536.reference(162)),
    ),
    ("ЗМЗ-40905.10", "F01"): (
        (r"гидротолкател", "недостаточная подача масла к гидротолкателям",
         "При сохраняющемся стуке проверьте подачу масла к гидротолкателям.", ZMZ409.reference(143)),
        (r"подач[ауи] масла", "износ кулачков распределительных валов",
         "Осмотрите кулачки распределительных валов на царапины, трещины и следы износа.", ZMZ409.reference(143)),
    ),
    ("ЗМЗ-5234.10", "F01"): (
        (r"зазор", "осадка клапанной пружины",
         "При сохраняющемся стуке проверьте состояние клапанных пружин при очной дефектовке.", ZMZ5234.reference(56)),
    ),
    ("ЯМЗ-536", "F09"): (
        (r"рукав|воздухопровод|впуск", "задевание колеса турбокомпрессора за корпус",
         "Если сохраняется шум со стороны турбокомпрессора, при очной дефектовке проверьте осевой и радиальный люфт его вала.",
         YAMZ536.reference(216)),
    ),
    ("ЯМЗ-536", "F10"): (
        (r"воздухопровод|засорен|компрессор пневмотормозов", "негерметичность пневмосистемы",
         "Если производительность компрессора пневмотормозов остаётся низкой, проверьте герметичность элементов пневмосистемы на стенде.",
         YAMZ536.reference(234)),
    ),
    ("ЗМЗ-40905.10", "F03"): (
        (r"уров|охлаждающ|бач", "потеря герметичности системы охлаждения",
         "Если уровень охлаждающей жидкости снова падает или перегрев сохраняется, проверьте систему охлаждения на утечки.",
         ZMZ409.reference(97)),
    ),
    ("ЗМЗ-5234.10", "F03"): (
        (r"охлаждающ|уров|теч|подт[её]к", "неисправность термостата",
         "Если перегрев сохраняется, проверьте работу термостата по руководству.",
         ZMZ5234.reference(54)),
    ),
    ("ЗМЗ-5234.10", "F06"): (
        (r"износ|наклад|пробуксов", "попадание масла на фрикционные накладки",
         "При подтверждённой пробуксовке проверьте, есть ли следы масла на фрикционных накладках.",
         ZMZ5234.reference(79)),
    ),
    ("ЗМЗ-5234.10", "F12"): (
        (r"свеч|зазор|зажиган", "ослабление искры из-за неисправности катушки зажигания",
         "Если причина перебоев сохраняется после проверки свечей, проверьте катушку зажигания при очной диагностике.",
         ZMZ5234.reference(54)),
    ),
    ("ЗМЗ-5234.10", "F13"): (
        (r"герметич|теч|подт[её]к", "неполное открытие воздушной заслонки",
         "Если утечка исключена и расход бензина остаётся повышенным, проверьте, не заедает ли привод воздушной заслонки.",
         ZMZ5234.reference(55)),
    ),
    ("ЯМЗ-236М2", "F03"): (
        (r"подте|подтё|уровень масла|эмульси", "нарушение циркуляции охлаждающей жидкости",
         "При сохраняющемся перегреве проверьте состояние ремня водяного насоса, радиатора и термостата.",
         YAMZ236.reference(54)),
    ),
}


ABNORMAL = re.compile(
    r"\b(?:неисправ\w*|не в норме|не работает|заеда\w*|заклини\w*|ослаб\w*|изнош\w*|"
    r"порван\w*|оборван\w*|теч[её]т|подтека\w*|люфтит|стучит|свистит|не держит|"
    r"есть (?:отклонени\w*|люфт|течь|подт[её]к\w*|трещин\w*|пробуксовка)|"
    r"(?:обнаружен\w*|обнаружил[аи]?|наш[её]л|нашла) (?:износ|люфт|течь|трещин\w*)|"
    r"ниже нормы|выше нормы)\b"
)
CONDITIONAL = re.compile(r"\b(?:если|вроде|кажется|не уверен\w*|возможно|пока не)\b")
NEGATED_BEFORE = re.compile(r"\b(?:не(?:\s+(?:было|обнаружено|выявлено|замечено))?|нет|без|отсутству(?:ет|ют))\s*$")
NEGATED_AFTER = re.compile(r"^\s*(?:нет\b|отсутству(?:ет|ют)\b|не\s+(?:наблюда\w*|обнаружен\w*|подтвержд[её]н\w*))")
ABNORMAL_SHORT = re.compile(r"(?:да[, ]+)?(?:(?:он|она|они) )?(?:есть отклонение|неисправен|неисправна|неисправны|не в норме)")
DIAGNOSTIC_LINE = re.compile(r"^(?:\d+[.)]\s*)?(?:проверить|измерить|осмотреть|сверить)\s", re.I)
REPAIR_LINE = re.compile(r"\b(?:заменить|отрегулировать|запустить|отсоединить|снять|разобрать|затянуть|промыть)\b", re.I)
NO_ALTERNATIVE = "Результат проверки учтён. Для следующей гипотезы нужны условия появления симптома и фактические показания."
ABNORMAL_FOLLOWUP = "Отклонение учтено. Уточните, что именно обнаружили и при каких условиях, чтобы проверить причину."


def report_text(question):
    text = REPORT_PREFIX.sub("", " ".join(question.casefold().split()))
    if (combined := re.fullmatch(r"(.+)[,.!;]\s+([^,.!;]+)", text)) and wants_next_check(combined[2]):
        text = combined[1]
    return text.strip(" .!")


def same_subject(left, right):
    return bool(set(tokens(normalized_words(left))) & set(tokens(normalized_words(right))) -
                {"проверит", "проверьте", "проверить", "состоян", "неисправност", "есть", "при"})


def documented_alternative(index, engine, code, used, *, finding=""):
    if not index:
        return None
    terms = index.fault_terms.get(code, ())
    for phrase in terms:
        for hit in index.search(phrase, engine, code, limit=7):
            if not hit.get("verified"):
                continue
            work = hit.get("work")
            operation = work.get("operation") if isinstance(work, dict) else None
            candidates = [operation] if operation else hit["text"].splitlines()
            for line in candidates:
                check = line.strip()
                if (not DIAGNOSTIC_LINE.match(check) or REPAIR_LINE.search(check)
                        or not same_subject(phrase, check)
                        or any(overlaps_check(check, old) for old in used)
                        or (finding and overlaps_check(check, finding))):
                    continue
                ref = Reference.model_validate(index.resolve_source(hit["fragment_id"], engine))
                return Reply(summary="Следующая проверка по руководству.",
                             sections=[Section(kind="checks", items=[check], references=[ref])],
                             analysis={"guided_followup": True})
    return None


def overlaps_check(left, right):
    left_words = set(tokens(normalized_words(left))) - {"проверить", "проверьте", "проверит", "измерит", "осмотрет"}
    right_words = set(tokens(normalized_words(right)))
    overlap = left_words & right_words
    return bool(left_words and (left_words <= right_words or len(overlap) / len(left_words) >= .45))


def normal_report(question: str) -> str | None:
    text = report_text(question)
    match = NORMAL.search(text)
    if not match or "?" in text or UNCERTAIN.search(text):
        return None
    return SHORT.sub("", text[:match.start()].strip(" ,.—–-"), count=1).strip(" ,.—–-")


CLUTCH_DRY = re.compile(
    r"(?:\bмасла\b[^.!?]*\bнет\b|\bнет масла\b|"
    r"\bмасло\b[^.!?]*\bне обнаружен\w*|\bнакладки\s+сухие\b|\bбез следов масла\b)"
)
CLUTCH_DRY_SHORT = re.compile(r"(?:да|проверил[аи]?)?[, ]*(?:масла нет|нет масла|без следов масла)")
CLUTCH_SURFACE = re.compile(r"наклад|ведом\w* диск")
CLUTCH_OIL = re.compile(r"масл.*наклад|наклад.*масл")
CLUTCH_SUMMARY = "На накладках масла нет. Возможная причина дребезга — износ или повреждение деталей демпфера ведомого диска."
CLUTCH_CHECK = "Если дребезг сохраняется, при очной дефектовке проверьте детали демпфера ведомого диска на износ и поломку."
CLUTCH_NEXT = "При сохраняющемся дребезге проверьте фрикционную шайбу и нажимную пружину гасителя ведомого диска."


def clutch_followup(engine, code, state, question, previous):
    if engine != "ЗМЗ-40905.10" or code != "F06":
        return None

    def dry_report(text):
        text = REPORT_PREFIX.sub("", " ".join(text.casefold().split())).strip(" .!")
        if (parts := re.split(r"[,.!]\s+", text)) and wants_next_check(parts[-1]):
            text = " ".join(parts[:-1])
        return ("?" not in text and not UNCERTAIN.search(text) and any(
            CLUTCH_DRY_SHORT.fullmatch(clause.strip()) or (
                CLUTCH_SURFACE.search(clause) and CLUTCH_DRY.search(clause)
            )
            for clause in re.split(r"[.!;]+", text)
        ))

    last_step = next((item["analysis"]["clutch_followup"] for item in reversed(state["messages"])
                      if (item.get("analysis") or {}).get("clutch_followup")), None)
    first = (state.get("brief") or {}).get("first_check", "").casefold()
    asked_next = wants_next_check(question) or FIRST_CHECK.fullmatch(question.strip())
    subject = normal_report(question)
    if last_step and (asked_next or dry_report(question)):
        step = last_step
    elif (CLUTCH_OIL.search(previous or first) and
          (dry_report(question) or (subject is not None and not subject))):
        step = "damper"
    elif CLUTCH_OIL.search(previous) and CLUTCH_SURFACE.search(report_text(question)) and re.search(
        r"\b(?:масло есть|есть масло|обнаружил[аи]? масло|масло обнаружено|замаслен\w*|масл[оа]\b[^.!?]*\bесть)\b",
        report_text(question),
    ) and not CONDITIONAL.search(question.casefold()) and "?" not in report_text(question):
        step = "contamination"
    elif last_step == "damper" and previous == CLUTCH_CHECK.casefold() and subject is not None and (
        not subject or re.search(r"демпфер", subject)
    ):
        step = "friction"
    elif last_step == "friction" and previous == CLUTCH_NEXT.casefold() and subject is not None and (
        not subject or re.search(r"шайб|пружин|гасител", subject)
    ):
        step = "inspect"
    elif asked_next and CLUTCH_OIL.search(first) and any(
        item["role"] == "user" and dry_report(item["text"]) for item in state["messages"]
    ):
        step = "damper"
    else:
        return None

    if step == "inspect":
        return Reply(summary="При сохраняющемся дребезге локализуйте его источник при очной дефектовке. "
                             "Укажите, как меняется шум при нажатии на педаль и на разных оборотах.",
                     analysis={"clutch_followup": step})
    summary, check = {
        "damper": (CLUTCH_SUMMARY, CLUTCH_CHECK),
        "contamination": ("Масло на накладках может вызывать пробуксовку.",
                          "При очной дефектовке определите, откуда масло попадает в картер сцепления."),
        "friction": ("Следующая гипотеза — износ фрикционной шайбы или ослабление нажимной пружины гасителя.",
                     CLUTCH_NEXT),
    }[step]
    return Reply(summary=summary,
                 sections=[Section(kind="checks", items=[check], references=[ZMZ409.reference(195)])],
                 analysis={"clutch_followup": step})


def next_hypothesis(vehicle, diagnosis, state, question, index=None) -> Reply | None:
    subject = normal_report(question)
    engine, code = vehicle["engine_model"], diagnosis.anomaly_type
    steps = STEPS.get((engine, code), ())

    turns = [
        [text for section in item.get("sections", []) if section["kind"] == "checks" for text in section["items"]]
        for item in state["messages"] if item["role"] == "assistant"
    ]
    latest = next((checks for checks in reversed(turns) if checks), None)
    previous = (latest[0] if latest else (state.get("brief") or {}).get("first_check", "")).casefold()
    if reply := clutch_followup(engine, code, state, question, previous):
        return reply
    finding = report_text(question)
    observations = list(ABNORMAL.finditer(finding))
    report_matches = "?" not in finding and not CONDITIONAL.search(finding) and observations and (
        ABNORMAL_SHORT.fullmatch(finding) or REPORT_PREFIX.match(question.casefold()) or same_subject(finding, previous)
        or same_subject(finding, diagnosis.description)
        or any(re.search(pattern, finding) and re.search(pattern, previous) for pattern, *_ in steps)
    )
    abnormal = report_matches and any(
        not NEGATED_BEFORE.search(finding[:match.start()]) and not NEGATED_AFTER.match(finding[match.end():])
        for match in observations
    )
    if report_matches and not abnormal and subject is None:
        return Reply(summary="Указанный признак не подтвердился. Сообщите результат назначенной проверки "
                             "или уточните, какие симптомы сохраняются.")
    if subject is None and not abnormal:
        if FIRST_CHECK.fullmatch(question.strip()) or wants_next_check(question):
            last_reply = next((item for item in reversed(state["messages"]) if item["role"] == "assistant" and (
                (item.get("analysis") or {}).get("guided_followup")
                or any(section["kind"] == "checks" for section in item.get("sections", []))
            )), None)
            if last_reply and (last_reply.get("analysis") or {}).get("guided_followup"):
                return Reply.model_validate(last_reply)
        return None
    if not previous and not (abnormal and same_subject(finding, diagnosis.description)):
        return None

    if engine in ZMZ409.engines:
        if code == "F12":
            steps = ((r"свеч|зазор|катушк", "потеря сигнала датчика синхронизации",
                      "При сохраняющихся перебоях проверьте надёжность разъёма и исправность датчика синхронизации.",
                      ZMZ409.reference(102)),)
        elif code == "F14":
            steps = (
                (r"цеп", "неисправность гидронатяжителя цепи",
                 "Проверьте гидронатяжитель привода ГРМ.", ZMZ409.reference(107)),
                (r"гидронатяжител", "повышенное удлинение цепи",
                 "При сохраняющемся шуме проверьте удлинение цепи ГРМ по руководству.", ZMZ409.reference(107)),
            )

    # A later specific check (e.g. oil supply) can contain the earlier check's component name.
    suggested = {text for checks in turns for text in checks}
    for pattern, hypothesis, check, reference in reversed(steps):
        if (subject and not re.search(pattern, subject)) or not re.search(pattern, previous):
            continue
        if check in suggested or check.casefold() == previous or (abnormal and overlaps_check(check, finding)):
            continue
        return Reply(
            summary=(f"Отклонение учтено. Возможная причина — {hypothesis}." if abnormal
                     else f"Следующая гипотеза — {hypothesis}."),
            sections=[Section(kind="checks", items=[check], references=[reference])],
            analysis={"guided_followup": True},
        )
    if subject and not same_subject(subject, previous) and not REPORT_PREFIX.match(question.casefold()):
        return None
    if reply := documented_alternative(index, engine, code, {*suggested, previous}, finding=finding if abnormal else ""):
        if abnormal:
            reply.summary = "Отклонение учтено. " + reply.summary
        return reply
    return Reply(summary=ABNORMAL_FOLLOWUP if abnormal else NO_ALTERNATIVE, analysis={"guided_followup": True})
