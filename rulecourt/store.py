"""Persistent Case records. Model output never writes trusted state or verdict fields."""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CaseStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0, confirmed_state TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    text TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id), run_id TEXT,
                    type TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS verdicts (
                    id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id),
                    run_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    status TEXT NOT NULL, reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self) -> dict[str, Any]:
        case_id = str(uuid4())
        with self._connect() as db:
            db.execute("INSERT INTO cases(id, created_at) VALUES (?, ?)", (case_id, _now()))
        case = self.get(case_id)
        assert case is not None
        return case

    def exists(self, case_id: str) -> bool:
        with self._connect() as db:
            return db.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None

    def add_message(self, case_id: str, text: str) -> str:
        message_id = str(uuid4())
        with self._connect() as db:
            db.execute(
                "INSERT INTO messages(id, case_id, text, created_at) VALUES (?, ?, ?, ?)",
                (message_id, case_id, text, _now()),
            )
        return message_id

    def add_event(self, case_id: str, run_id: str, event_type: str, **payload):
        with self._connect() as db:
            db.execute(
                "INSERT INTO events(case_id, run_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (case_id, run_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
            )

    def add_verdict(self, case_id: str, run_id: str, status: str, reason: str) -> dict[str, Any]:
        verdict_id = str(uuid4())
        with self._connect() as db:
            revision = db.execute("SELECT revision FROM cases WHERE id=?", (case_id,)).fetchone()[0]
            db.execute(
                "INSERT INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?)",
                (verdict_id, case_id, run_id, revision, status, reason, _now()),
            )
            db.execute(
                "INSERT INTO events(case_id, run_id, type, payload, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    case_id,
                    run_id,
                    "verdict",
                    json.dumps({"verdict_id": verdict_id, "status": status}),
                    _now(),
                ),
            )
        return {
            "id": verdict_id,
            "case_id": case_id,
            "run_id": run_id,
            "revision": revision,
            "status": status,
            "reason": reason,
            "evidence": [],
        }

    def get(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                return None
            messages = [
                dict(item)
                for item in db.execute(
                    "SELECT id, text, created_at FROM messages WHERE case_id=? ORDER BY created_at, rowid",
                    (case_id,),
                )
            ]
            verdicts = [
                dict(item)
                for item in db.execute(
                    "SELECT * FROM verdicts WHERE case_id=? ORDER BY created_at, rowid",
                    (case_id,),
                )
            ]
            return {
                "id": row["id"],
                "created_at": row["created_at"],
                "revision": row["revision"],
                "confirmed_state": json.loads(row["confirmed_state"]),
                "messages": messages,
                "verdicts": verdicts,
            }

    def events(self, case_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, run_id, type, payload, created_at FROM events WHERE case_id=? ORDER BY id",
                (case_id,),
            )
            return [{**dict(row), **json.loads(row["payload"])} for row in rows]
