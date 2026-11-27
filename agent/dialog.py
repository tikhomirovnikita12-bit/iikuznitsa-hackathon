"""Case-scoped dialogue, structured Ollama output and per-turn accounting."""
import json
import os
import re
import time
from pathlib import Path
from uuid import uuid4

from .backend import post
from .tools.diagnosis import describe, diagnose
from .tools import fluid_check, oil_pressure
from .tools.ticket import prepare


def schema(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


STRING = {"type": "string"}
TERM = re.compile(r"(?:а )?(?:что такое|что означает|что значит) ([а-яёa-z][а-яёa-z -]{1,55})")
PLAN = schema(action={"type": "string", "enum": ["diagnosis", "search", "ticket", "movement", "clarify", "off_topic", "next_step", "pause", "repair"]},
              query=STRING, question=STRING)
OIL_NORM_QUESTION = re.compile(
    r"^(?:а\s+)?(?:какое\s+(?:должно\s+быть\s+)?давление\s+масла|"
    r"каким\s+должно\s+быть\s+давление\s+масла|какова\s+норма\s+давления\s+масла|"
    r"норма\s+давления\s+масла)\b"
)


def ticket_query(diagnosis):
    if diagnosis["anomaly_type"] == "F02":
        return "Какие причины пониженного давления указаны в руководстве?"
    return diagnosis["anomaly_title"] or ""


def incompatible_fault(diagnosis):
    diesel = "диз" in diagnosis["fuel"].lower()
    return (diagnosis["anomaly_type"] == "F08" and not diesel
            or diagnosis["anomaly_type"] in {"F12", "F13"} and diesel)


def passages(hit):
    """Keep complete source passages; skip damaged tables, captions and clipped text."""
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
    # Keep paragraphs with numeric limits intact so their exceptions stay attached.
    if any(ch.isdigit() for ch in clean):
        return [clean] if len(clean) <= 500 else []
    return [sent for sent in re.split(r"(?<=[.!?])\s+(?=[А-ЯЁ])", clean) if 16 <= len(sent) <= 350]


def history_entry(question, reply):
    if reply.get("intent") != "conversation":
        return [question, reply["verdict"], reply["clarifying_question"], reply["checks"][:1],
                reply.get("pressure_measurement"), reply.get("fluid_check")]
    return None


class Ollama:
    def ask(self, prompt, format_, trace):
        model = os.getenv("AGENT_MODEL") or os.getenv("OLLAMA_MODEL") or "gemma2:2b"
        start = time.monotonic()
        result = post((os.getenv("OLLAMA_HOST_URL") or os.getenv("OLLAMA_URL") or "http://127.0.0.1:11434").rstrip("/") + "/api/chat",
                      dict(model=model, stream=False, format=format_, options={"temperature": 0},
                           messages=[dict(role="user", content=prompt)]), timeout=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "180")))
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

    def turn(self, vehicle_id, session_id, question, history=(), *, diagnosis=None, trace_out=None):
        start = time.monotonic()
        trace = dict(id=uuid4().hex, vehicle_id=vehicle_id, session_id=session_id,
                     question=question, tools=[], calls=[])
        try:
            tool_start = time.monotonic()
            reply = describe(diagnosis) if diagnosis is not None else diagnose(self.backend, vehicle_id, session_id)
            trace["tools"].append(dict(name="diagnose", source="session" if diagnosis is not None else "backend",
                                       seconds=time.monotonic() - tool_start))
            ml = reply["ml_observations"]
            if (ml["vehicle_id"], ml["session_id"]) != (vehicle_id, session_id):
                raise ValueError("Ответ диагностики относится к другой машине или сессии")
            if ml["status"] == "check_failed":
                return reply
            if incompatible_fault(ml):
                reply["clarifying_question"] = (f"ML указала «{ml['anomaly_title']}», а в карточке "
                    f"{ml['engine_model']} ({ml['fuel']}). Сверьте обозначение двигателя и выбранную запись.")
                return reply
            wording = question.casefold().strip(" .!?")
            if wording in {"привет", "здравствуйте", "добрый день", "спасибо", "понятно"}:
                message = ("Пожалуйста." if wording in {"спасибо", "понятно"}
                           else "Привет! Могу пояснить результат диагностики или поискать ответ в руководстве.")
                reply.update(intent="conversation", explanation=message)
                return reply
            short_request = re.sub(r"^(?:(?:ну|так|а|ладно|лад|окей|ок|давай)[ ,]+)+", "", wording)
            short_request = re.sub(r"\bч[её]\b", "что", short_request)
            previous = next((row[3][0] for row in reversed(history) if len(row) > 3 and row[3]), None)
            if ml["engine_model"] == "ЯМЗ-236М2" and ml["anomaly_type"] == "F02":
                fluids = next((row[5] for row in reversed(history) if len(row) > 5 and row[5] is not None), None)
                if fluids is not None and "?" not in question:
                    updated = fluid_check.report(short_request, fluids)
                    if updated is not None:
                        return self.fluid_reply(reply, updated, trace)
            else:
                fluids = None
            if fluids is not None and re.fullmatch(r"(?:а )?тебя (?:спросить|и спрашиваю)(?: надо)?", short_request):
                return self.fluid_reply(reply, fluids, trace)
            if short_request == "достаточно" and previous and "достаточно масла" in previous["text"]:
                reply.update(intent="clarify", clarifying_question=
                             "Вы про уровни масла и охлаждающей жидкости или на этом остановимся?")
                return reply
            if short_request in {"достаточно", "хватит", "стоп", "на этом всё", "на этом все"}:
                reply.update(intent="conversation", explanation="Хорошо, остановимся.")
                return reply
            if re.fullmatch(r"ты (?:что )?(?:охуел|ахуел|охренел|издеваешься)|ответь нормально", short_request):
                reply.update(intent="conversation", explanation=
                             "Предыдущий ответ запутал диалог. Можем вернуться к открытой проверке.")
                return reply
            pressure_case = ml["engine_model"] == "ЯМЗ-236М2" and ml["anomaly_type"] == "F02"
            previous_pressure = next((row[4] for row in reversed(history) if len(row) > 4 and row[4] is not None), None)
            movement = bool(re.search(r"перегна|перегон|своим ходом", question, re.I))
            values = oil_pressure.readings(question, previous_pressure) if pressure_case else {}
            if pressure_case and (movement or oil_pressure.PRESSURE.search(question)
                                  or (previous_pressure is not None and
                                      (values != previous_pressure or oil_pressure.BARE_NUMBER.fullmatch(question.strip())))):
                return self.pressure_reply(reply, values, trace)
            if OIL_NORM_QUESTION.match(wording):
                hits = self.search(question, ml, trace)
                source = next((hit for hit in hits if hit.get("verified")
                               and hit["fragment_id"].endswith("-oil-pressure")), None)
                if source:
                    sources = [source]
                    for fragment_id in source.get("supporting_fragments", ()):
                        related = self.manuals.searcher.fragments.get(fragment_id)
                        if related and ml["engine_model"] in related["applicable_engine_models"]:
                            sources.append(related)
                    reply.update(
                        intent="search",
                        checks=[dict(text=item["text"], fragment_id=item["fragment_id"]) for item in sources],
                        references=[self.manuals.reference(item["fragment_id"], ml["engine_model"])
                                    for item in sources],
                    )
                    return reply
            context = json.dumps([ml, [row[:4] for row in history], question], ensure_ascii=False)
            next_step = bool(re.fullmatch(
                r"(?:(?:(?:сам )?(?:мне )?(?:скажи|подскажи|расскажи) )?"
                r"что (?:(?:мне(?:-то)?|надо|нужно|теперь|дальше) )*"
                r"(?:делать|сделать|проверить|посмотреть)(?: (?:надо|нужно|дальше|сначала))?"
                r"|скажи как делать|с чего начать|(?:подскажи|посоветуй)(?: мне)?|что дальше)", short_request))
            followup = bool(re.fullmatch(
                r"(?:подскажи |скажи )?как (?:именно |это |мне )*(?:сделать|проверить|делать|выполнить)",
                short_request))
            diagnosis_request = bool(re.fullmatch(
                r"покажи (?:результат диагностики|диагностику)|что (?:случилось|произошло|не так)(?: с машиной)?",
                short_request))
            ticket_request = bool(re.fullmatch(
                r"(?:создай|создать|подготовь|оформи) заявку(?: на дефектовку)?", short_request))
            if term := TERM.fullmatch(wording):
                action = "search"
                plan = {"query": term[1], "question": ""}
            elif diagnosis_request or next_step or followup or ticket_request:
                action = "diagnosis" if diagnosis_request else "ticket" if ticket_request else "search"
                plan = {"query": ml["anomaly_title"] or "", "question": ""}
            else:
                plan = self.provider.ask(
                    "Классифицируй новую реплику в чате механика. Контекст помогает понять короткие "
                    "вопросы вроде «что дальше?», но сам не является запросом. "
                    "off_topic — личная реплика или обсуждение фильмов и знаменитостей, "
                    "даже если упоминается марка машины. "
                    "ticket — оформить заявку; movement — можно ли ехать; diagnosis — показать результат; "
                    "search — конкретный технический вопрос; next_step — просят подсказать, что делать "
                    "с открытой проверкой, даже если узел в реплике не назван; "
                    "pause — просят остановиться; repair — недовольны тем, как агент ответил; "
                    "clarify — вопрос по машине, требующий уточнения. "
                    "query — поисковая фраза; question — уточняющий вопрос. Для off_topic оба поля пустые. "
                    "Текущая реплика: " + json.dumps(question, ensure_ascii=False) +
                    "\nВспомогательный контекст: " + json.dumps([ml, [row[:4] for row in history]], ensure_ascii=False),
                    PLAN, trace)
                action = plan["action"]
            if pressure_case and action == "movement":
                return self.pressure_reply(reply, values, trace)
            if action in {"pause", "repair"}:
                message = ("Хорошо, остановимся." if action == "pause"
                           else "Предыдущий ответ запутал диалог. Можем вернуться к открытой проверке.")
                reply.update(intent="conversation", explanation=message)
                return reply
            if action == "next_step":
                next_step = True
                action = "search"
            if next_step and fluids:
                return self.fluid_reply(reply, fluids, trace)
            if followup and not previous:
                reply.update(intent="clarify", clarifying_question="Какую проверку нужно объяснить?")
                return reply
            reply["intent"] = action
            generic_check = any(phrase in question.casefold() for phrase in
                                ("что проверить", "с чего начать", "что посмотреть", "что делать дальше"))
            if ml["status"] == "normal" and (generic_check or next_step) and len(question.split()) <= 5:
                reply["clarifying_question"] = "По этой записи отклонений нет. Какой узел или симптом вас интересует?"
                return reply
            if action in {"off_topic", "search", "clarify"} and not (term or next_step or followup):
                relevance = self.provider.ask(
                    "Определи цель реплики. unrelated — личная реплика, мнение, разговор о кино "
                    "или о том, кому принадлежит автомобиль, даже если названа марка. "
                    "mechanic — вопрос о проверке, ремонте, эксплуатации, описание неисправности "
                    "или краткое уточнение вроде «почему?». Если сообщение просто называет владельца "
                    "и модель его машины и не описывает проблему, это unrelated. "
                    "«У Киану Ривза Ford Mustang» → unrelated. «У Джона Траволты хонда цивик» → unrelated. «Ford Mustang стучит» → mechanic. "
                    "Реплика: " + json.dumps(question, ensure_ascii=False),
                    schema(topic={"type": "string", "enum": ["unrelated", "mechanic"]}), trace)
                # A planner can copy an earlier user question instead of asking for missing detail.
                clarification = plan["question"].casefold().strip(" .!?")
                echoed_history = action == "clarify" and (
                    clarification == wording or any(
                        clarification == row[0].casefold().strip(" .!?") for row in history
                    )
                )
                if relevance["topic"] == "unrelated" or echoed_history:
                    reply.update(intent="conversation", explanation="Могу помочь с диагностикой или показать, что говорит руководство по этой машине.")
                    return reply
                if action == "off_topic":
                    action = reply["intent"] = "search"
            if action == "clarify":
                reply["clarifying_question"] = plan["question"] or "Какой узел и режим работы вас интересуют?"
                return reply
            if action == "diagnosis" and ml["status"] == "normal":
                return reply
            if action == "ticket" and ml["status"] != "anomaly":
                reply["clarifying_question"] = "Для заявки нужна проверка с обнаруженным отклонением."
                return reply
            query = plan["query"] or question
            if action == "ticket" and (ticket_request or not plan["query"]):
                query = ticket_query(ml)
            elif action == "diagnosis":
                query = " ".join(filter(None, [ml["anomaly_title"], ml["description"]]))
            # Preserve the mechanic's terminology for specific manual questions.
            if action == "search" and len(question.split()) >= 5 and not next_step:
                query = question
            first_coolant_check = ml["anomaly_type"] == "F03" and (next_step or action == "diagnosis")
            if first_coolant_check:
                query = "Проверять уровень охлаждающей жидкости в расширительном бачке"
            if ml["anomaly_type"] == "F02":
                if action == "movement" or (not term and "кпа" in question.lower() and "масл" in question.lower()):
                    query = "При каких условиях проверять давление масла?"
                    reply["clarifying_question"] = ("Укажите фактическое давление, обороты, прогрев "
                        "и применимость допуска после длительной эксплуатации.")
                elif action in {"ticket", "diagnosis"} or next_step:
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
                hits = self.search(query, ml, trace, initial_check=action == "diagnosis" or next_step)
                if hits:
                    selection_question = "Что проверить сначала?" if next_step else question
                    if action in {"diagnosis", "ticket"}:
                        selection_question += " " + ml["engine_model"] + " " + (ml["anomaly_title"] or "") + " " + ml["description"]
                    if followup:
                        selection_question += " " + previous["text"]
                    self.select(reply, action, selection_question, hits, trace,
                                first_coolant_check, next_step, previous if followup else None)
                if reply["references"]:
                    break
                if attempt == 0:
                    query = self.provider.ask("Переформулируй по-русски запрос для поиска по руководству. "
                        "Назови конкретный узел и условия. Предыдущий запрос: " + query + context,
                        schema(query=STRING), trace)["query"]
            if not reply["references"]:
                reply["clarifying_question"] = reply["clarifying_question"] or (
                    "В руководстве этого двигателя нет подтверждения для ответа. Уточните узел и условия проверки. Если данных недостаточно, передайте машину на дефектовку.")
            if action == "ticket":
                tool_start = time.monotonic()
                reply["ticket_draft"] = prepare(reply)
                trace["tools"].append(dict(name="prepare_ticket", seconds=time.monotonic() - tool_start))
                if not reply["ticket_draft"]:
                    reply["clarifying_question"] = "Для заявки нужна подтверждённая руководством проверка. Уточните узел."
            if ml["anomaly_type"] == "F02" and any(
                item["fragment_id"] == fluid_check.SOURCE and "достаточно масла" in item["text"]
                for item in reply["checks"]
            ):
                reply["fluid_check"] = fluids if fluids is not None else {}
            return reply
        except Exception as exc:
            trace["error"] = type(exc).__name__
            raise
        finally:
            trace["seconds"] = time.monotonic() - start
            self.log.parent.mkdir(parents=True, exist_ok=True)
            if trace_out is not None:
                trace_out.update(trace)
            with self.log.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(trace, ensure_ascii=False) + "\n")

    def search(self, query, diagnosis, trace, *, initial_check=False):
        started = time.monotonic()
        hits = self.manuals.find(query, diagnosis, initial_check=initial_check)
        trace["tools"].append(dict(name="search", query=query, seconds=time.monotonic() - started,
                                   pages=[(hit["manual"], hit["pdf_page"]) for hit in hits]))
        return hits

    def fluid_reply(self, reply, state, trace):
        ml = reply["ml_observations"]
        hits = self.search(fluid_check.QUERY, ml, trace)
        reply["fluid_check"] = state
        source = next((hit for hit in hits if hit["fragment_id"] == fluid_check.SOURCE), None)
        if source is None:
            reply.update(intent="clarify", clarifying_question="Не нашёл применимый раздел о проверке уровней в руководстве.")
            return reply
        result, question = fluid_check.next_action(state)
        if question:
            reply.update(intent="clarify", clarifying_question=question)
        else:
            ref = self.manuals.reference(source["fragment_id"], ml["engine_model"])
            reply.update(intent="search", checks=[dict(text=result, fragment_id=ref["fragment_id"])],
                         references=[ref])
        return reply

    def pressure_reply(self, reply, values, trace):
        ml = reply["ml_observations"]
        hits = self.search(oil_pressure.QUERY, ml, trace)
        reply.update(intent="movement", pressure_measurement=values,
                     verdict="Возможность перегона по этим данным не подтверждена.")
        source = next((hit for hit in hits if hit["fragment_id"] == oil_pressure.SOURCE), None)
        if source is None:
            reply["clarifying_question"] = "Не удалось найти применимый норматив в руководстве."
            return reply
        ref = self.manuals.reference(source["fragment_id"], ml["engine_model"])
        reply["references"].append(ref)
        result, reply["clarifying_question"] = oil_pressure.compare(values)
        if result:
            reply["checks"].append(dict(text=result + " " + source["text"], fragment_id=ref["fragment_id"]))
        reply["pressure_reference_text"] = source["text"]
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
        limit = 1 if previous or next_step else 3 if action == "diagnosis" else 2
        item = schema(source={"type":"string", "enum":list(options)},
                      field={"type":"string","enum":fields})
        answer = self.provider.ask("Выбери только прямой ответ на вопрос. Для 'сначала' или заявки "
            "выбери начальную проверку в checks. Для условий измерения выбери полный норматив. "
            "Упоминание термина, заголовок и прежний совет не считаются ответом. Если прямого ответа нет, верни пустой массив. Вопрос: " + question + " " + action +
            "\n" + json.dumps({k:v[:800] for k,v in options.items()}, ensure_ascii=False),
            schema(items={"type":"array","items":item,"maxItems":limit}), trace)
        seen = {(entry["fragment_id"], entry["text"]) for field in ("checks", "risk", "possible_causes")
                for entry in reply[field]}
        for selected in answer["items"][:limit]:
            if selected.get("field") not in fields:
                continue
            key = selected["source"]
            if key not in options or selected["field"] not in fields:
                continue
            fid = key.rsplit(":", 1)[0]
            identity = fid, options[key]
            if identity in seen:
                continue
            seen.add(identity)
            ref = self.manuals.reference(fid, reply["ml_observations"]["engine_model"])
            if ref not in reply["references"]:
                reply["references"].append(ref)
            reply[selected["field"]].append(dict(text=options[key], fragment_id=fid))
