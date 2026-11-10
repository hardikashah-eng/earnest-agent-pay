"""Return checker (spec/SPEC.md section 3f).

Before buying: is the item returnable, and for how long? The window becomes a confidence
signal, and a mandate that says `requires_returnable` escalates when the merchant's policy is
unknown.

After buying: every purchase (through earnest, order history or receipts) gets a return-by
date. Items the user rejected or disputed and can still return are flagged first; anything
with a deadline in the next few days comes next.

Windows come from earnest/data/return_policies.json (standard policies, checked by date in
the file). They change and have exceptions, so this is a reminder, not a guarantee.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).parent / "data" / "return_policies.json"
OPEN_ENDED_DAYS = 365   # how long we keep reminding for "no fixed window" merchants
SOON_DAYS = 7


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


@dataclass
class ReturnWindow:
    merchant_id: str | None
    days: int | None          # None = unknown
    open_ended: bool = False
    note: str = ""

    @property
    def known(self) -> bool:
        return self.days is not None or self.open_ended

    def deadline(self, bought: date) -> date | None:
        if self.open_ended:
            return bought + timedelta(days=OPEN_ENDED_DAYS)
        return bought + timedelta(days=self.days) if self.days is not None else None

    def label(self) -> str:
        if self.open_ended:
            return "no fixed return window"
        return f"{self.days}-day returns" if self.days is not None else "return policy unknown"


def window(merchant_id: str | None, category: str | None = None, memberships: list[str] = ()) -> ReturnWindow:
    data = _data()
    p = data["policies"].get(merchant_id or "")
    if p is None:
        return ReturnWindow(merchant_id, None)
    cat = (category or "").lower()
    is_electronics = cat in data["electronics_categories"]
    days = p.get("days")
    open_ended = bool(p.get("open_ended"))
    cats = p.get("categories", {})
    if is_electronics and "electronics" in cats:
        days, open_ended = cats["electronics"], False
    elif cat in cats:
        days, open_ended = cats[cat], False
    progs = [m.lower() for m in memberships]
    for prog, d in p.get("member_days", {}).items():
        if any(prog in m for m in progs) and days is not None:
            days = max(days, d)
    for prog, extra in p.get("member_extra_days", {}).items():
        if any(prog in m for m in progs) and days is not None:
            days += extra
    return ReturnWindow(merchant_id, days, open_ended, p.get("notes", ""))


@dataclass
class ReturnReminder:
    merchant: str
    item: str
    bought: date
    deadline: date
    days_left: int
    priority: str      # return_now | soon | open
    why: str
    request_id: str | None = None

    def to_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.__dict__.items()}


def reminders(purchases: list[dict], today: date, soon_days: int = SOON_DAYS) -> list[ReturnReminder]:
    """purchases: dicts with merchant, merchant_id, item, bought (date), category, memberships,
    verdict, request_id. Returns open reminders, most urgent first."""
    out = []
    for p in purchases:
        w = window(p.get("merchant_id"), p.get("category"), p.get("memberships", []))
        dl = w.deadline(p["bought"])
        if dl is None or dl < today:
            continue
        left = (dl - today).days
        if p.get("verdict") in ("rejected", "disputed"):
            prio, why = "return_now", f"you {p['verdict']} it and can still return it ({w.label()})"
        elif left <= soon_days:
            prio, why = "soon", f"return window closes in {left} day(s)"
        else:
            continue
        out.append(ReturnReminder(p["merchant"], p.get("item") or "(item not recorded)", p["bought"], dl, left,
                                  prio, why, p.get("request_id")))
    order = {"return_now": 0, "soon": 1}
    return sorted(out, key=lambda r: (order[r.priority], r.days_left))
