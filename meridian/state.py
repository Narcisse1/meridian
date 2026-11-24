"""App state (question history, past digests) in a small SQLite file.

Kept separate from the analysed PostgreSQL database, which Meridian can only read.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, asked_at TEXT NOT NULL, question TEXT NOT NULL,
    ok INTEGER NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, as_of TEXT NOT NULL,
    payload TEXT NOT NULL);
"""


class State:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.executescript(SCHEMA)

    def add_question(self, asked_at: str, payload: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO questions (asked_at, question, ok, payload) VALUES (?, ?, ?, ?)",
                (asked_at, payload["question"], int(bool(payload.get("ok"))), json.dumps(payload)),
            )

    def questions(self, limit: int = 30) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT asked_at, payload FROM questions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [{"asked_at": a, **json.loads(p)} for a, p in rows]

    def add_digest(self, created_at: str, payload: dict[str, Any]) -> None:
        with self._lock, self.conn:
            self.conn.execute(
                "INSERT INTO digests (created_at, as_of, payload) VALUES (?, ?, ?)",
                (created_at, payload["as_of"], json.dumps(payload)),
            )

    def latest_digest(self) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT created_at, payload FROM digests ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {"created_at": row[0], **json.loads(row[1])} if row else None
