"""Проверенные тестовые ответы для интеграции интерфейса до подключения агента команды."""

import re

from agent.tools import fluid_check
from backend.mechanic import conflict_case
from manual_search.corpus import verified_fragments

from .contracts import Brief, Diagnosis, DraftFields, Reference, Reply, Section
from .sources import YAMZ236, ZMZ409, ZMZ5234


OIL_LEVELS = YAMZ236.reference(53)
OIL_PRESSURE = YAMZ236.reference(19)
COOLANT_PAGE = YAMZ236.reference(23)
COOLANT_TABLE = YAMZ236.reference(54)
COOLANT_TEMP = YAMZ236.reference(4)
COOLANT_FRAGMENT = "yamz-236-3902150-b-re-p054-f03-coolant-in-oil"
COOLANT_FIRST_FRAGMENT = "yamz-236-3902150-b-re-p023-f03-first-check"
COOLANT_TEMP_FRAGMENT = "yamz-236-3902150-b-re-p004-coolant-temperature"
YAMZ536_OIL_PRESSURE = [
    fragment for fragment in verified_fragments()
    if fragment["fragment_id"] in {
        "yamz-536-01-02-rk-p013-oil-pressure",
        "yamz-536-3902150-re-p049-idle-oil-pressure",
    }
]
CHAIN = ZMZ409.reference(107)
VALVE_TEMP = ZMZ5234.reference(20)
VALVE_CLEARANCE = ZMZ5234.reference(21)
VALVE_NOISE = ZMZ5234.reference(56)

OIL_CAUSES = (
    ("Неисправный манометр", "Поставить исправный манометр."),
    ("Повышенная температура масла", "Таблица связывает её с неисправностью системы охлаждения масла."),
    ("Разжижение масла топливом", "Устранить утечки в сливной магистрали под крышками головок, "
     "в соединениях форсунок и топливопроводов, через уплотнения плунжерных пар ТНВД."),
    ("Загрязнение фильтра грубой очистки масла", "Промыть или заменить фильтрующий элемент."),
    ("Засорение заборника масляного насоса", "Промыть заборник."),
    ("Заедание плунжеров редукционного или дифференциального клапана масляного насоса",
     "Промыть клапан без разборки и при необходимости заменить."),
    ("Негерметичность соединений маслопроводов",
     "Проверить соединения и прокладки фильтров, трубок масляного насоса и фланца фильтра "
     "центробежной очистки. При необходимости подтянуть соединения или заменить прокладки."),
    ("Увеличение зазоров в коренных и шатунных подшипниках коленвала после длительной эксплуатации",
     "Направить двигатель в ремонт для замены вкладышей и, если потребуется, шлифовки шеек вала."),
)
OIL_NORMS = [
    "На прогретом двигателе: 400–700 кПа (4–7 кгс/см²) при 2100 об/мин и не менее "
    "100 кПа (1,0 кгс/см²) при минимальных оборотах холостого хода.",
    "После длительной эксплуатации допускается не ниже 300 кПа (3,0 кгс/см²) на номинальных "
    "оборотах и не ниже 50 кПа (0,5 кгс/см²) на минимальных оборотах.",
    "Руководство отдельно указывает рабочую температуру охлаждающей жидкости 75–100 °C.",
]
OIL_FIRST = "Проверьте количество масла в картере и охлаждающей жидкости."
CHAIN_FIRST = "Проверьте удлинение цепей ГРМ и состояние гидронатяжителя."
VALVE_FIRST = "При повторении стука проверьте зазоры клапанов на холодном двигателе (15–20 °C)."
COOLANT_FIRST = ("Осмотреть двигатель на подтекания охлаждающей жидкости и проверить уровень "
                 "масла в картере.")
COOLANT_RISK = ("Высокий предварительный риск: попадание охлаждающей жидкости в масло указано "
                "в руководстве как неисправность, требующая устранения, и снижает смазывающие "
                "свойства масла. До проверки не рекомендуется выпускать машину в рейс. "
                "Точная степень риска — по результатам очной дефектовки.")
