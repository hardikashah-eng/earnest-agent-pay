"""Purchase evaluation (spec/SPEC.md section 3l): a weighted scoring rubric and a net-value check,
so a purchase is judged on numbers instead of on how convincing the agent sounds.

Four metrics, each scored 1-10:
- price: cost efficiency. Cost per use (price / lifetime uses) against what your hour is worth,
  nudged by how the price compares with the market or the mandate's benchmark.
- time_saved: opportunity cost. Hours saved per year x your hourly value, against the price.
  An item that pays for itself in time scores 7; twice over scores 10.
- utility: functional necessity. How often it's used and how well it solves a specific problem;
  a core function scores higher than a nice-to-have.
- added_value: intangible and secondary benefits: stress, health, skill, social, comfort, resale.
  Several areas score higher than one.

Weights (summing to 100) come from your goal: balanced, save_money (price 40%), save_time
(time 40%), or your own. The total is the weighted average on 1-10. You can set a threshold
(say 7.5): requests below it come to you. For larger purchases the net-value check applies:

    net value (1 year) = hours saved x hourly value + estimated added value (incl. resale) - price

The agent estimates the inputs when it asks; you can review the purchase afterwards with what
really happened. The gap between the two is how well the agent judges value.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .models import ValidationError

METRICS = ("price", "time_saved", "utility", "added_value")
GOALS = {
    "balanced": {"price": 30, "time_saved": 25, "utility": 30, "added_value": 15},
    "save_money": {"price": 40, "time_saved": 20, "utility": 25, "added_value": 15},
    "save_time": {"price": 20, "time_saved": 40, "utility": 25, "added_value": 15},
    "wellbeing": {"price": 20, "time_saved": 20, "utility": 25, "added_value": 35},
}
BENEFIT_AREAS = ("stress", "health", "skill", "social", "comfort")
DEFAULT_POLICY = {"goal": "balanced", "weights": GOALS["balanced"], "threshold": 7.5, "hourly_value": 30.0,
                  "gate": False, "net_check_over": 100.0, "min_coverage": 0.5}
INPUT_FIELDS = {"lifespan_months", "uses_per_month", "total_uses", "hours_saved_per_year", "solves", "core",
                "benefits", "resale_value", "added_value_usd", "scores", "note"}


def _clamp(x: float, lo: float = 1.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def _num(d: dict, k: str, lo: float = 0.0, hi: float | None = None) -> float | None:
    v = d.get(k)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or math.isnan(v):
        raise ValidationError(f"evaluation.{k} must be a number")
    if v < lo or (hi is not None and v > hi):
        raise ValidationError(f"evaluation.{k} must be between {lo:g} and {hi:g}" if hi is not None
                              else f"evaluation.{k} must be >= {lo:g}")
    return float(v)


@dataclass
class EvalInputs:
    """What the agent (or you, in a review) says about the purchase. All optional; metrics with
    no inputs are left out and the weights of the rest are scaled up."""
    lifespan_months: float | None = None
    uses_per_month: float | None = None
    total_uses: float | None = None          # instead of lifespan x uses_per_month (consumables, tickets)
    hours_saved_per_year: float | None = None
    solves: float | None = None              # 1-10: how well it solves a specific problem
    core: bool | None = None                 # a core, frequent function (vs. nice-to-have)
    benefits: dict[str, float] = field(default_factory=dict)   # area -> 0-3
    resale_value: float | None = None        # what it could sell for after a year
    added_value_usd: float | None = None     # intangible value over a year, in dollars
    scores: dict[str, float] = field(default_factory=dict)     # direct 1-10 scores, beat the inputs
    note: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "EvalInputs":
        d = d or {}
        if not isinstance(d, dict):
            raise ValidationError("evaluation must be an object")
        extra = set(d) - INPUT_FIELDS
        if extra:
            raise ValidationError(f"unknown evaluation field(s): {sorted(extra)}")
        benefits = d.get("benefits") or {}
        if not isinstance(benefits, dict) or set(benefits) - set(BENEFIT_AREAS):
            raise ValidationError(f"evaluation.benefits keys must be among {', '.join(BENEFIT_AREAS)}")
        scores = d.get("scores") or {}
        if not isinstance(scores, dict) or set(scores) - set(METRICS):
            raise ValidationError(f"evaluation.scores keys must be among {', '.join(METRICS)}")
        core = d.get("core")
        if core is not None and not isinstance(core, bool):
            raise ValidationError("evaluation.core must be true or false")
        return cls(
            lifespan_months=_num(d, "lifespan_months"), uses_per_month=_num(d, "uses_per_month"),
            total_uses=_num(d, "total_uses"), hours_saved_per_year=_num(d, "hours_saved_per_year"),
            solves=_num(d, "solves", 1, 10), core=core,
            benefits={k: _num(benefits, k, 0, 3) for k in benefits},
            resale_value=_num(d, "resale_value"), added_value_usd=_num(d, "added_value_usd"),
            scores={k: _num(scores, k, 1, 10) for k in scores},
            note=str(d["note"])[:300] if d.get("note") else None)

    def to_dict(self) -> dict:
        out = {}
        for k in sorted(INPUT_FIELDS):
            v = getattr(self, k)
            if v not in (None, {}, ""):
                out[k] = v
        return out

    def is_empty(self) -> bool:
        return not self.to_dict()


def normalize_weights(w: dict) -> dict[str, float]:
    if set(w) != set(METRICS):
        raise ValidationError(f"weights need exactly: {', '.join(METRICS)}")
    if any(not isinstance(v, (int, float)) or v < 0 for v in w.values()):
        raise ValidationError("weights must be numbers >= 0")
    total = sum(w.values())
    if total <= 0:
        raise ValidationError("weights must add up to more than 0")
    if abs(total - 100) > 0.5:
        raise ValidationError(f"weights must add up to 100%, not {total:g}")
    return {k: float(w[k]) for k in METRICS}


def _uses(i: EvalInputs) -> float | None:
    if i.total_uses is not None:
        return i.total_uses
    if i.lifespan_months is not None and i.uses_per_month is not None:
        return i.lifespan_months * i.uses_per_month
    return None


def price_score(price: float, i: EvalInputs, hourly: float, market_price: float | None) -> tuple[float | None, str]:
    uses = _uses(i)
    parts = []
    score = None
    if uses:
        cpu = price / uses
        ratio = cpu / max(hourly, 0.01)
        # cost per use at 1% of your hour -> 10, 10% -> 7, 100% -> 4, 10x -> 1
        score = _clamp(10 - 3 * math.log10(max(ratio, 1e-9) / 0.01))
        parts.append(f"${cpu:,.2f} per use over {uses:g} uses")
    elif uses == 0:
        score = 1.0
        parts.append("no expected uses")
    if market_price:
        delta = (market_price - price) / market_price
        if score is None:
            score = _clamp(5.5 + delta * 10)
        else:
            score = _clamp(score + max(-2.0, min(2.0, delta * 10)))
        parts.append(f"{'below' if delta >= 0 else 'above'} the ${market_price:,.2f} market price by "
                     f"{abs(delta):.0%}")
    return (round(score, 1) if score is not None else None), "; ".join(parts)


def time_score(price: float, i: EvalInputs, hourly: float) -> tuple[float | None, str]:
    if i.hours_saved_per_year is None:
        return None, ""
    value = i.hours_saved_per_year * hourly
    r = value / price if price else 0.0
    # pays for itself (r = 1) -> 7; twice over -> 10
    score = 1 + 6 * r if r <= 1 else 7 + 3 * min(1.0, r - 1)
    return round(_clamp(score), 1), (f"{i.hours_saved_per_year:g} h/yr x ${hourly:g}/h = ${value:,.0f} "
                                     f"({r:.1f}x the price)")


def utility_score(i: EvalInputs) -> tuple[float | None, str]:
    parts, vals = [], []
    if i.uses_per_month is not None:
        # daily (30/mo) -> 10, weekly -> ~6, monthly -> ~4, never -> 2
        vals.append(2 + 8 * math.log(1 + i.uses_per_month) / math.log(31))
        parts.append(f"used {i.uses_per_month:g}x a month")
    if i.solves is not None:
        vals.append(i.solves)
        parts.append(f"solves the problem {i.solves:g}/10")
    if not vals:
        return None, ""
    score = sum(vals) / len(vals)
    if i.core is not None:
        score += 1 if i.core else -1
        parts.append("core function" if i.core else "nice to have")
    return round(_clamp(score), 1), ", ".join(parts)


def added_value_score(price: float, i: EvalInputs) -> tuple[float | None, str]:
    areas = {k: v for k, v in i.benefits.items() if v and v > 0}
    if not areas and i.resale_value is None and i.added_value_usd is None:
        return None, ""
    score = 1 + min(6.0, 1.5 * sum(areas.values()))
    if len(areas) >= 2:
        score += 1   # helps several parts of life
    parts = [", ".join(f"{k} +{v:g}" for k, v in areas.items())] if areas else []
    if i.resale_value:
        score += min(2.0, 4 * i.resale_value / price)
        parts.append(f"resale ${i.resale_value:,.0f}")
    if i.added_value_usd:
        score += min(2.0, 2 * i.added_value_usd / price)
        parts.append(f"~${i.added_value_usd:,.0f} of other value")
    return round(_clamp(score), 1), "; ".join(parts)


def net_value(price: float, i: EvalInputs, hourly: float) -> float | None:
    """(hours saved x hourly value) + estimated added value - price, over one year."""
    if i.hours_saved_per_year is None and i.added_value_usd is None and i.resale_value is None:
        return None
    return round((i.hours_saved_per_year or 0) * hourly + (i.added_value_usd or 0) + (i.resale_value or 0) - price, 2)


def evaluate(price: float, inputs: EvalInputs, policy: dict, market_price: float | None = None) -> dict:
    """Score a purchase. Returns metric scores with reasons, the weighted total, coverage (the share
    of weight backed by inputs), net value and whether it passes your threshold and net check."""
    hourly = policy["hourly_value"]
    weights = policy["weights"]
    found = {
        "price": price_score(price, inputs, hourly, market_price),
        "time_saved": time_score(price, inputs, hourly),
        "utility": utility_score(inputs),
        "added_value": added_value_score(price, inputs),
    }
    metrics = {}
    for m in METRICS:
        s, why = found[m]
        if m in inputs.scores:
            s, why = inputs.scores[m], "scored directly"
        metrics[m] = {"score": s, "weight": weights[m], "why": why or "no inputs"}
    scored = {m: v for m, v in metrics.items() if v["score"] is not None}
    covered = sum(v["weight"] for v in scored.values())
    total = (sum(v["score"] * v["weight"] for v in scored.values()) / covered) if covered else None
    for v in metrics.values():
        v["weighted"] = round(v["score"] * v["weight"] / 100, 2) if v["score"] is not None else None
    nv = net_value(price, inputs, hourly)
    coverage = round(covered / 100, 2)
    out = {"price": round(price, 2), "metrics": metrics, "total": round(total, 2) if total is not None else None,
           "coverage": coverage, "threshold": policy["threshold"], "net_value": nv,
           "net_check": price >= policy["net_check_over"], "inputs": inputs.to_dict()}
    out["passes"] = (total is not None and coverage >= policy["min_coverage"] and total >= policy["threshold"]
                     and not (out["net_check"] and nv is not None and nv < 0))
    return out


def gate_reasons(result: dict, policy: dict) -> list[str]:
    """Why a request should come to you instead of going through: below threshold, too little to
    judge it on, or a larger purchase that doesn't pay for itself."""
    reasons = []
    if result["total"] is None or result["coverage"] < policy["min_coverage"]:
        reasons.append(f"eval:incomplete:{result['coverage']:.2f}")
    if result["total"] is not None and result["total"] < policy["threshold"]:
        reasons.append(f"eval:below_threshold:{result['total']:.2f}<{policy['threshold']:g}")
    if result["net_check"] and result["net_value"] is not None and result["net_value"] < 0:
        reasons.append(f"eval:negative_net_value:{result['net_value']:.2f}")
    return reasons


def grade(avg: float | None, bias: float | None) -> str | None:
    """A-F for an agent's purchasing decisions; one letter down if it overrates by 1.5+ points."""
    if avg is None:
        return None
    letters = "ABCDF"
    i = 0 if avg >= 8.5 else 1 if avg >= 7.5 else 2 if avg >= 6.5 else 3 if avg >= 5 else 4
    if bias is not None and bias >= 1.5:
        i = min(4, i + 1)
    return letters[i]
