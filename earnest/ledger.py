"""SQLite ledger: profiles, mandates, requests, outcomes, trust scores, score events and the
user's own trusted/untrusted merchant lists."""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime

from .merchants import merchant_keys
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
CREATE TABLE IF NOT EXISTS merchant_lists (
    user_id TEXT NOT NULL, key TEXT NOT NULL, merchant TEXT NOT NULL, status TEXT NOT NULL,
    reason TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (user_id, key)
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, merchant TEXT NOT NULL, item TEXT NOT NULL,
    unit_price REAL NOT NULL, quantity INTEGER NOT NULL, date TEXT NOT NULL, source TEXT NOT NULL,
    product_id TEXT
);
CREATE INDEX IF NOT EXISTS orders_user ON orders (user_id, date);
CREATE TABLE IF NOT EXISTS interests (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, item TEXT NOT NULL,
    merchant TEXT, max_price REAL, source TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS confidence_weights (user_id TEXT PRIMARY KEY, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS price_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, merchant TEXT NOT NULL, item TEXT NOT NULL, price REAL NOT NULL,
    date TEXT NOT NULL, source TEXT NOT NULL, product_id TEXT, url TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS households (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, monthly_budget REAL, second_approval_over REAL
);
CREATE TABLE IF NOT EXISTS household_members (
    household_id TEXT NOT NULL, user_id TEXT NOT NULL, role TEXT NOT NULL, monthly_allowance REAL,
    PRIMARY KEY (user_id)
);
CREATE TABLE IF NOT EXISTS request_approvals (
    request_id TEXT NOT NULL, approver TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (request_id, approver)
);
CREATE TABLE IF NOT EXISTS trusted_issuers (user_id TEXT NOT NULL, kid TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (user_id, kid));
CREATE TABLE IF NOT EXISTS merchant_reputation (merchant TEXT PRIMARY KEY, data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS credentials (credential_id TEXT PRIMARY KEY, request_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verifications (
    request_id TEXT PRIMARY KEY, status TEXT NOT NULL, detail TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memberships (
    user_id TEXT NOT NULL, merchant TEXT NOT NULL, program TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (user_id, merchant)
);
"""


class Ledger:
    def __init__(self, path: str = ":memory:"):
        # `earnest serve` uses worker threads; it serializes access with a lock
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that newer versions expect to databases made by older ones."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(orders)")}
        if "product_id" not in cols:
            with self.db:
                self.db.execute("ALTER TABLE orders ADD COLUMN product_id TEXT")

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

    def purchases(self, user_id: str) -> list[dict]:
        """Approved requests with the user's verdict (if reported), oldest first."""
        rows = self.db.execute(
            "SELECT r.id, r.agent_id, r.task_type, r.merchant, r.amount, r.data, r.created_at, "
            "o.data AS outcome, v.status AS verification FROM requests r "
            "LEFT JOIN outcomes o ON o.request_id = r.id "
            "LEFT JOIN verifications v ON v.request_id = r.id "
            "WHERE r.user_id = ? AND r.status = 'approved' ORDER BY r.created_at", (user_id,))
        out = []
        for r in rows:
            data = json.loads(r["data"])
            out.append({"request_id": r["id"], "agent_id": r["agent_id"], "task_type": r["task_type"],
                        "verification": r["verification"],
                        "merchant": r["merchant"], "amount": r["amount"], "at": r["created_at"],
                        "item": data.get("item"), "quantity": data.get("quantity", 1),
                        "product_id": data.get("product_id"), "recurring": data.get("recurring", False),
                        "category": data.get("category"), "mandate_id": data.get("mandate_id"),
                        "verdict": json.loads(r["outcome"])["verdict"] if r["outcome"] else None})
        return out

    def statement_purchases(self, user_id: str) -> list[dict]:
        """Spending from imported card statements, if any were imported (see earnest.insights)."""
        has = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
        if not has:
            return []
        return [dict(r) for r in self.db.execute(
            "SELECT merchant, amount, date FROM transactions WHERE user_id = ? ORDER BY date", (user_id,))]

    # --- retailer order history, interests, memberships ---
    def add_orders(self, user_id: str, orders, source: str) -> tuple[int, int]:
        """Insert orders (earnest.insights.orders.Order), skipping ones already stored."""
        added = 0
        with self.db:
            for o in orders:
                added += self.db.execute(
                    "INSERT OR IGNORE INTO orders (id, user_id, merchant, item, unit_price, quantity, date, "
                    "source, product_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (o.id, user_id, o.merchant, o.item, o.unit_price, o.quantity, o.date.isoformat(),
                     source, getattr(o, "product_id", None))).rowcount
        return added, len(orders) - added

    def orders(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT merchant, item, unit_price, quantity, date, source, product_id FROM orders "
            "WHERE user_id = ? ORDER BY date", (user_id,))]

    def add_interest(self, user_id: str, item: str, merchant: str | None, max_price: float | None,
                     source: str, at: datetime) -> int:
        with self.db:
            return self.db.execute(
                "INSERT INTO interests (user_id, item, merchant, max_price, source, at) VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, item, merchant, max_price, source, at.isoformat())).lastrowid

    def interests(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT id, item, merchant, max_price, source, at FROM interests WHERE user_id = ? ORDER BY id",
            (user_id,))]

    def remove_interest(self, user_id: str, interest_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM interests WHERE user_id = ? AND id = ?",
                                   (user_id, interest_id)).rowcount > 0

    def add_membership(self, user_id: str, merchant: str, program: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO memberships VALUES (?, ?, ?, ?)",
                            (user_id, merchant, program, at.isoformat()))

    def remove_membership(self, user_id: str, merchant: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM memberships WHERE user_id = ? AND merchant = ?",
                                   (user_id, merchant)).rowcount > 0

    def memberships(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT merchant, program, at FROM memberships WHERE user_id = ? ORDER BY merchant", (user_id,))]

    # --- the user's merchant lists ---
    def set_merchant_status(self, user_id: str, merchant: str, status: str, reason: str, at: datetime) -> None:
        if status not in ("trusted", "untrusted"):
            raise ValueError("status must be trusted or untrusted")
        with self.db:
            for k in merchant_keys(merchant):
                self.db.execute("INSERT OR REPLACE INTO merchant_lists VALUES (?, ?, ?, ?, ?, ?)",
                                (user_id, k, merchant, status, reason, at.isoformat()))

    def forget_merchant(self, user_id: str, merchant: str) -> int:
        with self.db:
            n = 0
            for k in merchant_keys(merchant):
                n += self.db.execute("DELETE FROM merchant_lists WHERE user_id = ? AND key = ?",
                                     (user_id, k)).rowcount
        return n

    def merchant_overrides(self, user_id: str) -> dict[str, tuple[str, str]]:
        return {r["key"]: (r["status"], r["reason"]) for r in self.db.execute(
            "SELECT key, status, reason FROM merchant_lists WHERE user_id = ?", (user_id,))}

    def merchant_lists(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT DISTINCT merchant, status, reason, at FROM merchant_lists WHERE user_id = ? "
            "ORDER BY status, merchant", (user_id,))]

    # --- households ---
    def put_household(self, h) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO households VALUES (?, ?, ?, ?)",
                            (h.id, h.name, h.monthly_budget, h.second_approval_over))
            self.db.execute("DELETE FROM household_members WHERE household_id = ?", (h.id,))
            for m in h.members:
                self.db.execute("INSERT OR REPLACE INTO household_members VALUES (?, ?, ?, ?)",
                                (h.id, m.user_id, m.role, m.monthly_allowance))

    def household_for(self, user_id: str):
        from .household import Household, Member
        row = self.db.execute("SELECT household_id FROM household_members WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return None
        h = self.db.execute("SELECT * FROM households WHERE id = ?", (row["household_id"],)).fetchone()
        members = [Member(r["user_id"], r["role"], r["monthly_allowance"]) for r in self.db.execute(
            "SELECT * FROM household_members WHERE household_id = ? ORDER BY user_id", (h["id"],))]
        return Household(h["id"], h["name"], members, h["monthly_budget"], h["second_approval_over"])

    def approved_spend_since(self, user_ids: list[str], since_iso: str) -> float:
        if not user_ids:
            return 0.0
        q = ",".join("?" * len(user_ids))
        row = self.db.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS s FROM requests WHERE status = 'approved' "
            f"AND user_id IN ({q}) AND created_at >= ?", (*user_ids, since_iso)).fetchone()
        return float(row["s"])

    def mandate_spent_since(self, mandate_id: str, since_iso: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM requests WHERE mandate_id = ? AND status = 'approved' "
            "AND created_at >= ?", (mandate_id, since_iso)).fetchone()
        return float(row["s"])

    def add_approval(self, request_id: str, approver: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO request_approvals VALUES (?, ?, ?)",
                            (request_id, approver, at.isoformat()))

    def approvals(self, request_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT approver, at FROM request_approvals WHERE request_id = ? ORDER BY at", (request_id,))]

    def get_outcome(self, request_id: str) -> dict | None:
        row = self.db.execute("SELECT data, at FROM outcomes WHERE request_id = ?", (request_id,)).fetchone()
        return {**json.loads(row["data"]), "at": row["at"]} if row else None

    def request_events(self, request_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT kind, points, at FROM events WHERE request_id = ? ORDER BY id", (request_id,))]

    # --- portable trust & shared reputation ---
    def trust_issuer(self, user_id: str, kid: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO trusted_issuers VALUES (?, ?, ?)", (user_id, kid, at.isoformat()))

    def trusted_issuers(self, user_id: str) -> set[str]:
        return {r["kid"] for r in self.db.execute("SELECT kid FROM trusted_issuers WHERE user_id = ?", (user_id,))}

    def has_event(self, user_id: str, agent_id: str, task_type: str, kind: str, request_id: str) -> bool:
        return self.db.execute(
            "SELECT 1 FROM events WHERE user_id = ? AND agent_id = ? AND task_type = ? AND kind = ? "
            "AND request_id = ? LIMIT 1", (user_id, agent_id, task_type, kind, request_id)).fetchone() is not None

    def count_events_since(self, user_id: str, agent_id: str, task_type: str, kinds: tuple[str, ...],
                           since_iso: str) -> int:
        q = ",".join("?" * len(kinds))
        return self.db.execute(
            f"SELECT COUNT(*) AS n FROM events WHERE user_id = ? AND agent_id = ? AND task_type = ? "
            f"AND kind IN ({q}) AND at >= ?", (user_id, agent_id, task_type, *kinds, since_iso)).fetchone()["n"]

    def set_reputation_feed(self, merchants: dict, at: datetime) -> None:
        with self.db:
            self.db.execute("DELETE FROM merchant_reputation")
            for m, entry in merchants.items():
                self.db.execute("INSERT INTO merchant_reputation VALUES (?, ?, ?)", (m, json.dumps(entry), at.isoformat()))

    def reputation(self, keys: set[str]) -> dict | None:
        for k in sorted(keys):
            row = self.db.execute("SELECT data FROM merchant_reputation WHERE merchant = ?", (k,)).fetchone()
            if row:
                return json.loads(row["data"])
        return None

    # --- learned confidence weights ---
    def confidence_weights(self, user_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM confidence_weights WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(row["data"]) if row else None

    def set_confidence_weights(self, user_id: str, weights: dict | None, at: datetime) -> None:
        with self.db:
            if weights is None:
                self.db.execute("DELETE FROM confidence_weights WHERE user_id = ?", (user_id,))
            else:
                self.db.execute("INSERT OR REPLACE INTO confidence_weights VALUES (?, ?, ?)",
                                (user_id, json.dumps(weights), at.isoformat()))

    def labeled_purchases(self, user_id: str) -> list[tuple[dict, bool]]:
        """(factor scores, kept?) for every scored purchase the user has given a verdict on.
        An item mismatch counts as not kept."""
        rows = self.db.execute(
            "SELECT r.decision, o.data AS outcome, v.status AS verification FROM requests r "
            "LEFT JOIN outcomes o ON o.request_id = r.id LEFT JOIN verifications v ON v.request_id = r.id "
            "WHERE r.user_id = ? AND r.status = 'approved' ORDER BY r.created_at", (user_id,))
        out = []
        for r in rows:
            factors = ((json.loads(r["decision"]).get("confidence") or {}).get("factors"))
            if not factors:
                continue
            if r["verification"] == "mismatch":
                out.append((factors, False))
            elif r["outcome"]:
                out.append((factors, json.loads(r["outcome"])["verdict"] == "accepted"))
        return out

    # --- market price quotes ---
    def add_price_quote(self, q, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO price_quotes (merchant, item, price, date, source, product_id, url, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (q.merchant, q.item, q.price, q.on.isoformat(), q.source, q.product_id, q.url, at.isoformat()))

    def price_quotes(self, since: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT merchant, item, price, date, source, product_id, url FROM price_quotes WHERE date >= ? "
            "ORDER BY date", (since,))]

    # --- local secrets (approval links, attestations) ---
    def secret(self, name: str) -> bytes:
        """A random per-database secret, created on first use. Never leaves the machine."""
        row = self.db.execute("SELECT value FROM settings WHERE name = ?", (f"secret:{name}",)).fetchone()
        if row:
            return bytes.fromhex(row["value"])
        value = os.urandom(32)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (f"secret:{name}", value.hex()))
        return self.secret(name)

    # --- credentials & verification ---
    def link_credential(self, credential_id: str, request_id: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO credentials VALUES (?, ?)", (credential_id, request_id))

    def request_for_credential(self, credential_id: str) -> str | None:
        row = self.db.execute("SELECT request_id FROM credentials WHERE credential_id = ?",
                              (credential_id,)).fetchone()
        return row["request_id"] if row else None

    def set_verification(self, request_id: str, status: str, detail: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO verifications VALUES (?, ?, ?, ?)",
                            (request_id, status, detail, at.isoformat()))

    def verification(self, request_id: str) -> dict | None:
        row = self.db.execute("SELECT status, detail, at FROM verifications WHERE request_id = ?",
                              (request_id,)).fetchone()
        return dict(row) if row else None

    def last_event(self, user_id: str, agent_id: str, task_type: str, kind: str) -> datetime | None:
        row = self.db.execute(
            "SELECT MAX(at) AS at FROM events WHERE user_id = ? AND agent_id = ? AND task_type = ? AND kind = ?",
            (user_id, agent_id, task_type, kind)).fetchone()
        return datetime.fromisoformat(row["at"]) if row and row["at"] else None

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
