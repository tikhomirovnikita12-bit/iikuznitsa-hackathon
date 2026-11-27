"""Measure two fresh cases on run.py --agent with the competition mock services."""
import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

import httpx


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def token_sum(calls, key):
    return sum(c[key] for c in calls) if all(c.get(key) is not None for c in calls) else None


def run(client, directory, scenario, vehicle, session, questions, confirm=False):
    pair = dict(vehicle_id=vehicle, session_id=session)
    initial = client.get("/api/state", params=pair)
    initial.raise_for_status()
    state = initial.json()
    if state["messages"] or state["draft"] or state["calls"]:
        raise ValueError(f"Для замера нужна чистая тестовая сессия {session}.")
    rows, details = [], []
    started = now()
    for path, purpose in [("diagnose", "Диагностика"), *(("chat", q) for q in questions),
                          *([("chat", "Создай заявку"), ("ticket", "Подтверждение")] if confirm else [])]:
        payload = dict(pair)
        if path == "chat":
            payload["message"] = purpose
        if path == "ticket":
            draft = state["draft"]
            if not draft or not draft["operations"] or not draft["references"]:
                raise ValueError("Подтверждать можно только черновик с операциями и источниками.")
            payload.update(draft_id=state["draft_id"], revision=state["draft_revision"], priority_confirmed=True,
                           edits={k: draft[k] for k in ("conclusion", "operations", "spare_parts", "urgency")})
        old = state["calls"]
        begin, clock = now(), perf_counter()
        response = client.post("/api/" + path, json=payload)
        elapsed, end = round((perf_counter()-clock)*1000, 2), now()
        response.raise_for_status()
        state = response.json()
        if state["calls"][:len(old)] != old:
            raise ValueError("Журнал изменён параллельно или усечён.")
        calls = state["calls"][len(old):]
        llm = [c for c in calls if c["tool"] == "ollama.chat"]
        local = [c for c in calls if c["tool"] in ("agent.search", "manuals.search")]
        answer = state["messages"][-1] if state["messages"] else {}
        result = state["ticket"]["ticket_id"] or ("черновик" if state["draft"] else
                 answer.get("summary") or answer.get("text") or state["diagnosis"]["status"])
        if path == "ticket" and not state["ticket"]["ticket_id"]:
            raise ValueError("ТОиР не подтвердил создание заявки.")
        rows.append(dict(scenario=scenario, vehicle=vehicle, session=session, step=purpose, start=begin, end=end,
                         model=", ".join(dict.fromkeys(c["model"] for c in llm)) or "—",
                         llm_calls=len(llm), further_calls=max(0,len(llm)-1),
                         failed_calls=sum(c["status"] != "ok" for c in llm),
                         input_tokens=token_sum(llm,"input_tokens"), output_tokens=token_sum(llm,"output_tokens"),
                         local_searches=len(local), elapsed_ms=elapsed, result=result))
        details.append(dict(step=purpose, calls=calls, response=answer))
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{scenario}.csv").open("w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    all_llm = [c for detail in details for c in detail["calls"] if c["tool"] == "ollama.chat"]
    report = dict(start=started, end=end, mode="hybrid", indexing="Готовый локальный индекс; в сценарии не строился.",
                  elapsed_steps_ms=round(sum(row["elapsed_ms"] for row in rows),2), llm_calls=len(all_llm),
                  input_tokens=token_sum(all_llm,"input_tokens"), output_tokens=token_sum(all_llm,"output_tokens"),
                  steps=details)
    (directory / f"{scenario}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--confirm-mock-ticket", action="store_true", required=True,
                        help="Подтвердить одну заявку на изолированном тестовом ТОиР.")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, trust_env=False, timeout=240) as client:
        if client.get("/api/catalog").json()["mode"] != "hybrid":
            raise ValueError("Запустите стенд с --agent.")
        run(client, args.output, "dialog", "E549YT198", "S-0011",
            ["Какие тепловые зазоры клапанного механизма?", "Как это проверить?", "Можно ли своим ходом перегнать машину?"])
        run(client, args.output, "ticket", "M333YK47", "S-0005",
            ["Какая затяжка крышек коренных опор?", "Как это проверить?"], True)
