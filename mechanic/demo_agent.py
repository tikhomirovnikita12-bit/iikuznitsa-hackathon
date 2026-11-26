"""Проверенные тестовые ответы для интеграции интерфейса до подключения агента команды."""

import re

from .contracts import Brief, Diagnosis, DraftFields, Reply, Section
from .sources import YAMZ236, ZMZ409, ZMZ5234


OIL_LEVELS = YAMZ236.reference(53)
OIL_PRESSURE = YAMZ236.reference(19)
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

CREATE = re.compile(r"\b(?:созда\w*|оформ\w*|подготов\w*|сдела\w*)\s+"
                    r"(?:мне\s+)?(?:черновик\s+)?заявк\w*\b|^заявка(?:\s+на\s+дефектовку)?[.!]?$")
DRIVE = re.compile(r"перег(?:он|нат)|своим ходом|в рейс|можно ли ехать|доед(?:у|ет|ем)")
FIRST = re.compile(
    r"что (?:сначала )?проверить|что (?:делать|проверять)|с чего (?:начать|начнем|начнём)|"
    r"в первую очередь|что дальше|какие (?:причины|проверки)|почему (?:это )?опасно"
)
TOPICS = {
    "oil": re.compile(r"давлен\w*\s+масл|масл\w*\s+давлен|смазк|манометр|падает давлен|вкладыш|маслопровод"),
    "chain": re.compile(r"цеп[ьи]|цепей|натяжител|гидронатяж|шум|стуч|стук|грм"),
    "valves": re.compile(r"клапан|коромысл|зазор|стук"),
}
OTHER_SPEC = re.compile(r"момент\w*\s+затяж|объем\w*\s+масл|объём\w*\s+масл|маркировк|артикул")
MEASUREMENT = re.compile(r"\d+(?:[.,]\d+)?\s*(?:кпа|кгс|бар)\b")


def wants_draft(question: str) -> bool:
    return bool(CREATE.search(question.casefold().strip()))


def case_for(vehicle: dict, diagnosis: Diagnosis) -> str:
    engine = vehicle["engine_model"]
    code = diagnosis.anomaly_type
    if code == "F08" and vehicle["fuel"] == "бензин":
        return "mismatch"
    if code == "F02" and engine == "ЯМЗ-236М2":
        return "oil"
    if code == "F14" and engine in ZMZ409.engines:
        return "chain"
    if code == "F01" and engine == "ЗМЗ-5234.10":
        return "valves"
    return "unsupported"


def mismatch(vehicle: dict) -> str:
    return (
        f"Mock ML указал F08 «Дизельная топливоподача». В каталоге {vehicle['engine_model']} "
        "указан как бензиновый двигатель. Уточните фактическую модификацию двигателя "
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
        rationale=mismatch(vehicle) if case == "mismatch" else
        f"В тестовом режиме нет проверенной процедуры для {vehicle['engine_model']} и этого отклонения.",
        first_check="Сверьте модификацию двигателя и выбранную запись." if case == "mismatch" else
        "Уточните узел, симптомы и результаты контрольных измерений для дефектовки.",
    )


def source_section(kind, items, *references) -> Section:
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


def answer(vehicle: dict, diagnosis: Diagnosis, question: str) -> Reply:
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
        return Reply(summary=mismatch(vehicle), sections=[signs])
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
        else:
            sections.append(source_section("clarify", ["Уточните симптомы и результаты очной проверки."]))
        return Reply(summary="Данных для допуска к перегону своим ходом недостаточно. "
                             "Перед решением нужна очная проверка.", sections=sections)

    if case == "oil" and MEASUREMENT.search(text):
        return Reply(
            summary="Для оценки замера уточните обороты, степень прогрева и применим ли допуск "
                    "для двигателя после длительной эксплуатации.",
            sections=[signs, source_section("checks", OIL_NORMS, OIL_PRESSURE)],
        )
    topic = TOPICS.get(case)
    if topic and not OTHER_SPEC.search(text) and (topic.search(text) or FIRST.search(text)):
        if case == "oil":
            return oil_reply(signs)
        if case == "chain":
            return chain_reply(signs)
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
