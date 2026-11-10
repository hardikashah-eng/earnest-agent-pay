"""Purchase history (spec/SPEC.md section 3b).

Before spending, the gateway looks at what the user has bought before, from three sources:
- earnest purchases: merchant, item, amount and the user's verdict;
- retailer order history (Amazon, Costco, Walmart, Target ... exports): merchant, item,
  unit price, date;
- imported card statements: merchant and amount only (statements don't list items).

It also uses what the user has *shown interest in* (wishlist, cart, an interest they told
earnest about) and their store memberships (Prime, Costco, Walmart+, Target Circle).

Pure functions, no I/O. The weighted score built from this lives in confidence.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median

SAME_ITEM = 0.8      # token overlap (Jaccard) for "the same thing"
SIMILAR_ITEM = 0.4   # ... for "something like it"
PRICE_LOOKBACK_DAYS = 365   # prices older than this aren't a fair reference
RECENT_PRICES = 5           # use at most the latest N prices for the range

_STOP = {"a", "an", "the", "and", "or", "of", "for", "with", "in", "to", "by", "new", "pack", "set", "x"}


@dataclass(frozen=True)
class PastPurchase:
    merchant_keys: frozenset[str]
    amount: float                # per unit when the source knows the quantity
    on: date
    source: str                  # "earnest" | "order" | "statement"
    item: str | None = None
    verdict: str | None = None   # accepted | rejected | disputed | None (no outcome / not earnest)
    product_id: str | None = None


@dataclass(frozen=True)
class Interest:
    item: str
    merchant_keys: frozenset[str] = frozenset()   # empty = any merchant
    max_price: float | None = None
    source: str = "user"                          # user | wishlist | cart


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
    jaccard = len(ta & tb) / len(ta | tb)
    # Retail titles add brand and filler words ("Amazon Basics AA Batteries, 24 Pack"). If
    # every word of a specific description (3+ words, at least half the other) appears in
    # the other title, treat it as the same item.
    small, large = sorted((ta, tb), key=len)
    if len(small) >= 3 and small <= large and len(small) / len(large) >= 0.5:
        return max(jaccard, 0.85)
    return jaccard


@dataclass
class History:
    merchant_purchases: int = 0          # at this merchant, any source, not rejected/disputed
    from_statements: int = 0
    from_orders: int = 0
    same_item: int = 0                   # same item, this merchant, not rejected
    similar_item: int = 0
    same_item_elsewhere: int = 0         # same item bought from another merchant
    rejected_before: int = 0             # same/similar item here that the user rejected or disputed
    last_purchase: date | None = None
    last_same_item: date | None = None
    typical_amount: float | None = None  # median at this merchant
    same_item_prices: list[float] = field(default_factory=list)     # recent unit prices, any merchant
    similar_item_prices: list[float] = field(default_factory=list)
    interest: Interest | None = None
    memberships: list[str] = field(default_factory=list)            # programs at this merchant
    signals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {"merchant_purchases": self.merchant_purchases, "from_statements": self.from_statements,
               "from_orders": self.from_orders, "same_item": self.same_item,
               "similar_item": self.similar_item, "same_item_elsewhere": self.same_item_elsewhere,
               "rejected_before": self.rejected_before}
        if self.last_purchase:
            out["last_purchase"] = self.last_purchase.isoformat()
        if self.last_same_item:
            out["last_same_item"] = self.last_same_item.isoformat()
        if self.typical_amount is not None:
            out["typical_amount"] = self.typical_amount
        if self.same_item_prices:
            out["same_item_price_range"] = [min(self.same_item_prices), max(self.same_item_prices)]
        elif self.similar_item_prices:
            out["similar_item_price_range"] = [min(self.similar_item_prices), max(self.similar_item_prices)]
        if self.interest:
            out["interest"] = {"item": self.interest.item, "source": self.interest.source,
                               "max_price": self.interest.max_price}
        if self.memberships:
            out["memberships"] = self.memberships
        return out


def _recent(prices: list[tuple[date, float]], today: date) -> list[float]:
    cutoff = today - timedelta(days=PRICE_LOOKBACK_DAYS)
    keep = sorted(p for p in prices if p[0] >= cutoff)[-RECENT_PRICES:]
    return [round(a, 2) for _, a in keep]


def match_level(item: str, product_id: str | None, other_item: str, other_id: str | None) -> str | None:
    """'same', 'similar' or None. Product IDs, when both are known, decide 'same'."""
    sim = item_similarity(item, other_item)
    if product_id and other_id:
        if product_id == other_id:
            return "same"
        return "similar" if sim >= SIMILAR_ITEM else None
    if sim >= SAME_ITEM:
        return "same"
    return "similar" if sim >= SIMILAR_ITEM else None


def summarize(keys: set[str], past: list[PastPurchase], item: str | None, today: date,
              interests: list[Interest] = (), memberships: list[str] = (),
              product_id: str | None = None) -> History:
    h = History(memberships=list(memberships))
    here = [p for p in past if keys & p.merchant_keys]
    good = [p for p in here if p.verdict not in ("rejected", "disputed")]
    h.merchant_purchases = len(good)
    h.from_statements = sum(p.source == "statement" for p in good)
    h.from_orders = sum(p.source == "order" for p in good)
    if good:
        h.last_purchase = max(p.on for p in good)
        h.typical_amount = round(median(p.amount for p in good), 2)

    same_prices: list[tuple[date, float]] = []
    similar_prices: list[tuple[date, float]] = []
    if item:
        for p in past:
            if not p.item:
                continue
            level = match_level(item, product_id, p.item, p.product_id)
            if level is None:
                continue
            at_this_merchant = bool(keys & p.merchant_keys)
            if p.verdict in ("rejected", "disputed"):
                if at_this_merchant:
                    h.rejected_before += 1
                continue
            if level == "same":
                same_prices.append((p.on, p.amount))
                if at_this_merchant:
                    h.same_item += 1
                    h.last_same_item = max(h.last_same_item or p.on, p.on)
                else:
                    h.same_item_elsewhere += 1
            else:
                similar_prices.append((p.on, p.amount))
                if at_this_merchant:
                    h.similar_item += 1
        h.same_item_prices = _recent(same_prices, today)
        h.similar_item_prices = _recent(similar_prices, today)

        best = 0.0
        for i in interests:
            if i.merchant_keys and not (keys & i.merchant_keys):
                continue
            sim = item_similarity(item, i.item)
            if sim >= SIMILAR_ITEM and sim > best:
                best, h.interest = sim, i

    if h.merchant_purchases:
        h.signals.append(f"bought_here_before:{h.merchant_purchases}")
    if h.memberships:
        h.signals.append("member:" + ",".join(h.memberships))
    if h.same_item:
        h.signals.append("same_item_bought_here")
    elif h.similar_item:
        h.signals.append("similar_item_bought_here")
    elif h.same_item_elsewhere:
        h.signals.append("same_item_bought_elsewhere")
    if h.interest:
        h.signals.append(f"shown_interest:{h.interest.source}")
    if h.rejected_before:
        h.signals.append("previous_purchase_rejected")
    return h
