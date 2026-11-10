"""Purchase history as a confidence signal (spec/SPEC.md section 3b).

Before spending, the gateway looks at what the user has bought before:
- purchases made through earnest (merchant, item, amount, and the user's verdict), and
- imported card statements (merchant and amount only: statements don't list items).

Buying the same or a similar item from the same merchant again, at a similar price, raises
confidence. A past rejected or disputed purchase of that item there lowers it and makes the
gateway ask the user first. Pure functions, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from statistics import median

from .merchants import MerchantCheck

SAME_ITEM = 0.8      # token overlap (Jaccard) for "the same thing"
SIMILAR_ITEM = 0.4   # ... for "something like it"
PRICE_JUMP = 1.5     # paying this much more than last time is worth flagging

_STOP = {"a", "an", "the", "and", "or", "of", "for", "with", "in", "to", "by", "new", "pack", "set", "x"}
_BASE = {"verified": 0.6, "user_trusted": 0.7, "unknown": 0.3, "suspicious": 0.1, "untrusted": 0.0}


@dataclass(frozen=True)
class PastPurchase:
    merchant_keys: frozenset[str]
    amount: float
    on: date
    source: str                  # "earnest" | "statement"
    item: str | None = None
    verdict: str | None = None   # accepted | rejected | disputed | None (no outcome yet)


def item_tokens(item: str) -> set[str]:
    out = set()
    for t in re.findall(r"[a-z0-9]+", item.lower()):
        if t in _STOP:
            continue
        if len(t) > 4 and t.endswith("ies"):
            t = t[:-3] + "y"           # batteries -> battery
        elif len(t) > 3 and t.endswith("s") and not t.endswith("ss"):
            t = t[:-1]                 # cables -> cable
        out.add(t)
    return out


def item_similarity(a: str, b: str) -> float:
    ta, tb = item_tokens(a), item_tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


@dataclass
class History:
    merchant_purchases: int = 0          # at this merchant, any source, not rejected/disputed
    from_statements: int = 0
    same_item: int = 0                   # same item, this merchant, accepted or no verdict yet
    similar_item: int = 0
    rejected_before: int = 0             # same/similar item here that the user rejected or disputed
    last_purchase: date | None = None
    typical_amount: float | None = None  # median at this merchant
    item_typical_amount: float | None = None
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"merchant_purchases": self.merchant_purchases, "from_statements": self.from_statements,
               "same_item": self.same_item, "similar_item": self.similar_item,
               "rejected_before": self.rejected_before}
        if self.last_purchase:
            out["last_purchase"] = self.last_purchase.isoformat()
        if self.typical_amount is not None:
            out["typical_amount"] = self.typical_amount
        if self.item_typical_amount is not None:
            out["item_typical_amount"] = self.item_typical_amount
        return out


def summarize(keys: set[str], past: list[PastPurchase], item: str | None, amount: float) -> History:
    h = History()
    here = [p for p in past if keys & p.merchant_keys]
    good = [p for p in here if p.verdict not in ("rejected", "disputed")]
    h.merchant_purchases = len(good)
    h.from_statements = sum(p.source == "statement" for p in good)
    if good:
        h.last_purchase = max(p.on for p in good)
        h.typical_amount = round(median(p.amount for p in good), 2)

    item_amounts = []
    if item:
        for p in here:
            if not p.item:
                continue
            sim = item_similarity(item, p.item)
            if sim < SIMILAR_ITEM:
                continue
            if p.verdict in ("rejected", "disputed"):
                h.rejected_before += 1
            elif sim >= SAME_ITEM:
                h.same_item += 1
                item_amounts.append(p.amount)
            else:
                h.similar_item += 1
        if item_amounts:
            h.item_typical_amount = round(median(item_amounts), 2)

    if h.merchant_purchases:
        h.signals.append(f"bought_here_before:{h.merchant_purchases}")
    if h.same_item:
        h.signals.append("same_item_bought_here")
    elif h.similar_item:
        h.signals.append("similar_item_bought_here")
    if h.rejected_before:
        h.signals.append("previous_purchase_rejected")
    if h.item_typical_amount and amount > PRICE_JUMP * h.item_typical_amount:
        h.signals.append("price_above_previous")
    return h


def confidence(check: MerchantCheck, h: History) -> dict:
    """0..1 confidence that this purchase is from a merchant the user would trust."""
    score = _BASE.get(check.status, 0.3)
    if check.status != "untrusted":
        if h.merchant_purchases:
            score += 0.15 + (0.05 if h.merchant_purchases >= 3 else 0)
        if h.same_item:
            score += 0.2
        elif h.similar_item:
            score += 0.1
        if h.rejected_before:
            score -= 0.3
        if "price_above_previous" in h.signals:
            score -= 0.1
    score = round(max(0.0, min(1.0, score)), 2)
    level = "high" if score >= 0.75 else "medium" if score >= 0.45 else "low"
    return {"level": level, "score": score, "merchant": check.to_dict(), "history": h.to_dict(),
            "signals": [*check.reasons, *h.signals]}
