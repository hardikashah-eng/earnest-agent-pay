"""Learn each user's confidence weights from their own verdicts (spec/SPEC.md section 3e).

Every scored purchase stores its four factor scores. When the user later accepts it, that's a
positive label; rejected, disputed, or an item mismatch is a negative one. With enough labels
we fit a small logistic regression, p(keep) = sigmoid(b + sum(w_i * f_i)), then turn the
coefficients back into the familiar weights that sum to 1, so the score stays a readable
weighted average.

Small data guards:
- nothing is learned until there are MIN_SAMPLES labels with both outcomes present;
- coefficients are pulled toward the default weights (L2 prior), and the learned weights are
  blended with the defaults by n / (n + SHRINK), so 10 verdicts move things a little and 200
  move them a lot;
- the calibration report compares default and learned weights on held-out folds, and the
  learned weights are only kept if they predict the user's verdicts at least as well.

Pure Python, no numpy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from .confidence import WEIGHTS

FACTORS = ("authenticity", "price", "familiarity", "budget")
MIN_SAMPLES = 10
SHRINK = 30.0
L2 = 0.5
STEPS, LR = 1500, 0.3


@dataclass(frozen=True)
class Sample:
    factors: tuple[float, ...]   # in FACTORS order
    kept: bool


def _sigmoid(z: float) -> float:
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def fit_logistic(samples: list[Sample], prior: tuple[float, ...], scale: float = 4.0) -> tuple[float, list[float]]:
    """Gradient descent with an L2 pull toward scale * prior."""
    k = len(prior)
    target = [scale * p for p in prior]
    w, b = list(target), -scale / 2
    n = len(samples)
    for _ in range(STEPS):
        gw, gb = [0.0] * k, 0.0
        for s in samples:
            err = _sigmoid(b + sum(wi * fi for wi, fi in zip(w, s.factors))) - (1.0 if s.kept else 0.0)
            gb += err
            for i in range(k):
                gw[i] += err * s.factors[i]
        for i in range(k):
            w[i] -= LR * (gw[i] / n + L2 * (w[i] - target[i]) / n)
        b -= LR * gb / n
    return b, w


def to_weights(coefs: list[float], n: int, prior: dict[str, float] = WEIGHTS) -> dict[str, float]:
    pos = [max(0.0, c) for c in coefs]
    total = sum(pos)
    learned = [p / total for p in pos] if total > 0 else [prior[f] for f in FACTORS]
    alpha = n / (n + SHRINK)
    blended = [alpha * l + (1 - alpha) * prior[f] for l, f in zip(learned, FACTORS)]
    s = sum(blended)
    return {f: round(v / s, 3) for f, v in zip(FACTORS, blended)}


def score_with(weights: dict[str, float], factors: tuple[float, ...]) -> float:
    return sum(weights[f] * v for f, v in zip(FACTORS, factors))


def _calibrator(samples: list[Sample], weights: dict[str, float]) -> tuple[float, float]:
    """Map a weighted score to p(keep) with a 1-D logistic fit (Platt scaling)."""
    xs = [Sample((score_with(weights, s.factors),), s.kept) for s in samples]
    b, w = fit_logistic(xs, (1.0,), scale=4.0)
    return b, w[0]


def brier(samples: list[Sample], weights: dict[str, float], cal: tuple[float, float]) -> float:
    b, a = cal
    return sum((_sigmoid(b + a * score_with(weights, s.factors)) - (1.0 if s.kept else 0.0)) ** 2
               for s in samples) / len(samples)


def learn(samples: list[Sample], folds: int = 5) -> dict:
    """Fit weights and report whether they beat the defaults on held-out data."""
    n = len(samples)
    kept = sum(s.kept for s in samples)
    report: dict = {"samples": n, "kept": kept, "not_kept": n - kept, "default_weights": dict(WEIGHTS)}
    if n < MIN_SAMPLES or kept == 0 or kept == n:
        report.update(status="not_enough_data", weights=dict(WEIGHTS),
                      needed=f"{MIN_SAMPLES}+ verdicts including both kept and rejected purchases")
        return report

    prior = tuple(WEIGHTS[f] for f in FACTORS)
    k = min(folds, n)
    errs = {"default": 0.0, "learned": 0.0}
    for i in range(k):
        test = samples[i::k]
        train = [s for j, s in enumerate(samples) if j % k != i]
        if not test or all(s.kept for s in train) or not any(s.kept for s in train):
            continue
        learned_w = to_weights(fit_logistic(train, prior)[1], len(train))
        for name, wts in (("default", dict(WEIGHTS)), ("learned", learned_w)):
            errs[name] += brier(test, wts, _calibrator(train, wts)) * len(test)
    weights = to_weights(fit_logistic(samples, prior)[1], n)
    better = errs["learned"] <= errs["default"] + 1e-9
    report.update(
        status="learned" if better else "kept_defaults",
        weights=weights if better else dict(WEIGHTS),
        candidate_weights=weights,
        heldout_brier={"default": round(errs["default"] / n, 4), "learned": round(errs["learned"] / n, 4)},
        calibration=calibration_table(samples, weights if better else dict(WEIGHTS)))
    return report


def calibration_table(samples: list[Sample], weights: dict[str, float]) -> list[dict]:
    """How often purchases in each confidence band were actually kept."""
    bands = [(0.0, 0.5, "low"), (0.5, 0.75, "medium"), (0.75, 1.01, "high")]
    out = []
    for lo, hi, name in bands:
        group = [s for s in samples if lo <= score_with(weights, s.factors) < hi]
        if group:
            out.append({"band": name, "purchases": len(group),
                        "kept_rate": round(sum(s.kept for s in group) / len(group), 2)})
    return out
