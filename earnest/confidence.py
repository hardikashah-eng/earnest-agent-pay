"""Purchase confidence (spec/SPEC.md section 3c). Pure functions, no I/O.

Every purchase gets a 0..1 score from four factors, each scored 0..1 and explained:

    authenticity  0.35  is the merchant real and one the user deals with?
    price         0.25  is the price in range of what the user paid for this item
                        (or similar items) in the last year?
    familiarity   0.25  has the user bought this or something like it, or shown interest?
    budget        0.15  does the mandate's remaining budget comfortably allow it?

An untrusted merchant scores 0 overall. The gateway escalates anything below the user's
`min_confidence`, and lets a high-confidence routine repeat purchase skip the
approval-threshold check (never the tier cap).
"""
from __future__ import annotations

from statistics import median

from .history import History
from .merchants import MerchantCheck

WEIGHTS = {"authenticity": 0.35, "price": 0.25, "familiarity": 0.25, "budget": 0.15}
SAME_PRICE_TOLERANCE = 1.10      # up to 10% over the highest recent price is "in range"
SIMILAR_PRICE_TOLERANCE = 1.25
ROUTINE_REPEAT_MIN = 0.8


def _level(score: float) -> str:
    return "high" if score >= 0.75 else "medium" if score >= 0.5 else "low"


def authenticity(check: MerchantCheck, h: History) -> tuple[float, str]:
    if check.status == "untrusted":
        return 0.0, "untrusted merchant"
    if check.status == "suspicious":
        return 0.15, "suspicious merchant"
    if check.status in ("verified", "user_trusted"):
        if h.memberships:
            return 1.0, f"{check.status} merchant, user is a member ({', '.join(h.memberships)})"
        return 0.9, f"{check.status} merchant"
    # unknown merchant: the user's own relationship with it is what counts
    if h.memberships:
        return 0.9, "user is a member here"
    if h.merchant_purchases:
        return 0.7, f"user has bought here {h.merchant_purchases} time(s)"
    return 0.4, "unknown merchant, no history"


def _over(amount: float, ceiling: float, slope: float = 1.5) -> float:
    """1.0 at or under the ceiling, falling as the price goes above it, floor 0.1."""
    if amount <= ceiling:
        return 1.0
    return max(0.1, round(1 - (amount / ceiling - 1) * slope, 2))


WAIT_PRICE_JUMP = 1.2    # paying 20%+ over the lowest recent price...
WAIT_MIN_DAYS = 14       # ...with two weeks or more to the deadline: suggest waiting


def price(unit_amount: float, h: History, benchmark: float | None, market=None,
          market_adjust: tuple[list[str], float | None] = ([], None),
          days_to_deadline: int | None = None) -> tuple[float, str, list[str]]:
    f, why, signals = _history_price(unit_amount, h, benchmark, market)
    m_signals, adjust = market_adjust
    signals += m_signals
    if adjust is not None and adjust < 0:
        f = min(f, -adjust)
        why += f"; {m_signals[0].split(':')[1]} has it for ${market.best_price:.2f}"
    elif adjust is not None and adjust > 0:
        f = max(f, adjust)
        why += "; best price among verified stores this week"
    if (h.same_item_prices and unit_amount > WAIT_PRICE_JUMP * min(h.same_item_prices)
            and (days_to_deadline is None or days_to_deadline >= WAIT_MIN_DAYS)):
        signals.append(f"consider_waiting:{min(h.same_item_prices):.2f}")
    return f, why, signals


