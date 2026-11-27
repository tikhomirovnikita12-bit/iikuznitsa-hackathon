"""Run the full local regression suite for the Digital Mechanic stand."""

import argparse
import json
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv/Scripts/python.exe"
PYTHON = VENV_PYTHON if VENV_PYTHON.exists() else Path(sys.executable)
OPENER = build_opener(ProxyHandler({}))
MOCK_SPECS = (
    ("MOCK_ML_URL", "http://127.0.0.1:8001", "mock_ml.app:app", "mock-ml"),
    ("MOCK_TOIR_URL", "http://127.0.0.1:8002", "mock_toir.app:app", "mock-toir"),
)


def endpoint(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().rstrip("/")


def service_running(url: str, name: str) -> bool:
    try:
        with OPENER.open(f"{url}/health", timeout=0.3) as response:
            return json.load(response).get("service") == name
    except (OSError, ValueError):
        return False


@contextmanager
def mocks():
    """Поднять заглушки для браузерной проверки, если они уже не работают."""
    processes = []
    runtime = ROOT / ".runtime"
    runtime.mkdir(exist_ok=True)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1"}
    try:
        if not endpoint("TEAM_API_URL"):
            for variable, default, module, name in MOCK_SPECS:
                url = endpoint(variable, default)
                if url != default or service_running(url, name):
                    continue
                log_path = runtime / f"selftest-{name}.log"
                with log_path.open("w", encoding="utf-8") as log:
                    process = subprocess.Popen(
                        [str(PYTHON), "-m", "uvicorn", module, "--host", "127.0.0.1",
                         "--port", url.rsplit(":", 1)[1], "--no-access-log"],
                        cwd=ROOT / "artifacts/customer", env=env, stdout=log, stderr=log,
                    )
                processes.append(process)
                deadline = monotonic() + 15
                while monotonic() < deadline and process.poll() is None:
                    if service_running(url, name):
                        break
                    sleep(0.15)
                else:
                    raise RuntimeError(f"Не запустился {name}: {log_path.read_text(encoding='utf-8')}")
        yield
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()


def run(name: str, command: list[str], env: dict[str, str] | None = None) -> None:
    print(f"[selftest] {name}: {' '.join(command)}", flush=True)
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    if result.returncode:
        raise SystemExit(f"[selftest] {name} failed with code {result.returncode}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-browser", action="store_true", help="skip Playwright checks")
    parser.add_argument("--skip-search", "--skip-video", dest="skip_search", action="store_true",
                        help="skip the manual-search corpus check")
    parser.add_argument("--port", type=int, default=0, help="port for the browser stand")
    args = parser.parse_args()

    run("python tests", [str(PYTHON), "-m", "pytest", "tests", "-q"])
    run("javascript syntax", ["node", "--check", str(ROOT / "web/app.js")])

    if not args.skip_search:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        run("manual search", [str(PYTHON), "tests/check_manual_search.py",
                              str(ROOT / "artifacts/manual-corpus.json")], env)

    if not args.skip_browser:
        with mocks():
            run("browser", [str(PYTHON), "tests/browser_check.py", "--expect-team",
                            "--port", str(args.port)])

    print("[selftest] all checks passed", flush=True)


if __name__ == "__main__":
    main()
