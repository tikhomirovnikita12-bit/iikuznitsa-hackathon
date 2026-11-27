"""Repeat the full regression suite during a long support session.

Failures are logged but do not stop the loop: the final summary shows whether a
later iteration recovered. Useful when code or documentation keeps changing.
"""

import argparse
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/Scripts/python.exe"
if not PYTHON.exists():
    PYTHON = Path(sys.executable)


def stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=float, default=180)
    parser.add_argument("--interval", type=float, default=15, help="minutes between starts")
    parser.add_argument("--log", type=Path, default=ROOT / "artifacts/marathon-monitor.log")
    args = parser.parse_args()

    args.log.parent.mkdir(parents=True, exist_ok=True)
    deadline = datetime.now() + timedelta(minutes=args.minutes)
    results: list[tuple[str, bool]] = []

    with args.log.open("a", encoding="utf-8") as log:
        log.write(f"\n[{stamp()}] monitor started for {args.minutes:g} minutes\n")
        log.flush()
        iteration = 0
        while datetime.now() < deadline:
            iteration += 1
            command = [str(PYTHON), str(ROOT / "scripts/selftest.py"), "--port", "0"]
            started = stamp()
            result = subprocess.run(command, cwd=ROOT, check=False)
            passed = result.returncode == 0
            results.append((started, passed))
            line = f"[{started}] iteration {iteration}: {'PASS' if passed else 'FAIL'}"
            print(line, flush=True)
            log.write(line + "\n")
            log.flush()
            if datetime.now() + timedelta(minutes=args.interval) >= deadline:
                break
            time.sleep(args.interval * 60)

        summary = f"[{stamp()}] summary: {sum(ok for _, ok in results)}/{len(results)} passed"
        print(summary, flush=True)
        log.write(summary + "\n")


if __name__ == "__main__":
    main()
