"""Opt-in shared merchant reputation (spec/SPEC.md section 3h).

Each user's earnest knows, per merchant, how many purchases went fine and how many went wrong
(item mismatch, rejected or disputed, never verified). Users who opt in share a *contribution*:
those two counts per merchant with Laplace noise added on their own machine (local
differential privacy, epsilon per count), under a random one-time contribution id. No user id,
no items, no amounts, no dates.

An aggregator (any machine; a folder of contribution files is enough) sums contributions and
publishes only merchants with at least K contributors. Users import the feed:
- an unknown merchant with a high problem rate becomes **suspicious** (escalated);
- an unknown merchant with a long clean record gets a modest authenticity boost;
- verified merchants only get a signal.
"""
from __future__ import annotations

import math
import random
import secrets
from datetime import datetime

K_ANONYMITY = 5
MIN_CONTRIBUTORS = 10        # before the feed changes any decision
BAD_RATE, GOOD_RATE = 0.30, 0.05
GOOD_MIN_PURCHASES = 50


def _laplace(scale: float, rng: random.Random) -> float:
    u = rng.random() - 0.5
    return -scale * math.copysign(1, u) * math.log(1 - 2 * abs(u))


def contribution(stats: dict[str, dict], epsilon: float = 1.0, rng: random.Random | None = None) -> dict:
    """stats: {merchant: {"purchases": n, "problems": m}} -> a noisy, anonymous contribution."""
    rng = rng or random.SystemRandom()
    out = {}
    for merchant, s in stats.items():
        purchases = max(0, round(s["purchases"] + _laplace(1 / epsilon, rng)))
        problems = min(purchases, max(0, round(s["problems"] + _laplace(1 / epsilon, rng))))
        if purchases:
            out[merchant] = {"purchases": purchases, "problems": problems}
    return {"v": 1, "contribution_id": secrets.token_hex(8), "epsilon": epsilon, "merchants": out}


def aggregate(contributions: list[dict], k: int = K_ANONYMITY, now: datetime | None = None) -> dict:
    seen, totals = set(), {}
    for c in contributions:
        if c.get("contribution_id") in seen:
            continue
        seen.add(c.get("contribution_id"))
        for m, s in c.get("merchants", {}).items():
            t = totals.setdefault(m, {"contributors": 0, "purchases": 0, "problems": 0})
            t["contributors"] += 1
            t["purchases"] += int(s["purchases"])
            t["problems"] += int(s["problems"])
    merchants = {m: {**t, "problem_rate": round(t["problems"] / t["purchases"], 3) if t["purchases"] else 0.0}
                 for m, t in totals.items() if t["contributors"] >= k}
    return {"v": 1, "generated": (now or datetime.now()).isoformat(), "k": k, "contributions": len(seen),
            "merchants": merchants}


def judge(entry: dict | None) -> str | None:
    """'bad', 'good' or None for one feed entry."""
    if not entry or entry["contributors"] < MIN_CONTRIBUTORS:
        return None
    if entry["problem_rate"] >= BAD_RATE:
        return "bad"
    if entry["problem_rate"] <= GOOD_RATE and entry["purchases"] >= GOOD_MIN_PURCHASES:
        return "good"
    return None
