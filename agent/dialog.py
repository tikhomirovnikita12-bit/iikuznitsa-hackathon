"""Case-scoped dialogue, structured Ollama output and per-turn accounting."""
import json
import os
import time
from pathlib import Path
from uuid import uuid4

from .backend import post
from .tools.diagnosis import diagnose
from .tools.ticket import prepare


def schema(**fields):
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


STRING = {"type": "string"}
PLAN = schema(action={"type": "string", "enum": ["diagnosis", "search", "ticket", "movement", "clarify"]},
              query=STRING, question=STRING)


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
            context = json.dumps([ml, history, question], ensure_ascii=False)
            plan = self.provider.ask("Выбери действие по ПОСЛЕДНЕМУ вопросу механика. "
                "ticket — просят оформить заявку; movement — можно ли ехать или перегнать; "
                "diagnosis — показать результат; search — технический вопрос, включая что проверить сначала; "
                "clarify — неясен узел и контекст его не определяет. query — короткий русский поисковый запрос "
                "по узлу и признакам; question — уточняющий вопрос. Контекст, история и вопрос: " + context, PLAN, trace)
            action = plan["action"]
            # Exact UI commands are stable even when a small model misreads the history.
            if question.strip(" .!?").casefold() in {"создай заявку", "создать заявку"}:
                action = "ticket"
            generic_check = any(phrase in question.casefold() for phrase in
                                ("что проверить", "с чего начать", "что посмотреть", "что делать дальше"))
            if ml["status"] == "normal" and generic_check and len(question.split()) <= 5:
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
            if ml["anomaly_type"] == "F02":
                if action == "movement" or "кпа" in question.lower():
                    query = "При каких условиях проверять давление масла?"
                    reply["clarifying_question"] = ("Укажите фактическое давление, обороты, прогрев "
                        "и применимость допуска после длительной эксплуатации.")
                elif action in {"ticket", "diagnosis"} or "сначала" in question.lower():
                    query = "Какие причины пониженного давления указаны в руководстве?"

            if action == "movement":
                reply["verdict"] = "Возможность перегона по этой записи не подтверждена."
                reply["clarifying_question"] = reply["clarifying_question"] or "Какие показания приборов получены и при каком режиме двигателя?"
            for attempt in range(2):
                hits = self.manuals.find(query, ml)
                trace["tools"].append(dict(name="search", query=query,
                    pages=[(h["manual"], h["pdf_page"]) for h in hits]))
                if hits:
                    self.select(reply, action, question, hits, trace)
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

    def select(self, reply, action, question, hits, trace):
        # The provider selects original passages. Only server-owned text reaches the UI.
        options = {}
        for hit in hits:
            lines = hit["text"].splitlines() if hit.get("verified") else [hit["text"]]
            for i, line in enumerate(lines[:10]):
                if line.strip():
                    options[f"{hit['fragment_id']}:{i}"] = line
        item = schema(source={"type":"string", "enum":list(options)},
                      field={"type":"string","enum":["checks","risk","possible_causes"]})
        answer = self.provider.ask("Выбери до трёх применимых фрагментов. Для 'сначала' или заявки "
            "выбери начальную проверку в checks. Для условий измерения выбери полный норматив. "
            "Если данные нерелевантны, верни пустой массив. Вопрос: " + question + " " + action +
            "\n" + json.dumps({k:v[:800] for k,v in options.items()}, ensure_ascii=False),
            schema(items={"type":"array","items":item,"maxItems":3}), trace)
        for selected in answer["items"][:3]:
            key = selected["source"]
            if key not in options:
                continue
            fid = key.rsplit(":", 1)[0]
            ref = self.manuals.reference(fid, reply["ml_observations"]["engine_model"])
            if ref not in reply["references"]:
                reply["references"].append(ref)
            reply[selected["field"]].append(dict(text=options[key], fragment_id=fid))
