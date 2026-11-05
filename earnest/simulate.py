"""Two-agent simulation: same user, same kinds of tasks, different behavior.

- "steady" stays inside mandates, pays at or below the fair price, asks first when a
  purchase is above its limits.
- "sloppy" sometimes blows the budget, overpays, and some of its purchases get rejected.

Writes an SVG chart of trust score (with tier bands) over time and prints a summary.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .gateway import Gateway
from .tiers import DEFAULT_TIERS

USER = "user_demo"
TASK = "software"
MERCHANTS = ["cloudco", "devtools.io", "apihub", "seatlicense", "buildbox"]


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


@dataclass
class Track:
    name: str
    scores: list[float] = field(default_factory=list)
    tiers: list[str] = field(default_factory=list)
    interruptions: int = 0
    violations: int = 0
    completed: int = 0
    spent: float = 0.0
    benchmark_total: float = 0.0


def _steady(rng, price, budget_max, limits):
    amount = round(price * rng.uniform(0.85, 1.0), 2)
    over_limits = amount > min(limits["cap"], limits["approval_over"])
    return amount, over_limits, ("accepted", rng.random() > 0.05)


def _sloppy(rng, price, budget_max, limits):
    if rng.random() < 0.3:
        return round(budget_max * rng.uniform(1.1, 1.5), 2), False, None  # blows the budget
    amount = round(min(price * rng.uniform(1.1, 1.25), budget_max), 2)
    verdict = "rejected" if rng.random() < 0.25 else "accepted"
    return amount, False, (verdict, rng.random() > 0.3)


def simulate(days: int = 40, seed: int = 7) -> tuple[list[Track], Gateway]:
    rng = random.Random(seed)
    clock = FakeClock(datetime(2026, 11, 1, 9, tzinfo=timezone.utc))
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": USER, "price_sensitivity": 0.8, "autonomy": 0.5})
    agents = {"steady": _steady, "sloppy": _sloppy}
    tracks = {name: Track(name) for name in agents}

    for day in range(days):
        # Task sizes grow over time, so higher tiers start to matter.
        price = round(rng.uniform(8, 40) * (1 + day / 8), 2)
        merchant = rng.choice(MERCHANTS)
        for name, behave in agents.items():
            agent_id = f"agent_{name}"
            t = tracks[name]
            budget_max = round(price * 1.3, 2)
            mandate_id = f"m_{name}_{day:02d}"
            gw.add_mandate({"id": mandate_id, "user_id": USER, "intent": f"buy a {TASK} tool",
                            "task_type": TASK, "budget": {"max": budget_max}, "benchmark_price": price})
            limits = gw.limits(USER, agent_id, TASK)
            amount, ask_first, outcome = behave(rng, price, budget_max, limits)
            d = gw.request_spend({"agent_id": agent_id, "mandate_id": mandate_id, "amount": amount,
                                  "merchant": merchant, "category": TASK, "reason": "needed for the task",
                                  "ask_first": ask_first})
            if d.status == "escalated":
                t.interruptions += 1
                d = gw.resolve(d.request_id, approve=True)  # simulated user approves in-mandate asks
            if d.status == "denied":
                t.violations += 1
            elif d.status == "approved" and outcome:
                ok, _ = gw.backend.authorize(d.credential.id, amount, merchant, clock())
                assert ok, "credential should cover the approved amount"
                verdict, on_time = outcome
                gw.report_outcome({"request_id": d.request_id, "verdict": verdict, "on_time": on_time})
                t.completed += verdict == "accepted"
                t.spent += amount
                t.benchmark_total += price
            lim = gw.limits(USER, agent_id, TASK)
            t.scores.append(lim["score"])
            t.tiers.append(lim["tier"])
        clock.advance(days=1)
    return list(tracks.values()), gw


# ---------------------------------------------------------------- chart

COLORS = {"steady": "#2563eb", "sloppy": "#dc2626"}
BANDS = [("trusted", 60, None, "#ecfdf5"), ("established", 20, 60, "#f0f9ff"),
         ("new", 0, 20, "#fafafa"), ("restricted", None, 0, "#fef2f2")]


def render_svg(tracks: list[Track]) -> str:
    W, H, L, R, T, B = 760, 380, 60, 150, 40, 40
    pw, ph = W - L - R, H - T - B
    all_scores = [s for t in tracks for s in t.scores]
    lo = min(-20.0, min(all_scores) - 5)
    hi = max(80.0, max(all_scores) + 5)
    n = max(len(t.scores) for t in tracks)

    def x(i): return L + pw * i / max(1, n - 1)
    def y(v): return T + ph * (hi - v) / (hi - lo)

    caps = {t.name: t.cap for t in DEFAULT_TIERS}
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" font-family="system-ui, sans-serif" font-size="12">',
           f'<rect width="{W}" height="{H}" fill="#ffffff"/>',
           f'<text x="{L}" y="22" font-size="15" font-weight="600" fill="#111827">Trust score over time: same tasks, different behavior</text>']
    for name, b_lo, b_hi, fill in BANDS:
        top = y(b_hi if b_hi is not None else hi)
        bot = y(b_lo if b_lo is not None else lo)
        out.append(f'<rect x="{L}" y="{top:.1f}" width="{pw}" height="{bot - top:.1f}" fill="{fill}"/>')
        out.append(f'<text x="{L + 6}" y="{top + 14:.1f}" fill="#6b7280">{name} · cap ${caps[name]:,.0f}</text>')
    for v in range(int(lo // 20 * 20), int(hi) + 1, 20):
        if lo <= v <= hi:
            out.append(f'<line x1="{L}" x2="{L + pw}" y1="{y(v):.1f}" y2="{y(v):.1f}" stroke="#e5e7eb"/>')
            out.append(f'<text x="{L - 8}" y="{y(v) + 4:.1f}" text-anchor="end" fill="#6b7280">{v}</text>')
    out.append(f'<text x="{L + pw / 2}" y="{H - 8}" text-anchor="middle" fill="#6b7280">day</text>')
    for i in range(0, n, 5):
        out.append(f'<text x="{x(i):.1f}" y="{T + ph + 16}" text-anchor="middle" fill="#6b7280">{i}</text>')
    for t in tracks:
        pts = " ".join(f"{x(i):.1f},{y(s):.1f}" for i, s in enumerate(t.scores))
        c = COLORS.get(t.name, "#111827")
        out.append(f'<polyline points="{pts}" fill="none" stroke="{c}" stroke-width="2.5" stroke-linejoin="round"/>')
        ly = y(t.scores[-1])
        out.append(f'<circle cx="{x(len(t.scores) - 1):.1f}" cy="{ly:.1f}" r="3.5" fill="{c}"/>')
        out.append(f'<text x="{L + pw + 10}" y="{ly + 4:.1f}" fill="{c}" font-weight="600">{t.name} · {t.tiers[-1]}</text>')
    out.append("</svg>")
    return "\n".join(out)


def run(out: str = "demo/limits.svg", days: int = 40) -> list[Track]:
    tracks, _ = simulate(days=days)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(render_svg(tracks), encoding="utf-8")
    print(f"{'agent':<8} {'final tier':<12} {'score':>6} {'accepted':>9} {'violations':>11} {'asked you':>10} {'vs fair price':>14}")
    for t in tracks:
        vs = (t.spent / t.benchmark_total - 1) * 100 if t.benchmark_total else 0.0
        print(f"{t.name:<8} {t.tiers[-1]:<12} {t.scores[-1]:>6.1f} {t.completed:>9} {t.violations:>11} "
              f"{t.interruptions:>10} {vs:>+13.1f}%")
    print(f"\nchart written to {out}")
    return tracks
