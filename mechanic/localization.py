"""Localize chat text while the agent and source registry use Russian evidence."""

from collections import Counter
import csv
from html import escape
from html.parser import HTMLParser
import json
import os
import re
import unicodedata

import httpx

from .config import ROOT, endpoint
from .contracts import Reference, Reply, Section


LOCALES = json.loads((ROOT / "data/manuals/demo-locales.json").read_text(encoding="utf-8"))
with (ROOT / "data/manuals/translation-glossary.csv").open(encoding="utf-8") as entries:
    GLOSSARY = list(csv.DictReader(entries))
DEMO_CASES = {
    "oil": ("ЯМЗ-236М2", "F02", "yamz-236-3902150-b-re-p019-oil-pressure-original"),
    "chain": ("ЗМЗ-40905.10", "F14", "zmz-40904-40905-rk-p146-f14-tensioner-with-adapter"),
}
TERM_UNIT = r"кгс\s*[·⋅×./-]?\s*(?:см[²2]|м)|(?:кПа|МПа|Па|бар|kPa|MPa|bar|psi|Н[·⋅]?м|N[·⋅]?m|об\s*/\s*мин|r/min|rpm|мм|mm|°\s*[СCF]|%|шт\.?)"
NUMBER = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d+)(?:[.,]\d+)*"
VALUE = rf"[+−-]?{NUMBER}(?:\s*[-–—±]\s*[+−-]?{NUMBER})?(?:\s*(?:{TERM_UNIT})(?!\w))?"
IDENTIFIER = r"(?<!\w)(?:[A-ZА-Я]+[-–]?\d[\w.]*|[A-ZА-Я]\d{3}[A-ZА-Я]{2}\d{2,3})(?!\w)"
PROTECTED = re.compile(rf"{IDENTIFIER}|(?<!\w){VALUE}|(?<!\w)(?:{TERM_UNIT})(?!\w)", re.IGNORECASE)
TOKEN = re.compile(r"⟦MECH_([0-9]+)⟧")
HAS_VALUE = re.compile(r"\d")
UNAVAILABLE = "Переводчик недоступен. Выберите русский или один из демо-вопросов."
SCRIPT = {"hy": re.compile(r"[\u0531-\u0587]"), "hi": re.compile(r"[\u0900-\u097f]")}
NO_SOURCE = ("Для этого вопроса в тестовом режиме нет подтверждённой страницы для двигателя {engine}. "
             "Уточните узел и что именно нужно проверить. При необходимости передайте вопрос на дефектовку.")
QUESTION_WORDS = {
    "ru": "что как когда сначала проверить почему нужно машина укажите двигатель давление",
    "be": "што трэба спачатку праверыць гэтай машыне рухавік ціск колькі чаму",
    "az": "maşında əvvəlcə nəyi yoxlamalıyam təzyiqi mühərrikdə qədər lazımdır niyə",
    "kk": "осы көлікте алдымен нені тексеруім қозғалтқышта қысымы қанша керек",
    "ky": "бул унаада биринчи эмнени текшеришим кыймылдаткычта басымы канча керек",
    "tk": "ulagda ilki nämäni barlamaly hereketlendirijide basyşy näçe bolmaly haçan",
    "uz": "mashinada avval nimani tekshirishim dvigatelda bosimi qancha kerak nima",
    "tg": "дар ин мошин аввал чиро бояд тафтиш кунам муҳаррик фишори қадар чаро",
}


class TranslationError(Exception):
    """A translation could not be delivered without changing protected data."""


def catalog():
    return [{
        "code": code,
        "name": entry["name"],
        "examples": [
            {"engine_model": engine, "fault_code": fault, "question": entry[case][0]}
            for case, (engine, fault, _) in DEMO_CASES.items()
        ],
    } for code, entry in LOCALES.items()]


def canonical_question(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold()
                    .translate(str.maketrans("’‘՚", "'''", "؟՞")).split()).rstrip("?!.։।")


