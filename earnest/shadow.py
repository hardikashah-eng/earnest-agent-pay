"""Shadow mode (spec/SPEC.md section 3j): an agent works read-only against one of your cards.

- It learns your preferences from that card's history: where you shop, what you usually spend,
  what you buy regularly (`card_profile`).
- Its spend requests get the real decision but never a credential: status `shadow`, no money,
  no trust points from the decision itself.
- When your card later shows you bought what it proposed, that's a match (`match`).
- You can say whether you'd have let it buy each proposal (`earnest shadow review`).
- `readiness` says when it has earned going live, and suggests a starting allowance.

Pure functions; the gateway supplies transactions and requests.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from statistics import median
from typing import Callable

MATCH_WINDOW_DAYS = 14        # a proposal matches a card purchase within this many days after it
MATCH_TOLERANCE = 0.15        # amounts within 15% (or $2) count as the same purchase
MATCH_POINTS = 1.0            # trust points per matched proposal...
MATCH_POINTS_CAP = 15.0       # ...up to this many in total, so shadow mode alone stays below `established`
READY_REVIEWED = 10           # proposals you've reviewed before graduation is suggested
READY_AGREEMENT = 0.9         # share of reviewed proposals where earnest agreed with you
READY_MATCHED = 5             # or this many proposals that turned out to be real purchases


def _pct(values: list[float], q: float) -> float:
    s = sorted(values)
    if not s:
        return 0.0
    i = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[i]


def _nice(x: float) -> float:
    """Round a suggested dollar amount to something a person would type."""
    if x <= 0:
        return 0.0
    step = 5 if x < 100 else 25 if x < 500 else 100
    return float(max(step, round(x / step) * step))


@dataclass
class CardTx:
    id: str
    on: date
    merchant: str
    category: str
    amount: float


def card_profile(txs: list[CardTx], name: Callable[[str], str] = lambda m: m) -> dict:
    """What a card's history says about how you spend. Read-only suggestions; nothing is applied.
    `name` groups merchant spellings (the gateway passes the verified-merchant name)."""
    if not txs:
        return {"transactions": 0}
    amounts = [t.amount for t in txs]
    by_month: dict[str, float] = defaultdict(float)
    for t in txs:
        by_month[t.on.strftime("%Y-%m")] += t.amount
    by_merchant: dict[str, list[CardTx]] = defaultdict(list)
    for t in txs:
        by_merchant[name(t.merchant)].append(t)
    cats = Counter()
    for t in txs:
        cats[t.category] += t.amount
    total = sum(amounts)

    merchants = []
    for m, ts in sorted(by_merchant.items(), key=lambda kv: (-len(kv[1]), -sum(t.amount for t in kv[1]))):
        dates = sorted(t.on for t in ts)
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        merchants.append({
            "merchant": m, "purchases": len(ts), "typical": round(median(t.amount for t in ts), 2),
            "largest": round(max(t.amount for t in ts), 2), "last": dates[-1].isoformat(),
            "every_days": round(median(gaps)) if len(gaps) >= 2 else None})
    regulars = [m for m in merchants if m["purchases"] >= 3 and m["every_days"]]
    p75, p90 = _pct(amounts, 0.75), _pct(amounts, 0.9)
    return {
        "transactions": len(txs), "first": min(t.on for t in txs).isoformat(),
        "last": max(t.on for t in txs).isoformat(), "months": len(by_month),
        "monthly_spend": {"median": round(median(by_month.values()), 2), "max": round(max(by_month.values()), 2)},
        "purchase_size": {"median": round(median(amounts), 2), "p75": round(p75, 2), "p90": round(p90, 2),
                          "largest": round(max(amounts), 2)},
        "categories": [{"category": c, "share": round(v / total, 2)} for c, v in cats.most_common(6)],
        "merchants": merchants[:10],
        "regulars": regulars[:10],
        "suggested": {
            # most of your own purchases would go through without asking; bigger ones would ask
            "ask_me_over": _nice(p75),
            "monthly_budget": _nice(median(by_month.values())),
            "starting_allowance": _nice(p90),
            "known_merchants": [m["merchant"] for m in merchants if m["purchases"] >= 2][:20],
            "standing_restocks": [{"merchant": m["merchant"], "max_price": _nice(m["largest"]),
                                   "every_days": m["every_days"]} for m in regulars[:5]],
        },
    }


def amounts_match(proposed: float, actual: float) -> bool:
    return abs(proposed - actual) <= max(2.0, MATCH_TOLERANCE * proposed)


def match(proposal_on: date, proposal_keys: set[str], amount: float, txs: list[CardTx],
          keys_of: Callable[[str], set[str]], taken: set[str]) -> CardTx | None:
    """The earliest unclaimed card purchase at the same store, for about the same amount,
    on the day of the proposal or within MATCH_WINDOW_DAYS after it."""
    end = proposal_on + timedelta(days=MATCH_WINDOW_DAYS)
    for t in txs:
        if t.id in taken or not (proposal_on <= t.on <= end):
            continue
        if proposal_keys & keys_of(t.merchant) and amounts_match(amount, t.amount):
            return t
    return None


def agreement(would_be: str, would_buy: bool) -> str:
    """How earnest's shadow decision compares with your answer: agree, disagree or asked."""
    if would_be == "escalated":
        return "asked"
    return "agree" if (would_be == "approved") == would_buy else "disagree"


def readiness(reviewed: int, agreed: int, disagreed: int, matched: int, violations: int) -> dict:
    """Has the agent earned going live? Never with a would-be violation in the last 30 days."""
    rate = agreed / (agreed + disagreed) if agreed + disagreed else None
    reasons = []
    ready = False
    if violations:
        reasons.append(f"{violations} proposal(s) earnest would have denied as outside your rules")
    elif reviewed >= READY_REVIEWED and rate is not None and rate >= READY_AGREEMENT:
        ready = True
        reasons.append(f"earnest agreed with you on {agreed} of {agreed + disagreed} proposals")
    elif matched >= READY_MATCHED:
        ready = True
        reasons.append(f"{matched} of its proposals were things you went on to buy")
    else:
        need = []
        if reviewed < READY_REVIEWED:
            need.append(f"review {READY_REVIEWED - reviewed} more proposal(s)")
        if matched < READY_MATCHED:
            need.append(f"or {READY_MATCHED - matched} more that match real purchases")
        reasons.append("not yet: " + " ".join(need))
    return {"ready": ready, "agreement": round(rate, 2) if rate is not None else None, "why": reasons}
