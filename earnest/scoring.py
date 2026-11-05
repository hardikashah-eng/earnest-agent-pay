"""Trust scoring (spec/SPEC.md section 4). Pure functions, no I/O."""
from __future__ import annotations

from datetime import datetime

from .models import Mandate, Outcome, Profile

PURCHASE_APPROVED = 2.0
ASKED_FIRST = 1.0
VIOLATION = -10.0
VERDICT_POINTS = {"accepted": 3.0, "rejected": -5.0, "disputed": -15.0}
HALF_LIFE_DAYS = 90.0


def decay(score: float, last: datetime, now: datetime, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """Scores drift toward zero, so old behavior counts less over time."""
    days = max(0.0, (now - last).total_seconds() / 86400)
    return score * 0.5 ** (days / half_life_days)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def outcome_events(outcome: Outcome, amount: float, mandate: Mandate,
                   profile: Profile) -> list[tuple[str, float]]:
    """Events (kind, points) earned from the user's verdict on a purchase."""
    events = [(f"outcome_{outcome.verdict}", VERDICT_POINTS[outcome.verdict])]
    if outcome.verdict != "accepted":
        return events  # never score efficiency without an accepted outcome

    ps = profile.price_sensitivity
    if mandate.mode == "gift":
        if mandate.budget_min is not None and mandate.budget_min <= amount <= mandate.budget_max:
            events.append(("budget_fit", 1.0))
    elif mandate.benchmark_price:
        saving = (mandate.benchmark_price - amount) / mandate.benchmark_price
        pts = round(_clamp(4 * saving * ps, -3, 3), 2)
        if pts:
            events.append(("efficiency", pts))

    timeliness = (1.0 if outcome.on_time else -2.0) * (1 - ps)
    if round(timeliness, 2):
        events.append(("on_time" if outcome.on_time else "late", round(timeliness, 2)))
    return events
