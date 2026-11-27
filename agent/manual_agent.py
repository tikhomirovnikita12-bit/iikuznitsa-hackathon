"""Search the selected engine's manuals and return source passages."""

import json
import re
import time

from manual_search.search import COMMON_WORDS, normalized_words, tokens


def schema(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


STRING = {"type": "string"}
QUESTION_WORDS = set(tokens(normalized_words(
    "проверить измерить замерить узнать делать найти понять сверить посмотреть "
    "исправить устранить сначала проверка результат диагностики"
)))


def passages(hit):
    """Keep complete passages and preserve the conditions attached to numeric limits."""
    raw = hit["text"]
    if re.search(r"[\ufffd\ue000-\uf8ff\u2500-\u259f]|вероятная причина\s+способ устранения", raw, re.I):
        return []
    if re.match(r"\s*(?:рис\.|таблица\s+\d)", raw, re.I):
        return []
    if hit.get("verified"):
        return [line for line in raw.splitlines() if 12 <= len(line) <= 600 and not line.rstrip().endswith(":")]
    clean = re.sub(r"(?<=[а-яё])-\s*\n\s*(?=[а-яё])", "", raw)
    clean = " ".join(clean.split())
    if len(clean) > 700 or re.search(r"[а-яё]- [а-яё]|\d+\s*[–—]\s*\w+.*?;\s*\d+\s*[–—]", clean):
        return []
    if not clean.endswith((".", "!", "?")):
        return []
    if any(ch.isdigit() for ch in clean):
        return [clean] if len(clean) <= 500 else []
    return [sent for sent in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", clean) if 16 <= len(sent) <= 350]


class ManualAgent:
    def __init__(self, manuals):
        self.manuals = manuals

    def search(self, query, diagnosis, trace, *, initial_check=False):
        started = time.monotonic()
        hits = self.manuals.find(query, diagnosis, initial_check=initial_check)
        trace["tools"].append(dict(name="search", agent="manual", query=query, seconds=time.monotonic() - started,
                                   pages=[(hit["manual"], hit["pdf_page"]) for hit in hits]))
        return hits

    def cite(self, reply, field, text, fragment_id):
        ref = self.manuals.reference(fragment_id, reply["ml_observations"]["engine_model"])
        if ref not in reply["references"]:
            reply["references"].append(ref)
        item = dict(text=text, fragment_id=fragment_id)
        if item not in reply[field]:
            reply[field].append(item)

    def answer(self, reply, query, question, provider, trace, *, alternate="", coolant=False, next_step=False, previous=None):
        ml = reply["ml_observations"]
        action = reply["intent"]
        selection_question = "Что проверить сначала?" if next_step else question
        if action == "diagnosis":
            selection_question += f" {ml['engine_model']} {ml['anomaly_title'] or ''} {ml['description']}"
        if previous:
            selection_question += " " + previous["text"]

        initial = action == "diagnosis" or next_step
        hits = self.search(query, ml, trace, initial_check=initial)
        if not hits and alternate.strip() and alternate.casefold() != query.casefold():
            hits = self.search(alternate, ml, trace, initial_check=initial)
        self.select(reply, selection_question, query, hits, provider, trace, coolant, next_step, previous)

    def select(self, reply, question, query, hits, provider, trace, coolant, next_step, previous):
        # The model selects excerpts. The response only displays the indexed text.
        options = {}
        verified = {}
        subject = set(tokens(normalized_words(query))) - COMMON_WORDS - QUESTION_WORDS
        coolant_followup = bool(previous and "уровень охлаждающей жидкости" in previous["text"].lower())
        for hit in hits:
            for index, line in enumerate(passages(hit)[:10]):
                if previous and (line == previous["text"] or hit["fragment_id"] == previous["fragment_id"]):
                    continue
                if coolant_followup and "уровень жидкости в расширительном бачке" not in line.lower():
                    continue
                if coolant and not all(term in line.lower() for term in ("уров", "охлаждающ", "холодн")):
                    continue
                if not (previous or coolant) and subject and subject.isdisjoint(tokens(normalized_words(line))):
                    continue
                key = f"{hit['fragment_id']}:{index}"
                options[key] = line
                if hit.get("verified"):
                    verified[key] = line
        if not options:
            return
        if hits[0].get("verified") and verified:
            options = verified
        if coolant or (coolant_followup and len(options) == 1):
            key, passage = next(iter(options.items()))
            if coolant_followup:
                passage = re.sub(r"^\d+\.\s*", "", passage)
            self.cite(reply, "checks", passage, key.rsplit(":", 1)[0])
            return

        action = reply["intent"]
        fields = ["checks"] if previous or next_step or action == "ticket" else ["checks", "risk", "possible_causes"]
        limit = 1 if previous or next_step else 3 if action == "diagnosis" else 2
        item = schema(source={"type": "string", "enum": list(options)}, field={"type": "string", "enum": fields})
        answer = provider.ask(
            "Выбери только прямой ответ на вопрос из предложенных фрагментов. "
            "Для вопроса о первом шаге или заявки выбери начальную проверку в checks. "
            "Для измерения выбери полный норматив вместе с условиями. "
            "Простое упоминание термина, заголовок и прежний совет не подходят. "
            "Если подходящего фрагмента нет, верни пустой массив. Вопрос: " + question + " " + action +
            "\n" + json.dumps(options, ensure_ascii=False),
            schema(items={"type": "array", "items": item, "maxItems": limit}), trace,
        )
        pages = {hit["fragment_id"]: (hit.get("manual_id") or hit.get("manual") or hit["fragment_id"], hit.get("pdf_page"))
                 for hit in hits}
        seen = set()
        for selected in answer["items"][:limit]:
            key, field = selected["source"], selected["field"]
            if key not in options or field not in fields:
                continue
            fragment_id = key.rsplit(":", 1)[0]
            text = options[key]
            passage = (pages[fragment_id], " ".join(text.split()))
            if passage not in seen:
                self.cite(reply, field, text, fragment_id)
                seen.add(passage)
