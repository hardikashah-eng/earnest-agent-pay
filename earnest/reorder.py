"""Reorder prediction and standing mandates (spec/SPEC.md section 3f).

From everything the user has bought (earnest purchases, order history, receipts), find items
bought regularly ("AA batteries about every 3.5 months") and predict the next one. The user
can turn a prediction into a **standing mandate**: a pre-approved repeat purchase that an agent
may make without asking each time, but only for that item, at or under a unit price, and not
more often than an interval. That is the "human not present" case AP2 v0.2 added.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median

from .history import PastPurchase, match_level

MIN_PURCHASES = 3


@dataclass
class Prediction:
    item: str
    product_id: str | None
    purchases: int
    every_days: int
    regularity: float          # 0..1, how steady the interval is
    last: date
    next_due: date
    typical_price: float
    usual_merchant: str

    def to_dict(self) -> dict:
        return {k: (v.isoformat() if isinstance(v, date) else v) for k, v in self.__dict__.items()}


def _clusters(past: list[PastPurchase]) -> list[list[PastPurchase]]:
    groups: list[list[PastPurchase]] = []
    for p in sorted((p for p in past if p.item and p.verdict not in ("rejected", "disputed")), key=lambda p: p.on):
        for g in groups:
            rep = g[0]
            if match_level(p.item, p.product_id, rep.item, rep.product_id) == "same":
                g.append(p)
                break
        else:
            groups.append([p])
    return groups


def predict(past: list[PastPurchase], merchant_name, today: date) -> list[Prediction]:
    """merchant_name: callable mapping a PastPurchase's merchant keys to a display name."""
    out = []
    for g in _clusters(past):
        days = sorted({p.on for p in g})
        if len(days) < MIN_PURCHASES:
            continue
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        every = median(gaps)
        if every <= 0:
            continue
        spread = median(abs(x - every) for x in gaps)
        regularity = round(max(0.0, 1 - spread / every), 2)
        last = days[-1]
        recent = sorted(g, key=lambda p: p.on)[-3:]
        merchant = Counter(merchant_name(p) for p in g).most_common(1)[0][0]
        rep = g[-1]
        out.append(Prediction(rep.item, next((p.product_id for p in reversed(g) if p.product_id), None),
                              len(days), int(round(every)), regularity, last,
                              last + timedelta(days=int(round(every))),
                              round(median(p.amount for p in recent), 2), merchant))
    return sorted(out, key=lambda p: p.next_due)


def standing_checks(standing: dict, item: str | None, product_id: str | None, unit_price: float,
                    last_same_item: date | None, today: date) -> list[str]:
    """Deny reasons for a request against a standing mandate (empty = allowed)."""
    deny = []
    if not item or match_level(item, product_id, standing["item"], standing.get("product_id")) != "same":
        deny.append("standing:item_mismatch")
    if unit_price > float(standing["max_unit_price"]) + 1e-9:
        deny.append("standing:over_price")
    gap = int(standing.get("min_interval_days", 0))
    if last_same_item and gap and (today - last_same_item).days < gap:
        deny.append("standing:too_soon")
    return deny
