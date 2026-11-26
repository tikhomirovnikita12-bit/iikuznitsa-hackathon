import argparse
import json
import os
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener

import uvicorn

from mechanic.config import DEFAULT_ML, DEFAULT_TOIR, MOCKS, endpoint


opener = build_opener(ProxyHandler({}))


def running(url: str, name: str) -> bool:
    try:
        with opener.open(f"{url}/health", timeout=0.30) as result:
            return json.load(result).get("service") == name
    except (OSError, ValueError):
        return False


def start_mock(processes, module, url, name):
    if running(url, name):
        return
    if not (MOCKS / module.split(".", 1)[0] / "app.py").is_file():
        raise SystemExit("Подготовьте заглушки командой python -m mechanic.prepare с архивом заказчика. См. README.")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
         "--port", url.rsplit(":", 1)[1], "--no-access-log"],
        cwd=MOCKS,
    )
    processes.append(process)
    deadline = monotonic() + 12
    while monotonic() < deadline and process.poll() is None:
        if running(url, name):
            return
        sleep(0.15)
    raise RuntimeError(f"Не запустился {name} на {url}. Проверьте зависимости и порт.")


def main():
    parser = argparse.ArgumentParser(description="Локальный стенд «Цифровой механик»")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-mocks", action="store_true", help="Использовать уже запущенные сервисы")
    parser.add_argument("--agent", action="store_true", help="Подключить агента к общему экрану через Ollama")
    args = parser.parse_args()
    if args.agent:
        os.environ["CASE_AGENT"] = "1"
    processes = []
    configs = (
        ("MOCK_ML_URL", DEFAULT_ML, "mock_ml.app:app", "mock-ml"),
        ("MOCK_TOIR_URL", DEFAULT_TOIR, "mock_toir.app:app", "mock-toir"),
    )
    try:
        if not args.no_mocks and not endpoint("TEAM_API_URL"):
            for variable, default, module, name in configs:
                if endpoint(variable, default) == default:
                    start_mock(processes, module, default, name)
        uvicorn.run("mechanic.main:app", host=args.host, port=args.port, access_log=False)
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