COOLANT_CAUSES = [
    "Разрушение прокладок головок цилиндров.",
    "Недостаточная затяжка стакана форсунки.",
    "Подтекание по резиновым кольцам гильз цилиндров.",
    "Трещины в головке или блоке цилиндров.",
    "Перегрев: неисправный термометр, слабое натяжение или обрыв ремня водяного насоса, "
    "загрязнение радиатора, заедание термостата, газы в водяной рубашке, накипь.",
]
COOLANT_CHECK_ITEMS = [
    ("Проверить, нет ли воды в масле: осмотреть прокладки головок цилиндров, затяжку стакана "
     "форсунки и уплотнительные кольца гильз; при необходимости проверить головку и блок "
     "на трещины (PDF 54).", COOLANT_FRAGMENT),
    ("Проверить систему охлаждения: ремень водяного насоса, загрязнение радиатора, термостат, "
     "наличие газов в водяной рубашке и состояние накипи (PDF 54).", COOLANT_FRAGMENT),
    ("Сверить рабочую температуру охлаждающей жидкости с диапазоном 75–100 °C (PDF 4).",
     COOLANT_TEMP_FRAGMENT),
]
COOLANT_CHECKS = [text for text, _ in COOLANT_CHECK_ITEMS]
COOLANT_WORK = [
    "Заменить прокладку головки цилиндров при её разрушении.",
    "Подтянуть гайку крепления стакана форсунки.",
    "Заменить неисправные уплотнительные кольца гильз цилиндров.",
    "При трещинах головки или блока направить двигатель в ремонт.",
    "Устранить перегрев: натянуть или заменить ремень, очистить радиатор, заменить термостат, "
    "промыть систему охлаждения.",
]
COOLANT_NORM = ("Температура охлаждающей жидкости работающего двигателя должна находиться "
                "в пределах 75–100 °C.")

DRIVE = re.compile(r"перег(?:он|нат)|своим ходом|в рейс|можно ли ехать|доед(?:у|ет|ем)")
FIRST = re.compile(
    r"что (?:сначала )?проверить|что (?:делать|проверять)|с чего (?:начать|начнем|начнём)|"
    r"в первую очередь|что дальше|какие (?:причины|проверки)|почему (?:это )?опасно"
)
TOPICS = {
    "oil": re.compile(r"давлен\w*\s+масл|масл\w*\s+давлен|смазк|манометр|падает давлен|вкладыш|маслопровод"),
    "chain": re.compile(r"цеп[ьи]|цепей|натяжител|гидронатяж|шум|стуч|стук|грм"),
    "valves": re.compile(r"клапан|коромысл|зазор|стук"),
    "coolant": re.compile(r"охлажд|перегрев|эмульс|антифриз|вода\s+в\s+масл|масл\w*.*вода"),
}
OTHER_SPEC = re.compile(r"момент\w*\s+затяж|объем\w*\s+масл|объём\w*\s+масл|маркировк|артикул")
MEASUREMENT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кпа|кгс|бар)\b")
NORM = re.compile(r"(?:какая|какой|каков\w*) (?:норма|норматив|температура).*охлаждающ|"
                  r"охлаждающ\w*.*(?:температура|норматив)")
WORK = re.compile(r"(?:какие|что) (?:нужно )?(?:работы|заменить|ремонтировать|устранить)|"
                  r"как (?:устранить|отремонтировать|заменить)")


def case_for(vehicle: dict, diagnosis: Diagnosis) -> str:
    engine = vehicle["engine_model"]
    code = diagnosis.anomaly_type
    fuel = vehicle["fuel"]
    if (code == "F08" and fuel == "бензин") or (code in {"F12", "F13"} and fuel == "дизель"):
        return "mismatch"
    if code == "F02" and engine == "ЯМЗ-236М2":
        return "oil"
    if code == "F03" and engine == "ЯМЗ-236М2":
        return "coolant"
    if code == "F14" and engine in ZMZ409.engines:
        return "chain"
    if code == "F01" and engine == "ЗМЗ-5234.10":
        return "valves"
    return "unsupported"


def mismatch(vehicle: dict, diagnosis: Diagnosis) -> str:
    engine_type = "бензиновый" if vehicle["fuel"] == "бензин" else "дизельный"
    return (
        f"Mock ML указал {diagnosis.anomaly_type} «{diagnosis.anomaly_title}». В каталоге {vehicle['engine_model']} "
        f"указан как {engine_type} двигатель. Уточните фактическую модификацию двигателя "
        "и проверьте, что запись относится к выбранному ТС и сессии."
    )


