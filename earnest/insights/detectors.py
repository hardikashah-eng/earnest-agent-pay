"""Insight detectors. Each looks at transaction history up to the end of the report month
and returns Insights with an estimated monthly saving and a confidence.

Card statements show the store and total, not the items, so retailer-switch savings use a
rough relative price index (RETAILER_INDEX), not item prices. Item-level advice needs
receipt data (future work).
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from .statement import Transaction


@dataclass
class Insight:
    key: str              # stable id, used to dismiss ("I know about this")
    kind: str
    title: str
    detail: str
    action: str
    monthly_savings: float
    confidence: float     # 0..1

    @property
    def rank_score(self) -> float:
        return self.monthly_savings * self.confidence

    def to_dict(self) -> dict:
        return {"key": self.key, "kind": self.kind, "title": self.title, "detail": self.detail,
                "action": self.action, "monthly_savings": round(self.monthly_savings, 2),
                "confidence": self.confidence}


def month_bounds(month: str) -> tuple[date, date]:
    y, m = map(int, month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), m % 12 + 1, 1)
    return start, end  # [start, end)


def _in_month(txs, month):
    start, end = month_bounds(month)
    return [t for t in txs if start <= t.date < end]


def _money(x: float) -> str:
    return f"${x:,.2f}"


# ------------------------------------------------------------------ recurring charges

SUBSCRIPTION_CATEGORIES = {"streaming", "fitness", "other", "shopping"}


@dataclass
class Recurring:
    merchant: str
    category: str
    charges: list[Transaction]

    @property
    def first(self): return self.charges[0]
    @property
    def last(self): return self.charges[-1]


def find_recurring(txs: list[Transaction], as_of: date) -> list[Recurring]:
    """Same merchant, roughly monthly, similar amounts, still active."""
    by_merchant = defaultdict(list)
    for t in txs:
        if t.category in SUBSCRIPTION_CATEGORIES:
            by_merchant[t.merchant].append(t)
    out = []
    for merchant, charges in by_merchant.items():
        charges.sort(key=lambda t: t.date)
        if len(charges) < 3:
            continue
        gaps = [(b.date - a.date).days for a, b in zip(charges, charges[1:])]
        amounts = [t.amount for t in charges]
        monthly = all(24 <= g <= 38 for g in gaps)
        steady = statistics.pstdev(amounts) / statistics.mean(amounts) <= 0.2
        active = (as_of - charges[-1].date).days <= 40
        if monthly and steady and active:
            out.append(Recurring(merchant, charges[0].category, charges))
    return out


def detect_price_creep(txs, month) -> list[Insight]:
    _, end = month_bounds(month)
    out = []
    for r in find_recurring(txs, end):
        first, last = r.first.amount, r.last.amount
        if last >= first * 1.05 and last - first >= 1 and r.last.date >= month_bounds(month)[0]:
            out.append(Insight(
                key=f"price_creep:{r.merchant}", kind="price_creep",
                title=f"{r.merchant} went up from {_money(first)} to {_money(last)} a month",
                detail=f"Charged {len(r.charges)} times since {r.first.date:%b %d}; "
                       f"that's {_money((last - first) * 12)} more a year than when you started.",
                action="Check for a cheaper plan, an annual option, or ask about a retention offer.",
                monthly_savings=last - first, confidence=0.6))
    return out


def detect_overlapping_subscriptions(txs, month) -> list[Insight]:
    _, end = month_bounds(month)
    by_cat = defaultdict(list)
    for r in find_recurring(txs, end):
        by_cat[r.category].append(r)
    out = []
    for cat, subs in by_cat.items():
        if cat != "streaming" or len(subs) < 3:
            continue
        subs.sort(key=lambda r: r.last.amount)
        total = sum(r.last.amount for r in subs)
        names = ", ".join(f"{r.merchant} {_money(r.last.amount)}" for r in subs)
        # keep two, pause the rest (estimate conservatively: pause the cheapest ones)
        saving = sum(r.last.amount for r in subs[:-2])
        out.append(Insight(
            key=f"overlap:{cat}", kind="overlapping_subscriptions",
            title=f"{len(subs)} streaming subscriptions: {_money(total)} a month",
            detail=f"{names}. Most people actively use one or two at a time.",
            action="Rotate: keep two, pause the rest, and switch when you've finished a show.",
            monthly_savings=saving, confidence=0.5))
    return out


# ------------------------------------------------------------------ fees

FEE_ADVICE = [
    ("LATE", "Turn on autopay for at least the minimum payment."),
    ("FOREIGN", "Use a card with no foreign transaction fee when buying abroad or from overseas sites."),
    ("ANNUAL", "Ask the issuer to waive it or move you to a no-fee card."),
    ("INTEREST", "Pay the statement balance in full, or move the balance to a 0% card."),
    ("ATM", "Use in-network ATMs or get cash back at checkout."),
    ("OVERDRAFT", "Turn on low-balance alerts or overdraft protection from savings."),
]


def detect_fees(txs, month) -> list[Insight]:
    fees = [t for t in _in_month(txs, month) if t.category == "fees"]
    if not fees:
        return []
    total = sum(t.amount for t in fees)
    kinds = sorted({t.description for t in fees})
    advice = [a for word, a in FEE_ADVICE if any(word in t.description.upper() for t in fees)]
    return [Insight(
        key=f"fees:{month}", kind="fees",
        title=f"{_money(total)} in card fees this month",
        detail="; ".join(kinds),
        action=" ".join(advice) or "Call the issuer and ask for a one-time courtesy refund.",
        monthly_savings=total, confidence=0.85)]


# ------------------------------------------------------------------ retailer switch

# Relative cost of a typical grocery basket (1.0 = average). Rough and illustrative:
# replace with your region's numbers or real receipt data.
RETAILER_INDEX = {
    "Whole Foods": 1.25, "Safeway": 1.10, "Target": 1.00, "Kroger": 1.00,
    "Instacart": 1.20, "Trader Joe's": 0.90, "Costco": 0.85, "Walmart": 0.85, "Aldi": 0.75,
}


def detect_retailer_switch(txs, month) -> list[Insight]:
    month_txs = [t for t in _in_month(txs, month) if t.merchant in RETAILER_INDEX
                 and t.category in ("groceries", "delivery")]
    spend = defaultdict(float)
    for t in month_txs:
        spend[t.merchant] += t.amount
    if not spend:
        return []
    used = {m for m in spend}
    out = []
    for store, amount in spend.items():
        idx = RETAILER_INDEX[store]
        cheaper_used = [m for m in used if RETAILER_INDEX[m] < idx - 0.1]
        if cheaper_used:
            target = min(cheaper_used, key=RETAILER_INDEX.get)
            conf, why = 0.55, f"You already shop at {target}"
        else:
            target = min(RETAILER_INDEX, key=RETAILER_INDEX.get)
            conf, why = 0.35, f"{target} is typically cheaper"
        if RETAILER_INDEX[target] >= idx - 0.1:
            continue
        # assume about half of the spend could move
        saving = amount * 0.5 * (1 - RETAILER_INDEX[target] / idx)
        if saving < 15:
            continue
        out.append(Insight(
            key=f"switch:{store}->{target}", kind="retailer_switch",
            title=f"{_money(amount)} at {store} this month",
            detail=f"{why}; a typical basket there costs about "
                   f"{round((1 - RETAILER_INDEX[target] / idx) * 100)}% less (rough estimate, not item prices).",
            action=f"Move staples (pantry, dairy, produce basics) to {target}; keep specialty items at {store}.",
            monthly_savings=saving, confidence=conf))
    return out


# ------------------------------------------------------------------ delivery

def detect_delivery(txs, month) -> list[Insight]:
    orders = [t for t in _in_month(txs, month) if t.category == "delivery" and t.merchant != "Instacart"]
    if len(orders) < 4:
        return []
    total = sum(t.amount for t in orders)
    # delivery fees, service fees, tips and menu markups are commonly ~25-35% of the order
    saving = total * 0.30 * 0.5
    return [Insight(
        key=f"delivery:{month}", kind="delivery",
        title=f"{len(orders)} food delivery orders this month ({_money(total)})",
        detail="Fees, tips and menu markups usually add 25-35% over ordering for pickup.",
        action="Switch half of these to pickup, or check whether a delivery membership is cheaper than per-order fees.",
        monthly_savings=saving, confidence=0.45)]


# ------------------------------------------------------------------ spending spikes

def detect_spikes(txs, month) -> list[Insight]:
    start, _ = month_bounds(month)
    this = defaultdict(float)
    for t in _in_month(txs, month):
        this[t.category] += t.amount
    prior_months = []
    y, m = start.year, start.month
    for _ in range(3):
        y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        prior_months.append(f"{y:04d}-{m:02d}")
    history = defaultdict(list)
    for pm in prior_months:
        totals = defaultdict(float)
        month_txs = _in_month(txs, pm)
        if not month_txs:
            continue
        for t in month_txs:
            totals[t.category] += t.amount
        for cat in set(this) | set(totals):
            history[cat].append(totals.get(cat, 0.0))
    out = []
    for cat, amount in this.items():
        past = history.get(cat, [])
        if len(past) < 2 or cat in ("fees", "delivery"):  # these have their own detectors
            continue
        usual = statistics.mean(past)
        if usual > 0 and amount >= usual * 1.5 and amount - usual >= 75:
            top = sorted((t for t in _in_month(txs, month) if t.category == cat),
                         key=lambda t: -t.amount)[:3]
            out.append(Insight(
                key=f"spike:{cat}:{month}", kind="spike",
                title=f"{cat.title()} spending is {amount / usual:.1f}x your usual",
                detail=f"{_money(amount)} this month vs about {_money(usual)} normally. Biggest: "
                       + ", ".join(f"{t.merchant} {_money(t.amount)}" for t in top) + ".",
                action="Check for duplicate orders or returns you haven't sent back; set a monthly cap for this category.",
                monthly_savings=(amount - usual) * 0.3, confidence=0.4))
    return out


DETECTORS = [detect_fees, detect_price_creep, detect_overlapping_subscriptions,
             detect_retailer_switch, detect_delivery, detect_spikes]
