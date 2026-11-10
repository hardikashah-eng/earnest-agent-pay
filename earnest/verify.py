"""Verify that an agent bought what it said it would (spec/SPEC.md section 3d).

The confidence score trusts the `item` an agent names. Without a check, an agent could claim
"AA batteries" at a verified store, get the routine-repeat pass, and buy a gift card. Two
checks close that:

1. At checkout: when the merchant's cart line items are known (ACP / UCP checkout data, an
   AP2 cart mandate, or a card network's line-item data), the charge is declined unless the
   cart contains the claimed item and little else.
2. After the fact: when the order shows up in the user's order history or receipts, it's
   reconciled against the claim. A mismatch is a violation and suspends the agent's
   routine-repeat pass.

Pure functions, no I/O.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

from .history import SIMILAR_ITEM, item_similarity

NEUTRAL_LINE = re.compile(r"\b(tax|taxes|shipping|delivery|handling|fee|fees|tip|discount|coupon)\b", re.I)
OTHER_ITEMS_TOLERANCE = 0.10   # unclaimed items may make up at most 10% of the charge
ORDER_WINDOW_DAYS = (-1, 7)    # an order this many days before/after the request can match it
AMOUNT_TOLERANCE = 0.15        # an order this close in price, with a different item, is suspicious
GRACE_DAYS = 14                # after this long with no matching order, give up (no penalty)
MISMATCH_SUSPENDS_DAYS = 90    # how long a mismatch suspends the routine-repeat pass


@dataclass
class LineItem:
    item: str
    amount: float          # line total
    quantity: int = 1
    product_id: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "LineItem":
        return cls(str(d["item"]), float(d["amount"]), int(d.get("quantity", 1)), d.get("product_id"))


def item_matches(claim: str, item: str, claim_id: str | None = None, item_id: str | None = None) -> bool:
    if claim_id and item_id:
        return claim_id.strip().upper() == item_id.strip().upper()
    return item_similarity(claim, item) >= SIMILAR_ITEM


def check_cart(claim: str, line_items: list[LineItem], claim_id: str | None = None) -> tuple[bool, str]:
    """Does this cart contain the claimed item, and not much else?"""
    goods = [li for li in line_items if not NEUTRAL_LINE.search(li.item)]
    if not goods:
        return False, "item_mismatch:empty_cart"
    matched = [li for li in goods if item_matches(claim, li.item, claim_id, li.product_id)]
    if not matched:
        return False, "item_mismatch:not_in_cart"
    other = sum(li.amount for li in goods if li not in matched)
    total = sum(li.amount for li in goods)
    if total > 0 and other / total > OTHER_ITEMS_TOLERANCE:
        return False, "item_mismatch:extra_items"
    return True, "item_verified"


@dataclass(frozen=True)
class OrderLine:
    item: str
    total: float
    on: date
    product_id: str | None = None


def reconcile(claim: str, amount: float, requested_on: date, orders_here: list[OrderLine],
              today: date, claim_id: str | None = None) -> tuple[str, str]:
    """Match an approved request with orders from the same merchant.
    Returns (status, detail): verified | mismatch | pending | unverified."""
    lo = requested_on + timedelta(days=ORDER_WINDOW_DAYS[0])
    hi = requested_on + timedelta(days=ORDER_WINDOW_DAYS[1])
    window = [o for o in orders_here if lo <= o.on <= hi]
    for o in window:
        if item_matches(claim, o.item, claim_id, o.product_id):
            return "verified", o.item
    for o in window:
        if amount and abs(o.total - amount) <= AMOUNT_TOLERANCE * amount:
            return "mismatch", o.item
    if today > requested_on + timedelta(days=GRACE_DAYS):
        return "unverified", "no matching order found"
    return "pending", "waiting for the order to show up"