def brief(vehicle: dict, diagnosis: Diagnosis) -> Brief:
    case = case_for(vehicle, diagnosis)
    if case == "oil":
        return Brief(
            risk="high", rationale="Если давление действительно ниже нормы, есть риск повреждения "
            "подшипников коленвала. Замер давления отсутствует.",
            first_check=OIL_FIRST, references=[OIL_LEVELS, OIL_PRESSURE],
        )
    if case == "coolant":
        return Brief(
            risk="high", rationale=COOLANT_RISK, first_check=COOLANT_FIRST, references=[COOLANT_PAGE],
        )
    if case == "chain":
        return Brief(
            risk="medium", rationale="Равномерный шум спереди на средних оборотах совпадает с признаком "
            "удлинения цепи. Руководство также относит неисправный гидронатяжитель к причинам стука.",
            first_check=CHAIN_FIRST, references=[CHAIN],
        )
    if case == "valves":
        return Brief(
            risk="unassessed", rationale="Стук может быть связан с увеличенным зазором клапанов. "
            "Запись с помехами требует проверки, фактические зазоры неизвестны."
            if diagnosis.signal_quality == "warning" else
            "Стук может быть связан с увеличенным зазором клапанов. Фактические зазоры неизвестны.",
            first_check="Повторите запись. " + VALVE_FIRST if diagnosis.signal_quality == "warning" else VALVE_FIRST,
            references=[VALVE_TEMP, VALVE_CLEARANCE, VALVE_NOISE],
        )
    return Brief(
        risk="unassessed",
        rationale=mismatch(vehicle, diagnosis) if case == "mismatch" else
        f"В тестовом режиме нет проверенной процедуры для {vehicle['engine_model']} и этого отклонения.",
        first_check="Сверьте модификацию двигателя и выбранную запись." if case == "mismatch" else
        "Уточните узел, симптомы и результаты контрольных измерений для дефектовки.",
    )


def analysis(vehicle: dict, diagnosis: Diagnosis) -> dict | None:
    if case_for(vehicle, diagnosis) != "coolant":
        return None
    references = [
        {"fragment_id": COOLANT_FIRST_FRAGMENT, "manual": YAMZ236.name, "pdf_page": 23},
        {"fragment_id": COOLANT_FRAGMENT, "manual": YAMZ236.name, "pdf_page": 54},
        {"fragment_id": COOLANT_TEMP_FRAGMENT, "manual": YAMZ236.name, "pdf_page": 4},
    ]
    return {
        "possible_causes": [{"text": item, "fragment_id": COOLANT_FRAGMENT} for item in COOLANT_CAUSES],
        "risk": [{"text": COOLANT_RISK, "fragment_id": COOLANT_FRAGMENT}],
        "checks": [{"text": text, "fragment_id": fragment} for text, fragment in COOLANT_CHECK_ITEMS],
        "repair_actions": [{"text": item, "fragment_id": COOLANT_FRAGMENT} for item in COOLANT_WORK],
        "references": references,
    }


def source_section(kind, items, *references) -> Section:
    if kind in {"causes", "risk"}:
        items = ["Гипотеза: " + item for item in items]
    return Section(kind=kind, items=items, references=list(references))


def oil_reply(signs: Section) -> Reply:
    return Reply(
        summary="Начните с проверки количества масла и охлаждающей жидкости.",
        sections=[
            signs,
            source_section("causes", [cause for cause, _ in OIL_CAUSES], OIL_LEVELS),
            source_section("checks", [OIL_FIRST], OIL_LEVELS),
            source_section("checks", OIL_NORMS, OIL_PRESSURE),
            source_section("work", [f"{cause}. {action}" for cause, action in OIL_CAUSES], OIL_LEVELS),
            source_section("clarify", ["Нужны фактическое давление в кПа, обороты и температура "
                                       "охлаждающей жидкости при замере."]),
        ],
    )


def coolant_checks_sections() -> list[Section]:
    sections = [source_section("checks", [COOLANT_FIRST], COOLANT_PAGE)]
    sections.extend(
        source_section("checks", [text], COOLANT_TABLE if fragment == COOLANT_FRAGMENT else COOLANT_TEMP)
        for text, fragment in COOLANT_CHECK_ITEMS
    )
    return sections


def coolant_reply(signs: Section, *, work_only: bool = False) -> Reply:
    sections = [signs]
    if not work_only:
        sections.extend([
            source_section("risk", [COOLANT_RISK], COOLANT_TABLE),
            source_section("causes", COOLANT_CAUSES, COOLANT_TABLE),
            *coolant_checks_sections(),
        ])
    sections.extend([
        source_section("work", COOLANT_WORK, COOLANT_TABLE),
        source_section("clarify", ["Нужны результаты проверки масла на эмульсию, уровня и цвета "
                                   "охлаждающей жидкости, а также осмотра мест подтёков."]),
    ])
    return Reply(
        summary=("Возможные работы по руководству. Выполнять после подтверждения причины."
                 if work_only else
                 "Начните с проверки уровня масла и охлаждающей жидкости. При попадании "
                 "охлаждающей жидкости в масло нужна очная дефектовка."),
        sections=sections,
    )


