"""Keep elliptical questions attached to a manual topic and resolve reviewed passages together."""
import re

FOLLOW = re.compile(r"^(?:а )?(?:как (?:это|их|его|её) (?:проверить|измерить|сделать)|как именно|а подробнее|подробнее|при каких условиях|чем проверить)[?.! ]*$", re.I)
VALVES = re.compile(r"клапан", re.I)
BEARINGS = re.compile(r"коренн\w*\s+(?:опор|подшипник)|крыш\w*\s+коренн", re.I)
PRESSURE = re.compile(r"давлен\w*\s+масла", re.I)
MOVE = re.compile(r"перег[но]|своим ходом|можно ли (?:ехать|доехать|продолжать движение)", re.I)


def subject(question, history):
    if FOLLOW.fullmatch(question.strip()):
        return next((q for q in reversed(history) if not FOLLOW.fullmatch(q.strip())), "")
    return question


# Each item pairs a server-owned corpus ID with a reviewed, scoped excerpt.
VALVE_PAIR = (
    ("yamz-236-3902150-b-re-p031-valve-clearance",
     "РЭ: регулировочный зазор 0,25–0,30 мм. После проворачивания коленвала при повторной проверке — "
     "0,20–0,40 мм. Для регулировки указаны холодный двигатель или менее 15 минут после остановки."),
    ("yamz-236-01-01-rk-p269-l001",
     "РК, п. 6.4.1: перед обкаткой и после неё проверять и регулировать зазоры не ранее чем через "
     "15 минут после остановки. Уточните, проводится ТО или ремонт с обкаткой. Для демонстрации "
     "предлагается холодный двигатель после выдержки не менее 15 минут."),
)
SUPPORT_PAIR = (
    ("yamz-236-01-01-rk-p041-l005",
     "РК, стр. 41: установка коренных опор. Приведены два приёма затяжки и явная перекрёстная "
     "ссылка на раздел «Сборка двигателя»."),
    ("yamz-236-01-01-rk-p248-l001",
     "Раздел «Сборка двигателя», п. 5.2.9: для крышек при сборке приведены не менее трёх приёмов "
     "и 421–461 Н·м. Стяжные болты описаны отдельно. Уточните, какую операцию и какой крепёж проверяете."),
)
OIL_PAIR = (
    ("yamz-536-3902150-re-p049-oil-pressure",
     "РЭ: при прогреве до 80–100 °C давление при номинальной частоте 400–635 кПа, "
     "при минимальной частоте холостого хода — не менее 100 кПа."),
    ("yamz-536-3902150-re-p159-l008",
     "РЭ, раздел обкатки: при 700±50 мин⁻¹ — не менее 100 кПа; при номинальных оборотах — "
     "400–635 кПа. Уточните температуру и частоту вращения при замере."),
    ("yamz-536-01-02-rk-p013-l013",
     "РК: 400–635 кПа относится к номинальной частоте при прогреве до 80–100 °C."),
)


def reviewed(index, engine, topic):
    normalized = topic.casefold()
    if engine == "ЯМЗ-236М2" and VALVES.search(topic) and re.search(r"зазор|регулир", normalized):
        pair = VALVE_PAIR
    elif engine == "ЯМЗ-236М2" and BEARINGS.search(topic):
        pair = SUPPORT_PAIR
    elif engine == "ЯМЗ-536" and PRESSURE.search(topic):
        pair = OIL_PAIR
    else:
        return None
    if any(engine not in index.fragments.get(fid, {}).get("applicable_engine_models", []) for fid, _ in pair):
        return []
    if pair is OIL_PAIR:
        fid = pair[0][0]
        return ((fid, index.fragments[fid]["text"]), *pair[1:])
    return pair


def blocked_message(reason, diagnosis, engine):
    node = diagnosis.get("anomaly_title") or "указанный узел"
    if diagnosis.get("signal_quality") == "bad" or diagnosis["status"] == "check_failed":
        return "Повторите запись: " + diagnosis["description"] + ". По этой записи состояние двигателя оценить нельзя."
    return (f"{reason} Рекомендация механику: сверьте исполнение узла «{node}» на {engine} и повторите проверку. "
            "Если расхождение сохраняется или процедура не подтверждена, передайте узел на очную дефектовку.")
