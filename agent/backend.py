"""Adapters for the organizer's fleet and HTTP contracts."""
import json
import os
from pathlib import Path
from urllib.request import Request, urlopen
from zipfile import ZipFile
from mechanic.config import MATERIALS, MOCK_VEHICLES


def post(url, payload, timeout=30):
    request = Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


class Backend:
    def __init__(self):
        self.materials = Path(os.getenv("MATERIALS_DIR", "../007_хакатон_ИИ"))
        archive_path = next(self.materials.glob("*mock*.zip"), None)
        if archive_path:
            with ZipFile(archive_path) as archive:
                path = next(name for name in archive.namelist() if name.endswith("data/vehicles.json"))
                catalog = json.loads(archive.read(path))
        else:
            self.materials = MATERIALS
            catalog = json.loads(MOCK_VEHICLES.read_text(encoding="utf-8"))
        self.fleet = {v["vehicle_id"]: v for v in catalog["vehicles"]}

    def diagnose(self, vehicle_id, session_id):
        card = self.fleet[vehicle_id]
        session = next((s for s in card["sessions"] if s["session_id"] == session_id), None)
        if session is None:
            raise ValueError("Проверка принадлежит другому ТС")
        ids = dict(vehicle_id=vehicle_id, session_id=session_id)
        if os.getenv("AGENT_FIXTURE_MODE") == "1":
            result = {**session, **ids}
        else:
            base = os.getenv("MOCK_ML_URL", "http://127.0.0.1:8001")
            result = post(base.rstrip("/") + "/diagnose", ids)
        if any(result[k] != v for k, v in ids.items()):
            raise ValueError("Контекст ответа ML изменился")
        return dict(result, engine_model=card["engine_model"], fuel=card["fuel"])

    def submit(self, draft):
        base = os.getenv("MOCK_TOIR_URL", "http://127.0.0.1:8002")
        return post(base.rstrip("/") + "/tickets", draft)
