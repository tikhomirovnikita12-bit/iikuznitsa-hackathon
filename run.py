import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic, sleep
from urllib.request import ProxyHandler, build_opener

opener = build_opener(ProxyHandler({}))


def running(url: str, name: str) -> bool:
    try:
        with opener.open(f"{url}/health", timeout=0.30) as result:
            return json.load(result).get("service") == name
    except (OSError, ValueError):
        return False


def start_mock(processes, module, url, name, mocks):
    if running(url, name):
        return
    if not (mocks / module.split(".", 1)[0] / "app.py").is_file():
        raise SystemExit("Подготовьте заглушки командой python -m mechanic.prepare с архивом заказчика. См. README.")
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", module, "--host", "127.0.0.1",
         "--port", url.rsplit(":", 1)[1], "--no-access-log"],
        cwd=mocks,
    )
    processes.append(process)
    deadline = monotonic() + 12
    while monotonic() < deadline and process.poll() is None:
        if running(url, name):
            return
        sleep(0.15)
    raise RuntimeError(f"Не запустился {name} на {url}. "
                       "Проверьте сообщения сервиса выше и освободите указанный порт. "
                       "Для внешних сервисов задайте MOCK_ML_URL и MOCK_TOIR_URL в .env.")


def main():
    parser = argparse.ArgumentParser(description="Локальный стенд «Цифровой механик»")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-mocks", action="store_true", help="Использовать уже запущенные сервисы")
    parser.add_argument("--agent", action="store_true", help="Подключить агента к общему экрану через Ollama")
    parser.add_argument("--archive", type=Path, help="ZIP или каталог материалов; по умолчанию поиск в корне проекта")
    parser.add_argument("--rebuild-index", action="store_true", help="Пересобрать индекс и изображения, сохранив SQLite")
    args = parser.parse_args()
    if args.agent:
        os.environ["CASE_AGENT"] = "1"
    if not 1 <= args.port <= 65535:
        parser.error("--port должен быть от 1 до 65535")
    try:
        import uvicorn
        from mechanic.config import DEFAULT_ML, DEFAULT_TOIR, MOCKS, MOCK_VEHICLES, endpoint
        from mechanic.prepare import PreparationError, ensure_ready
    except ImportError as error:
        print(f"Не установлены зависимости проекта: {error.name}.\n"
              "Как исправить: активируйте виртуальное окружение и выполните "
              "python -m pip install -r requirements.txt. Либо запустите docker compose up --build.", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"Ошибка настроек: {error}\nПроверьте .env по примеру .env.example.", file=sys.stderr)
        return 1
    processes = []
    configs = (
        ("MOCK_ML_URL", DEFAULT_ML, "mock_ml.app:app", "mock-ml"),
        ("MOCK_TOIR_URL", DEFAULT_TOIR, "mock_toir.app:app", "mock-toir"),
    )
    try:
        if not endpoint("TEAM_API_URL") or not MOCK_VEHICLES.is_file():
            ensure_ready(args.archive, force=args.rebuild_index)
        if not MOCK_VEHICLES.is_file():
            raise PreparationError("Не найден каталог машин. Проверьте MOCK_DATA_PATH и CUSTOMER_DIR.")
        if not args.no_mocks and not endpoint("TEAM_API_URL"):
            os.environ["MOCK_DATA_PATH"] = str(MOCK_VEHICLES)
            for variable, default, module, name in configs:
                if endpoint(variable, default) == default:
                    start_mock(processes, module, default, name, MOCKS)
        print(f"Открыть приложение: http://{'127.0.0.1' if args.host == '0.0.0.0' else args.host}:{args.port}/", flush=True)
        uvicorn.run("mechanic.main:app", host=args.host, port=args.port, access_log=False)
    except (PreparationError, RuntimeError, OSError) as error:
        print(f"\nЗапуск остановлен.\n{error}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(f"\nОшибка настроек: {error}\n"
              "Проверьте .env: LOW_CONFIDENCE_THRESHOLD от 0 до 1, "
              "MOCK_API_TIMEOUT от 0 до 30 секунд (не включая 0), "
              "TEAM_API_TIMEOUT от 0 до 40 секунд (не включая 0).", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
