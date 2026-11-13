"""Agent accounts (spec/SPEC.md section 3i): a balance per (user, agent) that caps what the agent
can ever spend, funded by an allowance, store gift cards or prepaid cards.

Funding sources:
- `allowance`: money you set aside for the agent in earnest's ledger. Each approved purchase
  becomes a single-use virtual card (the payment backend), and the amount is held from the
  allowance. earnest never holds funds: the allowance is a limit on your own card.
- `gift_card`: a store gift card you bought and loaded into the agent's own store account. It
  only works at that store, so the store enforces the merchant lock and the balance is a real
  hard cap. earnest keeps the last 4 digits and the balance, never the code.
- `prepaid`: a prepaid Visa / Mastercard. Works anywhere, so earnest's rules are the only
  merchant check.

Pure functions; the gateway supplies balances and tiers.
"""
from __future__ import annotations

from dataclasses import dataclass

from .tiers import Tier

KINDS = ("allowance", "gift_card", "prepaid")
ALLOWANCE = "allowance"
BAD_EVENTS = ("violation", "item_mismatch", "outcome_disputed")  # these freeze the account


@dataclass
class Funding:
    id: str
    kind: str
    balance: float
    merchant: str | None = None   # gift cards: the store
    last4: str | None = None
    label: str | None = None
    merchant_keys: frozenset[str] = frozenset()

    def to_dict(self) -> dict:
        out = {"id": self.id, "kind": self.kind, "balance": round(self.balance, 2)}
        for k in ("merchant", "last4", "label"):
            if getattr(self, k):
                out[k] = getattr(self, k)
        return out

    def describe(self) -> str:
        if self.kind == "gift_card":
            return f"{self.label or self.merchant} gift card ending {self.last4}"
        if self.kind == "prepaid":
            return f"{self.label or 'prepaid card'} ending {self.last4}"
        return "allowance"


def ceiling(tiers: list[Tier]) -> float:
    """The most the allowance may hold: the highest per-purchase cap among the agent's tiers
    (one per task type it has a score for). New agents get the `new` tier's cap."""
    return max((t.cap for t in tiers), default=0.0)


def effective_ceiling(approved: float | None, tier_ceiling: float) -> float:
    """Decreases are automatic (the tier falls, the ceiling falls); increases need your OK."""
    return tier_ceiling if approved is None else min(approved, tier_ceiling)


def raise_offer(approved: float | None, tier_ceiling: float) -> dict | None:
    if approved is not None and tier_ceiling > approved + 1e-9:
        return {"from": approved, "to": tier_ceiling}
    return None


def pick_funding(sources: list[Funding], merchant_keys: set[str], amount: float) -> Funding | None:
    """Which source pays: a gift card for this store first (the store enforces the lock), then
    the allowance (a merchant-locked single-use card), then a prepaid card. Among cards of the
    same kind, the smallest balance that covers the amount, so balances get used up."""
    def covers(f: Funding) -> bool:
        return f.balance + 1e-9 >= amount
    gift = [f for f in sources if f.kind == "gift_card" and merchant_keys & f.merchant_keys and covers(f)]
    allowance = [f for f in sources if f.kind == "allowance" and covers(f)]
    prepaid = [f for f in sources if f.kind == "prepaid" and covers(f)]
    for group in (gift, allowance, prepaid):
        if group:
            return min(group, key=lambda f: (f.balance, f.id))
    return None


def usable_for(sources: list[Funding], merchant_keys: set[str]) -> float:
    """The most any single source could pay at this merchant."""
    best = 0.0
    for f in sources:
        if f.kind == "gift_card" and not merchant_keys & f.merchant_keys:
            continue
        best = max(best, f.balance)
    return round(best, 2)
