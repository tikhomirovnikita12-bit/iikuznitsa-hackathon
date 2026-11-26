"""Case-scoped dialogue, structured Ollama output and per-turn accounting."""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from .backend import post
from .tools.diagnosis import diagnose
from .tools import oil_pressure
from .tools.ticket import prepare


def schema(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


STRING = {"type": "string"}
PLAN = schema(action={"type": "string", "enum": ["diagnosis", "search", "ticket", "movement", "clarify"]},
              query=STRING, question=STRING)


def passages(hit):
    """Keep complete source passages; skip damaged tables, captions and clipped text."""
    raw = hit["text"]
    if re.search(r"[\ufffd\ue000-\uf8ff\u2500-\u259f]|вероятная причина\s+способ устранения", raw, re.I):
        return []
    if re.match(r"\s*(?:рис\.|таблица\s+\d)", raw, re.I):
        return []
    if hit.get("verified"):
        return [line for line in raw.splitlines() if 12 <= len(line) <= 600]
    clean = re.sub(r"(?<=[а-яё])-\s*\n\s*(?=[а-яё])", "", raw)
    clean = " ".join(clean.split())
    if len(clean) > 700 or re.search(r"[а-яё]- [а-яё]|\d+\s*[–—]\s*\w+.*?;\s*\d+\s*[–—]", clean):
        return []
    if not clean.endswith((".", "!", "?")):
        return []
    # Keep paragraphs with numeric limits intact so their exceptions stay attached.
    if any(ch.isdigit() for ch in clean):
        return [clean] if len(clean) <= 500 else []
    return [sent for sent in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", clean) if 16 <= len(sent) <= 350]


class Ollama:
    def ask(self, prompt, format_, trace):
        model = os.getenv("AGENT_MODEL", "gemma2:2b")
        start = time.monotonic()
        result = post(os.getenv("OLLAMA_HOST_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/chat",
                      dict(model=model, stream=False, format=format_, options={"temperature": 0},
                           messages=[dict(role="user", content=prompt)]), timeout=180)
        trace["calls"].append(dict(model=result["model"], seconds=time.monotonic()-start,
                                   input_tokens=result.get("prompt_eval_count"),
                                   output_tokens=result.get("eval_count")))
        return json.loads(result["message"]["content"])


class Agent:
    def __init__(self, backend, manuals, provider=None, log="artifacts/agent-turns.jsonl"):
        self.backend = backend
        self.manuals = manuals
        self.provider = provider or Ollama()
        self.log = Path(log)

    def turn(self, vehicle_id, session_id, question, history=()):
        start = time.monotonic()
        trace = dict(id=uuid4().hex, vehicle_id=vehicle_id, session_id=session_id,
                     question=question, tools=[], calls=[])
        try:
            trace["tools"].append({"name": "diagnose"})
            reply = diagnose(self.backend, vehicle_id, session_id)
            ml = reply["ml_observations"]
            if ml["status"] == "check_failed":
                return reply
            if (ml["anomaly_type"] == "F08" and "диз" not in ml["fuel"].lower()
                    or ml["anomaly_type"] in {"F12", "F13"} and "диз" in ml["fuel"].lower()):
                reply["clarifying_question"] = (f"ML указала «{ml['anomaly_title']}», а в карточке "
                    f"{ml['engine_model']} ({ml['fuel']}). Подтвердите двигатель и выбранную запись.")
                return reply
            pressure_case = ml["engine_model"] == "ЯМЗ-236М2" and ml["anomaly_type"] == "F02"
            previous_pressure = history[-1][4] if history and len(history[-1]) > 4 else None
            movement = bool(re.search(r"перегна|перегон|своим ходом", question, re.I))
            values = oil_pressure.readings(question, previous_pressure or {}) if pressure_case else {}
            if pressure_case and (movement or oil_pressure.PRESSURE.search(question)
                                  or (previous_pressure is not None and values != previous_pressure)):
                return self.pressure_reply(reply, values, trace)
            context = json.dumps([ml, [row[:4] for row in history], question], ensure_ascii=False)
            plan = self.provider.ask("Выбери действие по ПОСЛЕДНЕМУ вопросу механика. "
                "ticket — просят оформить заявку; movement — можно ли ехать или перегнать; "
                "diagnosis — показать результат; search — технический вопрос, включая что проверить сначала; "
                "clarify — неясен узел и контекст его не определяет. query — короткий русский поисковый запрос "
                "по узлу и признакам; question — уточняющий вопрос. Контекст, история и вопрос: " + context, PLAN, trace)
            action = plan["action"]
            if pressure_case and action == "movement":
                return self.pressure_reply(reply, values, trace)
            # Exact UI commands are stable even when a small model misreads the history.
            if re.fullmatch(r"(?:создай|создать|подготовь|оформи) заявку(?: на дефектовку)?",
                            question.strip(" .!?").casefold()):
                action = "ticket"
            wording = question.casefold().strip(" .!?")
            next_step = wording in {"а что мне делать", "что мне делать", "че сделать надо",
                                    "что делать", "что мне сделать", "что сделать", "что нужно сделать", "скажи как делать", "что проверить сначала",
                                    "а что проверить сначала", "что делать дальше", "с чего начать"}
            followup = bool(re.fullmatch(
                r"(?:а )?(?:подскажи |скажи )?как (?:именно |это |мне )*(?:сделать|проверить|делать|выполнить)",
                wording))
            previous = next((row[3][0] for row in reversed(history) if len(row) > 3 and row[3]), None)
            if followup and not previous:
                reply.update(intent="clarify", clarifying_question="Какую проверку нужно объяснить?")
                return reply
            if next_step or followup:
                action = "search"
            if wording in {"покажи результат диагностики", "покажи диагностику"}:
                action = "diagnosis"
            reply["intent"] = action
            generic_check = any(phrase in question.casefold() for phrase in
                                ("что проверить", "с чего начать", "что посмотреть", "что делать дальше"))
            if ml["status"] == "normal" and (generic_check or next_step) and len(question.split()) <= 5:
                reply["clarifying_question"] = "По этой записи отклонений нет. Какой узел или симптом вас интересует?"
                return reply
            if action == "clarify":
                reply["clarifying_question"] = plan["question"] or "Какой узел и режим работы вас интересуют?"
                return reply
            if action == "diagnosis" and ml["status"] == "normal":
                return reply
            if action == "ticket" and ml["status"] != "anomaly":
                reply["clarifying_question"] = "Для заявки нужна проверка с обнаруженным отклонением."
                return reply
            query = plan["query"] or question
            # Preserve the mechanic's terminology for specific manual questions.
            if action == "search" and len(question.split()) >= 5:
                query = question
            first_coolant_check = ml["anomaly_type"] == "F03" and (next_step or action == "diagnosis")
            if first_coolant_check:
                query = "Проверять уровень охлаждающей жидкости в расширительном бачке"
            if ml["anomaly_type"] == "F02":
                if action == "movement" or "кпа" in question.lower():
                    query = "При каких условиях проверять давление масла?"
                    reply["clarifying_question"] = ("Укажите фактическое давление, обороты, прогрев "
                        "и применимость допуска после длительной эксплуатации.")
                elif action in {"ticket", "diagnosis"} or "сначала" in question.lower():
                    query = "Какие причины пониженного давления указаны в руководстве?"

            if followup:
                first_coolant_check = False
                query = previous["text"]
                if "уровень охлаждающей жидкости" in query.lower():
                    query = "Уровень жидкости в расширительном бачке должен соответствовать указанному в руководстве по эксплуатации автомобиля"
            if action == "movement":
                reply["verdict"] = "Возможность перегона по этой записи не подтверждена."
                reply["clarifying_question"] = reply["clarifying_question"] or "Какие показания приборов получены и при каком режиме двигателя?"
            for attempt in range(2):
                hits = self.manuals.find(query, ml)
                trace["tools"].append(dict(name="search", query=query,
                    pages=[(h["manual"], h["pdf_page"]) for h in hits]))
                if hits:
                    self.select(reply, action, question + " " + previous["text"] if followup else question, hits, trace, first_coolant_check, next_step, previous if followup else None)
                if reply["references"]:
                    break
                if attempt == 0:
                    query = self.provider.ask("Переформулируй по-русски запрос для поиска по руководству. "
                        "Назови конкретный узел и условия. Предыдущий запрос: " + query + context,
                        schema(query=STRING), trace)["query"]
            if not reply["references"]:
                reply["clarifying_question"] = reply["clarifying_question"] or (
                    "Применимого фрагмента не найдено. Уточните узел и режим работы для дефектовки.")
            if action == "ticket":
                trace["tools"].append(dict(name="prepare_ticket"))
                reply["ticket_draft"] = prepare(reply)
                if not reply["ticket_draft"]:
                    reply["clarifying_question"] = "Для заявки нужна подтверждённая руководством проверка. Уточните узел."
            return reply
        except Exception as exc:
            trace["error"] = type(exc).__name__
            raise
        finally:
            trace["seconds"] = time.monotonic() - start
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as stream:
                stream.write(json.dumps(trace, ensure_ascii=False) + "\n")

    def pressure_reply(self, reply, values, trace):
        ml = reply["ml_observations"]
        hits = self.manuals.find(oil_pressure.QUERY, ml)
        trace["tools"].append(dict(name="search", query=oil_pressure.QUERY,
            pages=[(hit["manual"], hit["pdf_page"]) for hit in hits]))
        reply.update(intent="movement", pressure_measurement=values,
                     verdict="Возможность перегона по этим данным не подтверждена.")
        source = next((hit for hit in hits if hit["fragment_id"] == oil_pressure.SOURCE), None)
        if source is None:
            reply["clarifying_question"] = "Не удалось найти применимый норматив в руководстве."
            return reply
        ref = self.manuals.reference(source["fragment_id"], ml["engine_model"])
        reply["references"].append(ref)
        result, reply["clarifying_question"] = oil_pressure.compare(values)
        reply["checks"].append(dict(text=result or source["text"], fragment_id=ref["fragment_id"]))
        return reply

    def select(self, reply, action, question, hits, trace, coolant=False, next_step=False, previous=None):
        # The provider selects original passages. Only server-owned text reaches the UI.
        options = {}
        for hit in hits:
            for i, line in enumerate(passages(hit)[:10]):
                if previous and (line == previous["text"] or hit["fragment_id"] == previous["fragment_id"]):
                    continue
                if previous and "уровень охлаждающей жидкости" in previous["text"].lower():
                    if "уровень жидкости в расширительном бачке" not in line.lower():
                        continue
                if coolant and not all(term in line.lower() for term in ("уров", "охлаждающ", "холодн")):
                    continue
                options[f"{hit['fragment_id']}:{i}"] = line
        if not options:
            return
        if coolant or (previous and "уровень охлаждающей жидкости" in previous["text"].lower() and len(options) == 1):
            key, passage = next(iter(options.items()))
            fid = key.rsplit(":", 1)[0]
            if previous and "уровень охлаждающей жидкости" in previous["text"].lower():
                passage = re.sub(r"^\d+\.\s*", "", passage)
            reply["checks"].append(dict(text=passage, fragment_id=fid))
            reply["references"].append(self.manuals.reference(fid, reply["ml_observations"]["engine_model"]))
            return
        fields = ["checks"] if previous or next_step or action == "ticket" else ["checks", "risk", "possible_causes"]
        limit = 1 if previous or next_step or action == "diagnosis" else 2
        item = schema(source={"type":"string", "enum":list(options)},
                      field={"type":"string","enum":fields})
        answer = self.provider.ask("Выбери только прямой ответ на вопрос. Для 'сначала' или заявки "
            "выбери начальную проверку в checks. Для условий измерения выбери полный норматив. "
            "Если данные нерелевантны, верни пустой массив. Вопрос: " + question + " " + action +
            "\n" + json.dumps({k:v[:800] for k,v in options.items()}, ensure_ascii=False),
            schema(items={"type":"array","items":item,"maxItems":limit}), trace)
        for selected in answer["items"][:limit]:
            key = selected["source"]
            if key not in options:
                continue
            fid = key.rsplit(":", 1)[0]
            ref = self.manuals.reference(fid, reply["ml_observations"]["engine_model"])
            if ref not in reply["references"]:
                reply["references"].append(ref)
            reply[selected["field"]].append(dict(text=options[key], fragment_id=fid))
