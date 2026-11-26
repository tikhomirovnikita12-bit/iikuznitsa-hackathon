"""Local persistence and cross-request locking for draft delivery."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS diagnoses (
    vehicle_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (vehicle_id, session_id)
);

CREATE TABLE IF NOT EXISTS drafts (
    draft_id TEXT PRIMARY KEY,
    vehicle_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    anomaly_type TEXT NOT NULL,
    fields TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'draft',
    attempt_id TEXT,
    started_at REAL,
    error_code TEXT,
    error_message TEXT,
    UNIQUE (vehicle_id, session_id, anomaly_type)
);

CREATE TABLE IF NOT EXISTS submitted_tickets (
    draft_id TEXT PRIMARY KEY REFERENCES drafts (draft_id),
    ticket_id TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connection(self, *, write=False):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            with conn:
                if write:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()
