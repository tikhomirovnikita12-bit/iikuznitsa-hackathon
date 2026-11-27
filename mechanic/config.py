import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
DEFAULT_ML = "http://127.0.0.1:8001"
DEFAULT_TOIR = "http://127.0.0.1:8002"


def load_env() -> None:
    path = ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator or not name.strip().isidentifier():
            raise ValueError("В .env ожидаются строки ИМЯ=значение")
        os.environ.setdefault(name.strip(), value.strip().strip("\u0022'"))


def endpoint(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip().rstrip("/")


def serve_manual_pdfs() -> bool:
    return os.environ.get("SERVE_MANUAL_PDFS", "").strip() == "1"


load_env()
MATERIALS = ROOT / os.environ.get("MANUALS_DIR", "artifacts/manuals")
MOCKS = ROOT / os.environ.get("CUSTOMER_DIR", "artifacts/customer")
CUSTOMER_VEHICLES = MOCKS / "data" / "vehicles.json"
MOCK_VEHICLES = ROOT / (os.environ.get("MOCK_DATA_PATH") or str(CUSTOMER_VEHICLES))
PREVIEWS = ROOT / "artifacts" / "manual-previews"
CORPUS = ROOT / os.environ.get("TEAM_MANUALS_INDEX",
                            os.environ.get("MANUAL_CORPUS_PATH", "artifacts/manual-corpus.json"))