def _history_price(unit_amount: float, h: History, benchmark: float | None,
                   market=None) -> tuple[float, str, list[str]]:
    signals: list[str] = []
    if h.same_item_prices:
        hi, med = max(h.same_item_prices), median(h.same_item_prices)
        f = _over(unit_amount, hi * SAME_PRICE_TOLERANCE)
        if f == 1.0:
            signals.append("price_in_range")
            why = f"${unit_amount:.2f} vs ${min(h.same_item_prices):.2f}-${hi:.2f} paid for the same item"
        else:
            why = f"${unit_amount:.2f} is {unit_amount / med:.1f}x the ${med:.2f} usually paid for this item"
            if unit_amount > 1.5 * med:
                signals.append("price_above_previous")
        return f, why, signals
    if h.similar_item_prices:
        med = median(h.similar_item_prices)
        f = min(0.85, _over(unit_amount, med * SIMILAR_PRICE_TOLERANCE, slope=1.0))
        if f >= 0.85:
            signals.append("price_in_similar_range")
        return f, f"${unit_amount:.2f} vs ~${med:.2f} for similar items", signals
    if h.interest and h.interest.max_price:
        ok = unit_amount <= h.interest.max_price
        return (0.9 if ok else 0.3), f"user said up to ${h.interest.max_price:.2f}", signals
    if market is not None:
        return (min(0.85, _over(unit_amount, market.median_price * 1.1)),
                f"vs ${market.median_price:.2f} median at verified stores this week", signals)
    if benchmark:
        return min(0.8, _over(unit_amount, benchmark * 1.1)), f"vs mandate benchmark ${benchmark:.2f}", signals
    return 0.5, "no price reference", ["no_price_reference"]


def familiarity(h: History, item: str | None, intent_match: bool) -> tuple[float, str]:
    if h.rejected_before:
        return 0.0, "user rejected this item from this merchant before"
    if h.same_item:
        return 1.0, "user bought this item here before"
    if h.similar_item or h.same_item_elsewhere:
        return 0.75, "user bought this item elsewhere" if h.same_item_elsewhere else "user bought a similar item here"
    if h.interest:
        return 0.8, f"user showed interest ({h.interest.source}: {h.interest.item})"
    if intent_match:
        return 0.55, "item matches the mandate's intent"
    if h.merchant_purchases:
        return 0.5, "user shops here, item is new"
    return 0.3, "no purchase history or interest" if item else "no item given"


def budget(amount: float, budget_max: float | None, spent: float,
           budget_min: float | None = None) -> tuple[float, str]:
    if budget_max is None:
        return 0.5, "no mandate"
    remaining = budget_max - spent
    if amount > remaining + 1e-9:
        return 0.0, f"over remaining budget ${remaining:.2f}"
    if budget_min is not None and budget_min <= amount:
        return 1.0, f"within ${budget_min:.2f}-${budget_max:.2f} range"
    share = amount / remaining if remaining else 1.0
    f = 1.0 if share <= 0.5 else round(1.0 - (share - 0.5) * 0.8, 2)
    return f, f"uses {share:.0%} of remaining ${remaining:.2f}"


def score(check: MerchantCheck, h: History, *, amount: float, quantity: int = 1,
          item: str | None = None, intent_match: bool = False, benchmark: float | None = None,
          budget_max: float | None = None, spent: float = 0.0, budget_min: float | None = None,
          extra_signals: list[str] = (), weights: dict[str, float] | None = None, market=None,
          market_adjust: tuple[list[str], float | None] = ([], None),
          days_to_deadline: int | None = None) -> dict:
    weights = weights or WEIGHTS
    unit = amount / max(1, quantity)
    a, a_why = authenticity(check, h)
    p, p_why, p_sig = price(unit, h, benchmark, market, market_adjust, days_to_deadline)
    f, f_why = familiarity(h, item, intent_match)
    b, b_why = budget(amount, budget_max, spent, budget_min)
    factors = {"authenticity": {"score": a, "why": a_why}, "price": {"score": p, "why": p_why},
               "familiarity": {"score": f, "why": f_why}, "budget": {"score": b, "why": b_why}}
    total = 0.0 if check.status == "untrusted" else sum(weights[k] * v["score"] for k, v in factors.items())
    total = round(total, 2)
    signals = [*check.reasons, *h.signals, *p_sig, *extra_signals]
    routine = (h.same_item > 0 and "price_in_range" in signals and check.status != "suspicious"
               and a >= 0.7 and total >= ROUTINE_REPEAT_MIN)
    if routine:
        signals.append("routine_repeat_purchase")
    out = {"level": _level(total), "score": total, "factors": factors, "weights": dict(weights),
           "weights_source": "default" if weights is WEIGHTS else "learned",
           "merchant": check.to_dict(), "history": h.to_dict(), "signals": signals}
    if market is not None:
        out["market"] = market.to_dict()
    return out
