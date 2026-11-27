"""Run from the repository root with python -m agent.benchmark."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from mechanic.config import CORPUS
from .backend import Backend
from .dialog import Agent, history_entry
from .tools.manual import Manuals


def main():
    reports = []
    with TemporaryDirectory() as folder:
        log = Path(folder)/"turns.jsonl"
        agent = Agent(Backend(), Manuals(CORPUS), log=log)
        for questions in [["Что проверить сначала?"]] * 3 + [
                ["Покажи диагностику", "Что проверить сначала?", "Создай заявку"]]:
            history = []
            turns = []
            for q in questions:
                reply = agent.turn("E549YT198", "S-0011", q, history)
                if entry := history_entry(q, reply):
                    history.append(entry)
                row = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
                if q == "Что проверить сначала?" and not (reply["checks"] and reply["references"]):
                    raise RuntimeError("Основной вопрос не вернул подтверждённую проверку")
                row.update(draft=bool(reply["ticket_draft"]), references=reply["references"])
                if q == "Создай заявку" and not reply["ticket_draft"]:
                    raise RuntimeError("Полный проход не сформировал черновик")
                turns.append(row)
            calls = [c for t in turns for c in t["calls"]]
            totals = {}
            for key in ("input_tokens", "output_tokens"):
                counts = [call.get(key) for call in calls]
                totals[key] = sum(counts) if None not in counts else None
            reports.append(dict(turns=turns, seconds=sum(t["seconds"] for t in turns), **totals))
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