def chain_reply(signs: Section) -> Reply:
    return Reply(
        summary="Руководство связывает такой шум с возможным увеличением длины цепи.",
        sections=[
            signs,
            source_section("causes", [
                "При увеличении длины цепи возможен равномерный шум спереди двигателя, который "
                "усиливается на средних оборотах и при переменных нагрузках.",
                "Неисправный гидронатяжитель указан как отдельная возможная причина стука.",
            ], CHAIN),
            source_section("checks", [CHAIN_FIRST], CHAIN),
            source_section("work", [
                "При подтверждённой неисправности гидронатяжитель подлежит замене.",
                "При подтверждённом удлинении цепей руководство предусматривает при необходимости "
                "замену цепей со звёздочками комплектом.",
            ], CHAIN),
            source_section("clarify", ["Нужны результаты проверки удлинения цепей и гидронатяжителя."]),
        ],
    )


def valve_reply(signs: Section, signal_quality: str) -> Reply:
    return Reply(
        summary="Проверьте зазоры клапанов. При помехах сначала повторите запись." if signal_quality == "warning"
        else "Проверьте зазоры клапанов на холодном двигателе.",
        sections=[
            signs,
            source_section("causes", ["Руководство относит слишком большие зазоры между клапанами "
                                      "и коромыслами к возможным причинам стука."], VALVE_NOISE),
            source_section("checks", ["Проверьте зазоры при температуре двигателя 15–20 °C. "
                                      "Норма на холодном двигателе 0,20–0,30 мм."],
                           VALVE_TEMP, VALVE_CLEARANCE),
            source_section("work", ["Если зазор выходит за допуск, отрегулируйте его по процедуре "
                                    "руководства и проверьте повторно."], VALVE_CLEARANCE),
            source_section("clarify", ["Уточните, остаётся ли стук после повторной записи, и сообщите "
                                       "измеренные зазоры, температуру двигателя и номера цилиндров."]
                           if signal_quality == "warning" else
                           ["Сообщите измеренные зазоры, температуру двигателя и номера цилиндров."]),
        ],
    )


def fluid_report_reply(question: str, history) -> Reply | None:
    previous = next((analysis["fluid_check"] for message in reversed(history)
                     if message["role"] == "assistant" and (analysis := message.get("analysis"))
                     and isinstance(analysis.get("fluid_check"), dict)), None)
    levels = fluid_check.report(question, previous)
    if levels is None:
        return None
    action, clarification = fluid_check.next_action(levels)
    guidance = action or clarification
    return Reply(summary=guidance,
                 sections=[source_section("checks" if action else "clarify", [guidance], OIL_LEVELS)],
                 analysis={"fluid_check": levels})


