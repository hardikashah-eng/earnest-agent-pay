"""Transaction storage, kept in the same SQLite file as the trust ledger.

Statement data is sensitive and stays local: nothing here sends it anywhere.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime

from .statement import Transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, date TEXT NOT NULL, description TEXT NOT NULL,
    merchant TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS tx_user_date ON transactions (user_id, date);
CREATE TABLE IF NOT EXISTS imports (
    file_hash TEXT NOT NULL, user_id TEXT NOT NULL, name TEXT NOT NULL, source TEXT NOT NULL,
    added INTEGER NOT NULL, duplicates INTEGER NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (file_hash, user_id)
);
CREATE TABLE IF NOT EXISTS dismissed_insights (
    user_id TEXT NOT NULL, key TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (user_id, key)
);
"""


class TransactionStore:
    def __init__(self, db: sqlite3.Connection):
        self.db = db
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def add(self, user_id: str, txs: list[Transaction], source: str) -> tuple[int, int]:
        """Insert transactions, skipping ones already stored. Returns (added, duplicates)."""
        added = 0
        with self.db:
            for t in txs:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO transactions VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (t.id, user_id, t.date.isoformat(), t.description, t.merchant, t.category, t.amount, source))
                added += cur.rowcount
        return added, len(txs) - added

    def all(self, user_id: str, until: date | None = None) -> list[Transaction]:
        q, args = "SELECT * FROM transactions WHERE user_id = ?", [user_id]
        if until is not None:
            q += " AND date <= ?"
            args.append(until.isoformat())
        rows = self.db.execute(q + " ORDER BY date, id", args)
        return [Transaction(r["id"], date.fromisoformat(r["date"]), r["description"], r["merchant"],
                            r["category"], r["amount"]) for r in rows]

    # --- import bookkeeping (for automated sync) ---
    def file_imported(self, user_id: str, file_hash: str) -> bool:
        return self.db.execute("SELECT 1 FROM imports WHERE user_id = ? AND file_hash = ?",
                               (user_id, file_hash)).fetchone() is not None

    def record_import(self, user_id: str, file_hash: str, name: str, source: str,
                      added: int, duplicates: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO imports VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (file_hash, user_id, name, source, added, duplicates, datetime.now().isoformat()))

    def imports(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT name, source, added, duplicates, at FROM imports WHERE user_id = ? ORDER BY at", (user_id,))]

    # --- dismissed insights ---
    def dismiss(self, user_id: str, key: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO dismissed_insights VALUES (?, ?, ?)",
                            (user_id, key, datetime.now().isoformat()))

    def dismissed(self, user_id: str) -> set[str]:
        return {r["key"] for r in self.db.execute("SELECT key FROM dismissed_insights WHERE user_id = ?", (user_id,))}
