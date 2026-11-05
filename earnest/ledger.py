"""SQLite ledger: profiles, mandates, requests, outcomes, trust scores and score events."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from .models import Mandate, Outcome, Profile, SpendRequest
from .scoring import decay

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS mandates (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL, mandate_id TEXT NOT NULL,
    task_type TEXT, merchant TEXT NOT NULL, amount REAL NOT NULL, status TEXT NOT NULL,
    data TEXT NOT NULL, decision TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outcomes (request_id TEXT PRIMARY KEY, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trust (
    user_id TEXT NOT NULL, agent_id TEXT NOT NULL, task_type TEXT NOT NULL,
    score REAL NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, agent_id, task_type)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    task_type TEXT NOT NULL, kind TEXT NOT NULL, points REAL NOT NULL, request_id TEXT, at TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    # --- profiles & mandates ---
    def put_profile(self, p: Profile) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO profiles VALUES (?, ?)", (p.user_id, json.dumps(p.to_dict())))

    def get_profile(self, user_id: str) -> Profile | None:
        row = self.db.execute("SELECT data FROM profiles WHERE user_id = ?", (user_id,)).fetchone()
        return Profile.from_dict(json.loads(row["data"])) if row else None

    def put_mandate(self, m: Mandate) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO mandates VALUES (?, ?, ?)",
                            (m.id, m.user_id, json.dumps(m.to_dict())))

    def get_mandate(self, mandate_id: str) -> Mandate | None:
        row = self.db.execute("SELECT data FROM mandates WHERE id = ?", (mandate_id,)).fetchone()
        return Mandate.from_dict(json.loads(row["data"])) if row else None

    def list_mandates(self, user_id: str) -> list[Mandate]:
        rows = self.db.execute("SELECT data FROM mandates WHERE user_id = ? ORDER BY id", (user_id,))
        return [Mandate.from_dict(json.loads(r["data"])) for r in rows]

    # --- requests ---
    def add_request(self, request_id: str, user_id: str, task_type: str | None, req: SpendRequest,
                    status: str, decision: dict, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (request_id, user_id, req.agent_id, req.mandate_id, task_type, req.merchant, req.amount,
                 status, json.dumps(req.to_dict()), json.dumps(decision), at.isoformat()))

    def update_request(self, request_id: str, status: str, decision: dict) -> None:
        with self.db:
            self.db.execute("UPDATE requests SET status = ?, decision = ? WHERE id = ?",
                            (status, json.dumps(decision), request_id))

    def get_request(self, request_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()

    def pending(self, user_id: str) -> list[sqlite3.Row]:
        return list(self.db.execute(
            "SELECT * FROM requests WHERE user_id = ? AND status = 'escalated' ORDER BY created_at", (user_id,)))

    def spent_on_mandate(self, mandate_id: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM requests WHERE mandate_id = ? AND status = 'approved'",
            (mandate_id,)).fetchone()
        return float(row["s"])

    def known_merchant(self, user_id: str, merchant: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM requests WHERE user_id = ? AND merchant = ? AND status = 'approved' LIMIT 1",
            (user_id, merchant)).fetchone()
        return row is not None

    # --- outcomes ---
    def add_outcome(self, o: Outcome, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT INTO outcomes VALUES (?, ?, ?)",
                            (o.request_id, json.dumps(o.to_dict()), at.isoformat()))

    def has_outcome(self, request_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM outcomes WHERE request_id = ?", (request_id,)).fetchone() is not None

    # --- trust ---
    def score(self, user_id: str, agent_id: str, task_type: str, now: datetime) -> float:
        row = self.db.execute(
            "SELECT score, updated_at FROM trust WHERE user_id = ? AND agent_id = ? AND task_type = ?",
            (user_id, agent_id, task_type)).fetchone()
        if not row:
            return 0.0
        return decay(row["score"], datetime.fromisoformat(row["updated_at"]), now)

    def add_points(self, user_id: str, agent_id: str, task_type: str, kind: str, points: float,
                   request_id: str | None, now: datetime) -> float:
        new = self.score(user_id, agent_id, task_type, now) + points
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO trust VALUES (?, ?, ?, ?, ?)",
                            (user_id, agent_id, task_type, new, now.isoformat()))
            self.db.execute(
                "INSERT INTO events (user_id, agent_id, task_type, kind, points, request_id, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, task_type, kind, points, request_id, now.isoformat()))
        return new

    def events(self, user_id: str, agent_id: str | None = None) -> list[sqlite3.Row]:
        if agent_id is None:
            return list(self.db.execute("SELECT * FROM events WHERE user_id = ? ORDER BY id", (user_id,)))
        return list(self.db.execute(
            "SELECT * FROM events WHERE user_id = ? AND agent_id = ? ORDER BY id", (user_id, agent_id)))