def detect_language(text, fallback, *, engine):
    normalized = canonical_question(text)
    for code, entry in LOCALES.items():
        if any(normalized == canonical_question(entry[case][0]) for case in DEMO_CASES):
            return code
    sample = PROTECTED.sub("", text.replace(engine, "")).strip()
    letters = sum(character.isalpha() for character in sample)
    if not letters:
        return fallback
    for code, alphabet in SCRIPT.items():
        if len(alphabet.findall(sample)) >= letters / 2:
            return code
    words = set(re.findall(r"[^\W\d_]+", sample.casefold()))
    signatures = sorted(((len(words.intersection(signature.split())), code)
                         for code, signature in QUESTION_WORDS.items()), reverse=True)
    if signatures[0][0] >= 2 and signatures[0][0] > signatures[1][0]:
        return signatures[0][1]
    try:
        if endpoint("LOCAL_TRANSLATION_URL"):
            scores = local_request("detect", {"text": sample})["languages"]
        elif cloud_configured():
            project = os.environ["GOOGLE_TRANSLATION_PROJECT"]
            url = f"https://translation.googleapis.com/v3/projects/{project}/locations/global:detectLanguage"
            result = google_request(url, {"mimeType": "text/plain", "content": sample})
            scores = {row["languageCode"]: row["confidence"] for row in result["languages"]}
        else:
            return fallback
    except (TranslationError, KeyError, TypeError):
        return fallback
    candidates = sorted(((score, code) for code, score in scores.items() if code in LOCALES), reverse=True)
    return candidates[0][1] if candidates and candidates[0][0] >= 0.2 else fallback


def demo_case(engine, diagnosis, message, input_language):
    if diagnosis.status != "anomaly" or diagnosis.signal_quality == "bad":
        return None
    for case, (model, fault, _) in DEMO_CASES.items():
        if (engine, diagnosis.anomaly_type) != (model, fault):
            continue
        question = canonical_question(message)
        if any(question == canonical_question(LOCALES[language][case][0])
               for language in {input_language, "ru"}):
            return case
    return None


def demo_reply(case, locale, evidence):
    engine, _, fragment_id = DEMO_CASES[case]
    reference = Reference.model_validate(evidence.resolve_source(fragment_id, engine))
    fragment = evidence.fragments[fragment_id]
    pressure, rpm, length = ("кПа", "об/мин", "мм") if locale == "ru" else ("kPa", "rpm", "mm")
    values = {
        "engine": engine, "range": f"400–700 {pressure}", "rpm": f"2100 {rpm}",
        "idle": f"100 {pressure}", "worn_nominal": f"300 {pressure}", "worn_idle": f"50 {pressure}",
        "part": "40904.1006100", "length": f"55,5 ± 0,5 {length}",
    }
    _, main, extra = LOCALES[locale][case]
    summary, detail = (text.format_map(values) for text in (main, extra))
    reply = Reply(summary=summary, sections=[Section(kind="checks", items=[summary, detail], references=[reference])])
    quote = {"text": fragment["text"], "references": [reference.model_dump()]}
    return reply, quote


class TranslationText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def protect(text, engine):
    values = []
    pattern = re.compile(rf"{re.escape(engine)}|{PROTECTED.pattern}", re.IGNORECASE)

    def replace(match):
        values.append(match[0])
        return f'⟦MECH_{len(values) - 1}⟧'

    masked = pattern.sub(replace, text)
    html = TOKEN.sub(lambda match: f'<span class="notranslate" translate="no">{match[0]}</span>', escape(masked))
    return html, values


def restore(translated, values):
    parser = TranslationText()
    parser.feed(translated)
    parser.close()
    text = "".join(parser.parts).strip()
    expected = Counter(str(index) for index in range(len(values)))
    if not text or Counter(TOKEN.findall(text)) != expected or HAS_VALUE.search(TOKEN.sub("", text)):
        raise TranslationError("Перевод изменил защищённые значения.")
    return TOKEN.sub(lambda match: values[int(match[1])], text)


def cloud_configured():
    return bool(os.environ.get("GOOGLE_TRANSLATION_PROJECT") and os.environ.get("GOOGLE_TRANSLATION_GLOSSARY"))


def local_request(action, payload):
    try:
        response = httpx.post(f"{endpoint('LOCAL_TRANSLATION_URL')}/{action}", json=payload, timeout=22)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise TranslationError(UNAVAILABLE) from error


