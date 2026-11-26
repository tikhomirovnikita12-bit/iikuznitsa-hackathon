"""Run the backend and the two customer mocks for local development."""

import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parent
CUSTOMER = ROOT / "artifacts/customer"
HOST = "127.0.0.1"
SERVICES = (
    ("mock-ml", "mock_ml.app:app", 8001, CUSTOMER),
    ("mock-toir", "mock_toir.app:app", 8002, CUSTOMER),
    ("digital-mechanic", "backend.app:app", 8000, ROOT),
)


def ensure_running(process, name, port):
    code = process.poll()
    if code is not None:
        raise RuntimeError(f"{name} на порту {port} завершился с кодом {code}. Подробности выше.")


def wait_for_health(opener, process, name, port):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ensure_running(process, name, port)
        try:
            with opener.open(f"http://{HOST}:{port}/health", timeout=0.5) as response:
                health = json.load(response)
            if isinstance(health, dict) and health.get("status") == "ok" and health.get("service") == name:
                return
        except (URLError, OSError, ValueError):
            pass
        time.sleep(0.2)
    raise RuntimeError(f"{name} на порту {port} не ответил на /health за 20 секунд.")


def stop_services(services):
    for process, _, _ in reversed(services):
        if process.poll() is None:
            process.terminate()
    for process, _, _ in reversed(services):
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def main():
    if importlib.util.find_spec("uvicorn") is None:
        print("Установите зависимости этим Python: python -m pip install -r requirements.txt",
              file=sys.stderr)
        return 1
    if not all((CUSTOMER / file).is_file() for file in ("mock_ml/app.py", "mock_toir/app.py", "data/vehicles.json")):
        print("Распакуйте ZIP заказчика в artifacts/customer. Инструкция в docs/backend.md.",
              file=sys.stderr)
        return 1

    def handle_termination(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, handle_termination)
    opener = build_opener(ProxyHandler({}))
    running = []
    try:
        for name, app, port, directory in SERVICES:
            env = None
            if name == "digital-mechanic":
                env = {**os.environ, "MOCK_ML_URL": f"http://{HOST}:8001", "MOCK_TOIR_URL": f"http://{HOST}:8002"}
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", app, "--host", HOST, "--port", str(port)],
                cwd=directory, env=env, start_new_session=True,
            )
            running.append((process, name, port))
            wait_for_health(opener, process, name, port)
        print(f"Цифровой механик: http://{HOST}:8000/ · Swagger UI: http://{HOST}:8000/docs", flush=True)
        while True:
            for service in running:
                ensure_running(*service)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nОстанавливаю сервисы.", flush=True)
        return 0
    except (RuntimeError, OSError) as error:
        print(f"Ошибка запуска: {error}", file=sys.stderr)
        return 1
    finally:
        stop_services(running)


if __name__ == "__main__":
    sys.exit(main())
