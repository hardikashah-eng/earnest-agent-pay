"""Trust tiers. Defaults mirror spec/trust_tiers.json (a test keeps them in sync)."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    name: str
    min_score: float | None  # None = anything below the next tier (restricted)
    cap: float
    lifetime_hours: float
    approval_over: float
    single_use: bool
    approve_new_merchants: bool

    def effective_approval_over(self, autonomy: float) -> float:
        """Profile autonomy scales the approval threshold (0.5 = unchanged)."""
        return self.approval_over * (0.5 + autonomy)


DEFAULT_TIERS: tuple[Tier, ...] = (
    Tier("trusted", 60, 1000, 168, 500, False, True),
    Tier("established", 20, 200, 24, 150, True, False),
    Tier("new", 0, 25, 1, 10, True, False),
    Tier("restricted", None, 0, 1, 0, True, False),
)


def tier_for(score: float, tiers: tuple[Tier, ...] = DEFAULT_TIERS) -> Tier:
    """Highest tier whose min_score the score meets; otherwise the floor tier."""
    for t in tiers:
        if t.min_score is not None and score >= t.min_score:
            return t
    return tiers[-1]