def translate_local(texts, source, target, engine):
    terms = {row[source].casefold(): row[target] for row in GLOSSARY if row[source] and row[target]}
    terms_pattern = "|".join(map(re.escape, sorted(terms, key=len, reverse=True)))
    locked = re.compile(rf"{re.escape(engine)}|{PROTECTED.pattern}|(?<!\w)(?:{terms_pattern})(?!\w)", re.IGNORECASE)
    parts_by_text = []
    pending = {}
    for text in texts:
        parts = []
        offset = 0
        for match in locked.finditer(text):
            parts.append((text[offset:match.start()], True))
            parts.append((terms.get(match[0].casefold(), match[0]), False))
            offset = match.end()
        parts.append((text[offset:], True))
        parts_by_text.append(parts)
        pending.update(dict.fromkeys(translation_segment(part) for part, unlocked in parts
                                     if unlocked and any(c.isalpha() for c in part)))
    if pending:
        try:
            translated = local_request("translate", {"texts": list(pending), "source": source, "target": target})["texts"]
            for original, value in zip(pending, translated, strict=True):
                if not isinstance(value, str) or not value.strip() or HAS_VALUE.search(value):
                    raise TranslationError("Локальный перевод изменил защищённые значения.")
                pending[original] = translation_segment(value)
        except (KeyError, ValueError, TypeError) as error:
            raise TranslationError(UNAVAILABLE) from error
    return ["".join(part.replace(segment, pending[segment], 1)
                    if unlocked and (segment := translation_segment(part)) in pending else part
                    for part, unlocked in parts) for parts in parts_by_text]


def translation_segment(value):
    return re.sub(r"^[^\w]*|[^\w]*$", "", value, flags=re.UNICODE)


def google_request(url, payload):
    project = os.environ.get("GOOGLE_TRANSLATION_PROJECT")
    glossary = os.environ.get("GOOGLE_TRANSLATION_GLOSSARY")
    if not project or not glossary:
        raise TranslationError(UNAVAILABLE)

    try:
        from google.auth import default
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import Request
    except ImportError as error:
        raise TranslationError(UNAVAILABLE) from error

    headers = {}
    try:
        credentials, _ = default(scopes=["https://www.googleapis.com/auth/cloud-translation"])
        credentials.before_request(Request(), "POST", url, headers)
        with httpx.Client(timeout=8) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
        return response.json()
    except (GoogleAuthError, httpx.HTTPError, ValueError, TypeError, AttributeError) as error:
        raise TranslationError(UNAVAILABLE) from error


def translate(texts, source, target, *, engine):
    if source == target:
        return texts
    if any(TOKEN.search(text) for text in texts):
        raise TranslationError("В тексте есть служебные маркеры перевода.")
    if not cloud_configured():
        if endpoint("LOCAL_TRANSLATION_URL"):
            return translate_local(texts, source, target, engine)
        raise TranslationError(UNAVAILABLE)
    project = os.environ["GOOGLE_TRANSLATION_PROJECT"]
    glossary = os.environ["GOOGLE_TRANSLATION_GLOSSARY"]
    parent = f"projects/{project}/locations/us-central1"
    url = f"https://translation.googleapis.com/v3/{parent}:translateText"
    unique = dict.fromkeys(texts)
    protected = {text: protect(text, engine) for text in unique}
    try:
        rows = google_request(url, {
            "mimeType": "text/html", "sourceLanguageCode": source, "targetLanguageCode": target,
            "contents": [masked for masked, _ in protected.values()],
            "glossaryConfig": {"glossary": f"{parent}/glossaries/{glossary}", "ignoreCase": True},
        })["glossaryTranslations"]
        for (text, (_, values)), row in zip(protected.items(), rows, strict=True):
            unique[text] = restore(row["translatedText"], values)
    except (ValueError, KeyError, TypeError, AttributeError) as error:
        raise TranslationError("Сервис перевода недоступен. Повторите запрос или выберите русский.") from error
    return [unique[text] for text in texts]


def translated_reply(reply, language, engine):
    if not reply.sections and reply.summary == NO_SOURCE.format(engine=engine):
        return {"language": language, "summary": LOCALES[language]["no_source"].format(engine=engine),
                "sections": [], "original_sections": []}
    texts = [reply.summary, *(item for section in reply.sections for item in section.items)]
    summary, *items = translate(texts, "ru", language, engine=engine)
    translated = iter(items)
    sections = [section.model_copy(update={"items": [next(translated) for _ in section.items]})
                for section in reply.sections]
    return {"language": language, "summary": summary, "sections": [section.model_dump() for section in sections],
            "original_sections": [section.model_dump() for section in reply.sections if section.references]}


def public_messages(messages):
    result = []
    for message in messages:
        visible = dict(message)
        if "input_text" in visible:
            visible["text"] = visible.pop("input_text")
        visible.update(visible.pop("localized", {}))
        result.append(visible)
    return result
