import json
import sqlite3
from contextlib import closing
from pathlib import Path
from time import time
from uuid import NAMESPACE_URL, uuid5


UNKNOWN = "ТОиР мог принять заявку. Результат отправки неизвестен. Сверьте заявку в ТОиР."


def empty_state() -> dict:
    return {
        "diagnosis": None,
        "brief": None,
        "brief_error": None,
        "messages": [],
        "draft": None,
        "draft_id": None,
        "draft_revision": None,
        "draft_origin": None,
        "ticket": {"status": "idle", "ticket_id": None, "message": None},
        "calls": [],
    }


def record_call(state: dict, call: dict) -> None:
    state["calls"] = [*state["calls"][-99:], call]


class Store:
    def __init__(self, path: Path, send_deadline: float):
        self.path = path
        self.send_deadline = send_deadline
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    vehicle_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY(vehicle_id, session_id)
                )
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def _read(self, connection, pair) -> dict:
        row = connection.execute(
            "SELECT value FROM sessions WHERE vehicle_id = ? AND session_id = ?", pair
        ).fetchone()
        state = empty_state() if row is None else {**empty_state(), **json.loads(row[0])}
        if state["draft"] and state["draft_id"] is None:
            original = json.dumps([*pair, state["draft"]["anomaly_type"]])
            state.update(draft_id=uuid5(NAMESPACE_URL, "digital-mechanic/draft/" + original).hex, draft_revision=1)
        ticket = state["ticket"]
        if ticket["status"] == "sending" and time() >= ticket["started_at"] + self.send_deadline:
            ticket.update(status="unknown", message=UNKNOWN)
        return state

    def read(self, pair) -> dict:
        with closing(self.connect()) as connection:
            return self._read(connection, pair)

    def update(self, pair, edit) -> dict:
        with closing(self.connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            state = self._read(connection, pair)
            edit(state)
            connection.execute(
                """
                INSERT INTO sessions VALUES (?, ?, ?)
                ON CONFLICT(vehicle_id, session_id) DO UPDATE SET value=excluded.value
                """,
                (*pair, json.dumps(state, ensure_ascii=False)),
            )
            return state


def public_state(state: dict) -> dict:
    ticket = state["ticket"]
    return {
        **{key: state[key] for key in empty_state()},
        "ticket": {name: ticket[name] for name in ("status", "ticket_id", "message")},
    }
