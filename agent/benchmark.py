"""Run from the repository root with python -m agent.benchmark."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from .backend import Backend
from .dialog import Agent
from .tools.manual import Manuals


def main():
    reports = []
    with TemporaryDirectory() as folder:
        log = Path(folder)/"turns.jsonl"
        agent = Agent(Backend(), Manuals(), log=log)
        for questions in [["Что проверить сначала?"]] * 3 + [
                ["Покажи диагностику", "Что проверить сначала?", "Создай заявку"]]:
            history = []
            turns = []
            for q in questions:
                reply = agent.turn("E549YT198", "S-0011", q, history)
                history.append([q, reply["verdict"]])
                row = json.loads(log.read_text().splitlines()[-1])
                row.update(draft=bool(reply["ticket_draft"]), references=reply["references"])
                turns.append(row)
            calls = [c for t in turns for c in t["calls"]]
            reports.append(dict(turns=turns, seconds=sum(t["seconds"] for t in turns),
                                **{k:sum(c[k] for c in calls) for k in ("input_tokens","output_tokens")}))
    print(json.dumps(reports, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
