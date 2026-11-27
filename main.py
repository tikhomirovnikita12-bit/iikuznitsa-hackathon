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
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from dotenv import load_dotenv


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


def fetch_status(opener, url):
    try:
        with opener.open(url, timeout=0.5) as response:
            status = json.load(response)
        return status if isinstance(status, dict) else {}
    except (URLError, OSError, ValueError):
        return {}


def wait_for_health(opener, process, name, port, url, ready):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        ensure_running(process, name, port)
        if ready(fetch_status(opener, url)):
            return
        time.sleep(0.2)
    raise RuntimeError(f"{name} на порту {port} не ответил на {url} за 20 секунд.")


def start_local_ollama(opener, running):
    models_directory = os.getenv("LOCAL_OLLAMA_MODELS")
    if not models_directory:
        return
    url = os.getenv("OLLAMA_URL", f"http://{HOST}:11434").rstrip("/")
    address = urlsplit(url)
    if address.scheme != "http" or address.hostname not in (HOST, "localhost") or not address.port:
        raise RuntimeError("Для запуска Ollama задайте локальный OLLAMA_URL с портом.")

    def ready(status):
        return isinstance(status.get("version"), str)

    version_url = f"{url}/api/version"
    if ready(fetch_status(opener, version_url)):
        return
    process = subprocess.Popen(
        ["ollama", "serve"], cwd=ROOT,
        env={**os.environ, "OLLAMA_HOST": address.netloc, "OLLAMA_MODELS": models_directory},
        start_new_session=True, creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    running.append((process, "Ollama", address.port))
    wait_for_health(opener, process, "Ollama", address.port, version_url, ready)


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
    load_dotenv(ROOT / ".env")
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
        start_local_ollama(opener, running)
        for name, app, port, directory in SERVICES:
            env = None
            if name == "digital-mechanic":
                env = {**os.environ, "MOCK_ML_URL": f"http://{HOST}:8001", "MOCK_TOIR_URL": f"http://{HOST}:8002"}
            process = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", app, "--host", HOST, "--port", str(port)],
                cwd=directory, env=env, start_new_session=True,
            )
            running.append((process, name, port))
            wait_for_health(
                opener, process, name, port, f"http://{HOST}:{port}/health",
                lambda status: status.get("status") == "ok" and status.get("service") == name,
            )
        print(f"API: http://{HOST}:8000/health · Swagger UI: http://{HOST}:8000/docs", flush=True)
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
