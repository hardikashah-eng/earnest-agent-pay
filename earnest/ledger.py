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
CREATE TABLE IF NOT EXISTS agent_accounts (
    user_id TEXT NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL, status_reason TEXT,
    max_allowance REAL, topup_to REAL, topup_every_days INTEGER, last_topup TEXT, created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, agent_id)
);
CREATE TABLE IF NOT EXISTS account_funding (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL, kind TEXT NOT NULL,
    merchant TEXT, last4 TEXT, label TEXT, active INTEGER NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS account_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, agent_id TEXT NOT NULL,
    funding_id TEXT NOT NULL, kind TEXT NOT NULL, amount REAL NOT NULL, request_id TEXT, note TEXT,
    at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS account_entries_agent ON account_entries (user_id, agent_id, at);
CREATE TABLE IF NOT EXISTS shadow_enrollments (
    user_id TEXT NOT NULL, agent_id TEXT NOT NULL, card TEXT NOT NULL, started_at TEXT NOT NULL,
    ended_at TEXT, PRIMARY KEY (user_id, agent_id)
);
CREATE TABLE IF NOT EXISTS gift_people (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, relationship TEXT NOT NULL,
    sizes TEXT NOT NULL, notes TEXT, budget REAL, auto_buy_max REAL, address TEXT, created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS gift_people_name ON gift_people (user_id, name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS gift_occasions (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, person_id TEXT NOT NULL, kind TEXT NOT NULL,
    month INTEGER NOT NULL, day INTEGER NOT NULL, year INTEGER, label TEXT, source TEXT NOT NULL,
    UNIQUE (person_id, kind, month, day)
);
CREATE TABLE IF NOT EXISTS gift_prefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, person_id TEXT NOT NULL, text TEXT NOT NULL,
    kind TEXT NOT NULL, source TEXT NOT NULL, at TEXT NOT NULL, UNIQUE (person_id, text, kind)
);
CREATE TABLE IF NOT EXISTS gift_policy (user_id TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS gift_plans (
    id TEXT PRIMARY KEY, user_id TEXT NOT NULL, person_id TEXT NOT NULL, occasion_id TEXT NOT NULL,
    on_date TEXT NOT NULL, budget REAL NOT NULL, status TEXT NOT NULL, agent_id TEXT, mandate_id TEXT,
    options TEXT, chosen INTEGER, request_id TEXT, note TEXT, feedback TEXT, created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL, UNIQUE (occasion_id, on_date)
);
CREATE TABLE IF NOT EXISTS gift_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL, person_id TEXT NOT NULL, title TEXT NOT NULL,
    year INTEGER NOT NULL, amount REAL, plan_id TEXT, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gift_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, merchant TEXT NOT NULL, price REAL NOT NULL,
    data TEXT NOT NULL, UNIQUE (title, merchant)
);
CREATE TABLE IF NOT EXISTS shadow_reviews (request_id TEXT PRIMARY KEY, would_buy INTEGER NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_matches (
    request_id TEXT PRIMARY KEY, tx_id TEXT NOT NULL, tx_date TEXT NOT NULL, tx_amount REAL NOT NULL,
    at TEXT NOT NULL
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

    # --- agent accounts (spec section 3i) ---
    def put_account(self, user_id: str, agent_id: str, max_allowance: float | None, topup_to: float | None,
                    topup_every_days: int | None, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO agent_accounts (user_id, agent_id, status, max_allowance, topup_to, topup_every_days, "
                "created_at) VALUES (?, ?, 'active', ?, ?, ?, ?) ON CONFLICT (user_id, agent_id) DO UPDATE SET "
                "max_allowance = excluded.max_allowance, topup_to = excluded.topup_to, "
                "topup_every_days = excluded.topup_every_days",
                (user_id, agent_id, max_allowance, topup_to, topup_every_days, at.isoformat()))
            self.db.execute(
                "INSERT OR IGNORE INTO account_funding VALUES (?, ?, ?, 'allowance', NULL, NULL, NULL, 1, ?)",
                (f"allowance:{user_id}:{agent_id}", user_id, agent_id, at.isoformat()))

    def get_account(self, user_id: str, agent_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM agent_accounts WHERE user_id = ? AND agent_id = ?",
                              (user_id, agent_id)).fetchone()
        return dict(row) if row else None

    def accounts(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM agent_accounts WHERE user_id = ? ORDER BY agent_id", (user_id,))]

    def update_account(self, user_id: str, agent_id: str, **fields) -> None:
        allowed = {"status", "status_reason", "max_allowance", "last_topup"}
        if not fields or set(fields) - allowed:
            raise ValueError(f"can only update {sorted(allowed)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE agent_accounts SET {sets} WHERE user_id = ? AND agent_id = ?",
                            (*fields.values(), user_id, agent_id))

    def add_funding(self, funding_id: str, user_id: str, agent_id: str, kind: str, merchant: str | None,
                    last4: str | None, label: str | None, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT INTO account_funding VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)",
                            (funding_id, user_id, agent_id, kind, merchant, last4, label, at.isoformat()))

    def set_funding_active(self, funding_id: str, active: bool) -> None:
        with self.db:
            self.db.execute("UPDATE account_funding SET active = ? WHERE id = ?", (int(active), funding_id))

    def funding(self, user_id: str, agent_id: str, active_only: bool = True) -> list[dict]:
        q = ("SELECT f.*, COALESCE((SELECT SUM(e.amount) FROM account_entries e WHERE e.funding_id = f.id), 0) "
             "AS balance FROM account_funding f WHERE f.user_id = ? AND f.agent_id = ?")
        if active_only:
            q += " AND f.active = 1"
        return [dict(r) for r in self.db.execute(q + " ORDER BY f.created_at, f.id", (user_id, agent_id))]

    def add_entry(self, user_id: str, agent_id: str, funding_id: str, kind: str, amount: float,
                  request_id: str | None, note: str | None, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO account_entries (user_id, agent_id, funding_id, kind, amount, request_id, note, at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, agent_id, funding_id, kind, round(amount, 2), request_id, note, at.isoformat()))

    def entries(self, user_id: str, agent_id: str, since_iso: str = "0000-01-01") -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT funding_id, kind, amount, request_id, note, at FROM account_entries "
            "WHERE user_id = ? AND agent_id = ? AND at >= ? ORDER BY id", (user_id, agent_id, since_iso))]

    def entries_for_request(self, request_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT user_id, agent_id, funding_id, kind, amount, at FROM account_entries WHERE request_id = ? "
            "ORDER BY id", (request_id,))]

    def holds(self, user_id: str) -> list[dict]:
        """Allowance holds for virtual cards, with the request's decision (for the credential)."""
        return [dict(r) for r in self.db.execute(
            "SELECT e.agent_id, e.funding_id, e.amount, e.request_id, r.decision FROM account_entries e "
            "JOIN requests r ON r.id = e.request_id WHERE e.user_id = ? AND e.kind = 'hold' ORDER BY e.id",
            (user_id,))]

    def task_types(self, user_id: str, agent_id: str) -> list[str]:
        return [r["task_type"] for r in self.db.execute(
            "SELECT task_type FROM trust WHERE user_id = ? AND agent_id = ? ORDER BY task_type", (user_id, agent_id))]

    def points_of_kind(self, user_id: str, agent_id: str, task_type: str, kind: str) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(points), 0) AS s FROM events WHERE user_id = ? AND agent_id = ? AND task_type = ? "
            "AND kind = ?", (user_id, agent_id, task_type, kind)).fetchone()
        return float(row["s"])

    # --- gift concierge (spec section 3k) ---
    def put_person(self, row: dict) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO gift_people VALUES (:id, :user_id, :name, :relationship, :sizes, :notes, :budget, "
                ":auto_buy_max, :address, :created_at) ON CONFLICT (id) DO UPDATE SET name = excluded.name, "
                "relationship = excluded.relationship, sizes = excluded.sizes, notes = excluded.notes, "
                "budget = excluded.budget, auto_buy_max = excluded.auto_buy_max, address = excluded.address",
                {**row, "sizes": json.dumps(row.get("sizes") or {})})

    def people(self, user_id: str) -> list[dict]:
        return [{**dict(r), "sizes": json.loads(r["sizes"])} for r in self.db.execute(
            "SELECT * FROM gift_people WHERE user_id = ? ORDER BY name COLLATE NOCASE", (user_id,))]

    def person(self, user_id: str, ref: str) -> dict | None:
        """By id, or by name (case-insensitive)."""
        r = self.db.execute("SELECT * FROM gift_people WHERE user_id = ? AND (id = ? OR name = ? COLLATE NOCASE)",
                            (user_id, ref, ref)).fetchone()
        return {**dict(r), "sizes": json.loads(r["sizes"])} if r else None

    def put_occasion(self, row: dict) -> bool:
        with self.db:
            return self.db.execute(
                "INSERT OR IGNORE INTO gift_occasions VALUES (:id, :user_id, :person_id, :kind, :month, :day, :year, "
                ":label, :source)", row).rowcount > 0

    def occasions(self, user_id: str, person_id: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM gift_occasions WHERE user_id = ?", [user_id]
        if person_id:
            q += " AND person_id = ?"
            args.append(person_id)
        return [dict(r) for r in self.db.execute(q + " ORDER BY month, day", args)]

    def delete_occasion(self, user_id: str, occasion_id: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM gift_occasions WHERE user_id = ? AND id = ?",
                                   (user_id, occasion_id)).rowcount > 0

    def add_pref(self, user_id: str, person_id: str, text: str, kind: str, source: str, at: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO gift_prefs (user_id, person_id, text, kind, source, at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (person_id, text, kind) DO UPDATE SET source = excluded.source, at = excluded.at",
                (user_id, person_id, text, kind, source, at.isoformat()))

    def prefs(self, person_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT text, kind, source, at FROM gift_prefs WHERE person_id = ? ORDER BY id", (person_id,))]

    def remove_pref(self, person_id: str, text: str) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM gift_prefs WHERE person_id = ? AND text = ? COLLATE NOCASE",
                                   (person_id, text)).rowcount > 0

    def gift_policy(self, user_id: str) -> dict:
        r = self.db.execute("SELECT data FROM gift_policy WHERE user_id = ?", (user_id,)).fetchone()
        return json.loads(r["data"]) if r else {}

    def put_gift_policy(self, user_id: str, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO gift_policy VALUES (?, ?)", (user_id, json.dumps(data)))

    def put_plan(self, row: dict) -> bool:
        with self.db:
            return self.db.execute(
                "INSERT OR IGNORE INTO gift_plans (id, user_id, person_id, occasion_id, on_date, budget, status, "
                "agent_id, created_at, updated_at) VALUES (:id, :user_id, :person_id, :occasion_id, :on_date, "
                ":budget, :status, :agent_id, :at, :at)", row).rowcount > 0

    def update_plan(self, plan_id: str, at: datetime, **fields) -> None:
        allowed = {"status", "mandate_id", "options", "chosen", "request_id", "note", "feedback", "budget",
                   "agent_id"}
        if set(fields) - allowed:
            raise ValueError(f"can only update {sorted(allowed)}")
        if "options" in fields and fields["options"] is not None:
            fields["options"] = json.dumps(fields["options"])
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.db:
            self.db.execute(f"UPDATE gift_plans SET {sets}, updated_at = ? WHERE id = ?",
                            (*fields.values(), at.isoformat(), plan_id))

    def plan(self, plan_id: str) -> dict | None:
        r = self.db.execute("SELECT * FROM gift_plans WHERE id = ?", (plan_id,)).fetchone()
        return self._plan_row(r) if r else None

    def plans(self, user_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
        q, args = "SELECT * FROM gift_plans WHERE user_id = ?", [user_id]
        if statuses:
            q += f" AND status IN ({','.join('?' * len(statuses))})"
            args += list(statuses)
        return [self._plan_row(r) for r in self.db.execute(q + " ORDER BY on_date, id", args)]

    def plans_for_agent(self, agent_id: str, statuses: tuple[str, ...]) -> list[dict]:
        q = f"SELECT * FROM gift_plans WHERE agent_id = ? AND status IN ({','.join('?' * len(statuses))})"
        return [self._plan_row(r) for r in self.db.execute(q + " ORDER BY on_date", (agent_id, *statuses))]

    @staticmethod
    def _plan_row(r) -> dict:
        d = dict(r)
        d["options"] = json.loads(d["options"]) if d["options"] else []
        return d

    def add_gift_history(self, user_id: str, person_id: str, title: str, year: int, amount: float | None,
                         plan_id: str | None, source: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO gift_history (user_id, person_id, title, year, amount, plan_id, source) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)", (user_id, person_id, title, year, amount, plan_id, source))

    def gift_history(self, person_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute(
            "SELECT title, year, amount, plan_id, source FROM gift_history WHERE person_id = ? ORDER BY year DESC, id DESC",
            (person_id,))]

    def gift_spend(self, user_id: str, year: int) -> float:
        r = self.db.execute("SELECT COALESCE(SUM(amount), 0) AS s FROM gift_history WHERE user_id = ? AND year = ? "
                            "AND plan_id IS NOT NULL", (user_id, year)).fetchone()
        return float(r["s"])

    def put_catalog(self, title: str, merchant: str, price: float, data: dict) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO gift_catalog (title, merchant, price, data) VALUES (?, ?, ?, ?)",
                            (title, merchant, price, json.dumps(data)))

    def catalog(self) -> list[dict]:
        return [json.loads(r["data"]) for r in self.db.execute("SELECT data FROM gift_catalog ORDER BY id")]

    # --- shadow mode (spec section 3j) ---
    def put_shadow(self, user_id: str, agent_id: str, card: str, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO shadow_enrollments VALUES (?, ?, ?, ?, NULL)",
                            (user_id, agent_id, card, at.isoformat()))

    def end_shadow(self, user_id: str, agent_id: str, at: datetime) -> bool:
        with self.db:
            return self.db.execute(
                "UPDATE shadow_enrollments SET ended_at = ? WHERE user_id = ? AND agent_id = ? AND ended_at IS NULL",
                (at.isoformat(), user_id, agent_id)).rowcount > 0

    def shadow_enrollment(self, user_id: str, agent_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM shadow_enrollments WHERE user_id = ? AND agent_id = ?",
                              (user_id, agent_id)).fetchone()
        return dict(row) if row else None

    def shadow_requests(self, user_id: str, agent_id: str | None = None) -> list[dict]:
        q = ("SELECT r.*, sr.would_buy, sm.tx_id, sm.tx_date, sm.tx_amount FROM requests r "
             "LEFT JOIN shadow_reviews sr ON sr.request_id = r.id "
             "LEFT JOIN shadow_matches sm ON sm.request_id = r.id "
             "WHERE r.user_id = ? AND r.status = 'shadow'")
        args: list = [user_id]
        if agent_id is not None:
            q += " AND r.agent_id = ?"
            args.append(agent_id)
        return [dict(r) for r in self.db.execute(q + " ORDER BY r.created_at", args)]

    def put_shadow_review(self, request_id: str, would_buy: bool, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO shadow_reviews VALUES (?, ?, ?)",
                            (request_id, int(would_buy), at.isoformat()))

    def put_shadow_match(self, request_id: str, tx_id: str, tx_date: str, tx_amount: float, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO shadow_matches VALUES (?, ?, ?, ?, ?)",
                            (request_id, tx_id, tx_date, tx_amount, at.isoformat()))

    def matched_tx_ids(self, user_id: str) -> set[str]:
        return {r["tx_id"] for r in self.db.execute(
            "SELECT sm.tx_id FROM shadow_matches sm JOIN requests r ON r.id = sm.request_id WHERE r.user_id = ?",
            (user_id,))}

    def card_transactions(self, user_id: str, card: str | None) -> list[dict]:
        """Imported statement rows for one card (all cards when `card` is None), oldest first."""
        has = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
        if not has:
            return []
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(transactions)")}
        q, args = "SELECT * FROM transactions WHERE user_id = ?", [user_id]
        if card is not None:
            if "card" not in cols:
                return []
            q += " AND card = ?"
            args.append(card)
        return [dict(r) for r in self.db.execute(q + " ORDER BY date, id", args)]

    def cards(self, user_id: str) -> list[dict]:
        has = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'transactions'").fetchone()
        if not has or "card" not in {r["name"] for r in self.db.execute("PRAGMA table_info(transactions)")}:
            return []
        return [dict(r) for r in self.db.execute(
            "SELECT card, COUNT(*) AS transactions, MIN(date) AS first, MAX(date) AS last FROM transactions "
            "WHERE user_id = ? AND card IS NOT NULL GROUP BY card ORDER BY card", (user_id,))]

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
