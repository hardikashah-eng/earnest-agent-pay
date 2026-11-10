"""After the purchase (spec/SPEC.md section 3f): what needs the user's attention?

- outcome:     bought a week ago, no verdict yet. Verdicts are what trust is built on.
- delivery:    10+ days and no matching order or receipt: did it arrive?
- price_drop:  the same item is now 5%+ cheaper at the same store while it can still be
               returned: return and rebuy, or ask for a price adjustment.
- renewal:     a subscription the agent bought renews within 5 days: still using it?
- returns:     see returns.py (rejected items that can still go back, windows closing soon).

Kept-past-the-window purchases can be accepted automatically (profile `auto_accept_kept`),
so trust grows without the user having to report every good outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

OUTCOME_AFTER_DAYS = 7
DELIVERY_AFTER_DAYS = 10
PRICE_DROP = 0.05
RENEWAL_EVERY_DAYS = 30
RENEWAL_NOTICE_DAYS = 5


@dataclass
class FollowUp:
    kind: str
    priority: int        # 0 = most urgent
    message: str
    request_id: str | None = None
    saving: float | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def purchase_followups(p: dict, today: date, later_prices: list[float], return_deadline: date | None) -> list[FollowUp]:
    """p: an approved earnest purchase dict (request_id, item, merchant, amount, quantity, bought,
    verdict, verification, recurring)."""
    out = []
    age = (today - p["bought"]).days
    what = p.get("item") or f"${p['amount']:.2f} at {p['merchant']}"
    if p.get("verdict") is None and age >= OUTCOME_AFTER_DAYS:
        out.append(FollowUp("outcome", 2, f"How did {what} work out? Your verdict sets the agent's trust.",
                            p["request_id"]))
    if (p.get("verdict") is None and age >= DELIVERY_AFTER_DAYS
            and p.get("verification") in (None, "pending", "unverified")):
        out.append(FollowUp("delivery", 1, f"No order or receipt found for {what} after {age} days: did it arrive?",
                            p["request_id"]))
    unit = p["amount"] / max(1, p.get("quantity", 1))
    if later_prices and return_deadline and today <= return_deadline and p.get("verdict") != "rejected":
        low = min(later_prices)
        if low <= unit * (1 - PRICE_DROP):
            save = round((unit - low) * max(1, p.get("quantity", 1)), 2)
            out.append(FollowUp("price_drop", 1,
                                f"{what} is now ${low:.2f} at {p['merchant']} (you paid ${unit:.2f}). Return and "
                                f"rebuy or ask for a price adjustment by {return_deadline.isoformat()}.",
                                p["request_id"], save))
    if p.get("recurring"):
        k = max(1, -(-age // RENEWAL_EVERY_DAYS))
        renews = p["bought"] + timedelta(days=RENEWAL_EVERY_DAYS * k)
        if 0 <= (renews - today).days <= RENEWAL_NOTICE_DAYS:
            out.append(FollowUp("renewal", 1, f"{what} renews on {renews.isoformat()}. Still using it?",
                                p["request_id"]))
    return out


def should_auto_accept(p: dict, today: date, return_deadline: date | None) -> bool:
    """Verified, no complaint, and the return window has closed (or 30 days with no known window)."""
    if p.get("verdict") is not None or p.get("verification") != "verified":
        return False
    cutoff = return_deadline or (p["bought"] + timedelta(days=30))
    return today > cutoff
