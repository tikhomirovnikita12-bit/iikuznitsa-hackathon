"""Keep elliptical questions attached to a manual topic and resolve reviewed passages together."""
import re

FOLLOW = re.compile(
    r"^(?:а )?(?:как (?:это|их|его|её) (?:проверить|измерить|сделать)|как именно|подробнее|"
    r"при каких условиях(?: (?:проверять|замерять|измерять))?|чем проверить|на холостых)[?.! ]*$", re.I,
)
PRESSURE = re.compile(r"давлен\w*\s+масла", re.I)
MOVE = re.compile(r"перег[но]|своим ходом|можно ли (?:ехать|доехать|продолжать движение)", re.I)


def subject(question, history):
    if FOLLOW.fullmatch(question.strip()):
        return next((q for q in reversed(history) if not FOLLOW.fullmatch(q.strip())), "")
    return question


OIL_NORM = "yamz-536-01-02-rk-p013-oil-pressure"
OIL_BREAK_IN = "yamz-536-3902150-re-p159-break-in-oil-pressure"


def reviewed_oil_pressure(index, engine, topic):
    if engine != "ЯМЗ-536" or not PRESSURE.search(topic):
        return None
    norm = index.fragments.get(OIL_NORM)
    if not norm:
        return []
    ids = [OIL_NORM, *norm.get("supporting_fragments", ())]
    if "обкатк" in topic.casefold():
        ids.append(OIL_BREAK_IN)
    if any(engine not in index.fragments.get(fid, {}).get("applicable_engine_models", ()) for fid in ids):
        return []
    return [(fid, index.fragments[fid]["text"]) for fid in ids]


def blocked_message(reason, diagnosis, engine):
    node = diagnosis.get("anomaly_title") or "указанный узел"
    if diagnosis.get("signal_quality") == "bad" or diagnosis["status"] == "check_failed":
        return "Повторите запись: " + diagnosis["description"] + ". По этой записи состояние двигателя оценить нельзя."
    return (f"{reason} Рекомендация механику: сверьте исполнение узла «{node}» на {engine} и повторите проверку. "
            "Если расхождение сохраняется или процедура не подтверждена, передайте узел на очную дефектовку.")