def answer(vehicle: dict, diagnosis: Diagnosis, question: str, *, history=()) -> Reply:
    text = question.casefold()
    signs = source_section("signs", [diagnosis.description])
    if diagnosis.status == "check_failed":
        return Reply(summary="Повторите запись и запустите проверку новой сессии. "
                             "По этой записи вывод о двигателе невозможен.", sections=[signs])
    if diagnosis.status == "normal":
        if DRIVE.search(text):
            return Reply(summary="По акустической проверке нельзя принять решение о перегоне. "
                                 "Сообщите, почему возник вопрос, и укажите показания приборов этой сессии.",
                         sections=[signs])
        if FIRST.search(text):
            return Reply(summary=f"По сессии {diagnosis.session_id} значимых отклонений не обнаружено. "
                                 "Продолжайте штатный контроль двигателя.", sections=[signs])
        return Reply(summary="В этой сессии значимых отклонений нет. Уточните симптом или узел, "
                             "по которому нужна проверка.", sections=[signs])

    case = case_for(vehicle, diagnosis)
    if case == "mismatch":
        return Reply(summary=mismatch(vehicle, diagnosis), sections=[signs])
    if case == "oil" and not (DRIVE.search(text) or MEASUREMENT.search(text)):
        if reply := fluid_report_reply(question, history):
            return reply
    if case == "coolant" and NORM.search(text):
        return Reply(
            summary=COOLANT_NORM,
            sections=[signs, source_section("checks", [COOLANT_NORM], COOLANT_TEMP)],
        )
    if case == "coolant" and WORK.search(text):
        return coolant_reply(signs, work_only=True)
    if not DRIVE.search(text) and not MEASUREMENT.search(text) and (conflict := conflict_case(question, vehicle["engine_model"])):
        fragments = {item["fragment_id"]: item for item in verified_fragments()}
        if all(fragment in fragments for fragment in conflict["fragment_ids"]):
            sections = [signs]
            for fragment in conflict["fragment_ids"]:
                item = fragments[fragment]
                sections.append(source_section("checks", [item["text"]],
                    YAMZ236.reference(item["pdf_page"]) if item["manual_id"] == "yamz-236-3902150-b-re" else
                    {"manual": item["manual"], "pdf_page": item["pdf_page"]}))
            if conflict.get("limitations"):
                sections.append(source_section("clarify", ["Ограничения: " + conflict["limitations"]]))
            return Reply(summary=conflict["explanation"], sections=sections)
    if DRIVE.search(text):
        sections = [signs]
        if case == "oil":
            sections.extend([
                source_section("checks", [OIL_FIRST], OIL_LEVELS),
                source_section("checks", OIL_NORMS, OIL_PRESSURE),
                source_section("clarify", ["Нужны показания исправного манометра на прогретом двигателе "
                                           "при 2100 об/мин и на минимальном холостом ходу, температура "
                                           "охлаждающей жидкости и состояние сигнализатора."]),
            ])
        elif case == "chain":
            sections.append(source_section("checks", [CHAIN_FIRST], CHAIN))
        elif case == "coolant":
            sections.extend([
                source_section("risk", [COOLANT_RISK], COOLANT_TABLE),
                *coolant_checks_sections(),
                source_section("clarify", ["Нужны результаты проверки масла на эмульсию, уровня и цвета "
                                           "охлаждающей жидкости, а также осмотра мест подтёков."]),
            ])
        else:
            sections.append(source_section("clarify", ["Уточните симптомы и результаты очной проверки."]))
        return Reply(summary="Данных для допуска к перегону своим ходом недостаточно. "
                             "Перед решением нужна очная проверка. Высокий приоритет заявки не является прямым запретом из руководства.", sections=sections)

    if case == "oil" and MEASUREMENT.search(text):
        return Reply(
            summary="Для оценки замера уточните обороты, степень прогрева и применим ли допуск "
                    "для двигателя после длительной эксплуатации.",
            sections=[signs, source_section("checks", OIL_NORMS, OIL_PRESSURE)],
        )
    if vehicle["engine_model"] == "ЯМЗ-536" and re.fullmatch(
        r"(?:какое|какое должно быть) давление масла на прогретом двигателе[?.! ]*", text
    ):
        return Reply(summary="Норматив давления масла на прогретом ЯМЗ-536.", sections=[
            signs,
            *(source_section("checks", [fragment["text"]], Reference(
                manual=fragment["manual"], pdf_page=fragment["pdf_page"],
            )) for fragment in YAMZ536_OIL_PRESSURE),
        ])
    topic = TOPICS.get(case)
    if topic and not OTHER_SPEC.search(text) and (topic.search(text) or FIRST.search(text)):
        if case == "oil":
            return oil_reply(signs)
        if case == "chain":
            return chain_reply(signs)
        if case == "coolant":
            return coolant_reply(signs)
        return valve_reply(signs, diagnosis.signal_quality)
    return Reply(summary=f"Для этого вопроса в тестовом режиме нет подтверждённой страницы "
                         f"для двигателя {vehicle['engine_model']}. Уточните узел и что именно "
                         "нужно проверить. При необходимости передайте вопрос на дефектовку.")


def draft(vehicle: dict, diagnosis: Diagnosis) -> DraftFields | None:
    case = case_for(vehicle, diagnosis)
    if case == "oil":
        return DraftFields(
            conclusion="По записи подозрение на низкое давление масла. Глухой стук в нижней части блока "
                       "и сигнал низкого давления. Фактическое давление требуется измерить.",
            urgency="high",
            operations=[
                OIL_FIRST,
                "Проверить давление на прогретом двигателе при 2100 об/мин и при минимальных оборотах "
                "холостого хода. Зафиксировать температуру, обороты и давление.",
                "Если давление ниже нормы, проверить причины по таблице руководства на странице PDF 53.",
            ],
            spare_parts=[], references=[OIL_PRESSURE, OIL_LEVELS],
        )
    if case == "chain":
        return DraftFields(
            conclusion="По записи подозрение на удлинение цепи или неисправность гидронатяжителя. "
                       "Равномерный шум спереди усиливается на средних оборотах.",
            urgency="medium", operations=[CHAIN_FIRST, "Зафиксировать результаты дефектовки цепей и натяжителя."],
            spare_parts=[], references=[CHAIN],
        )
    if case == "valves":
        return DraftFields(
            conclusion="По записи подозрение на увеличенный зазор в клапанном механизме. Требуется замер.",
            urgency="medium", operations=[VALVE_FIRST, "Зафиксировать зазоры и условия измерения."],
            spare_parts=[], references=[VALVE_TEMP, VALVE_CLEARANCE, VALVE_NOISE],
        )
    return None
