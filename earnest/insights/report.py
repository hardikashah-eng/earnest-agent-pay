"""Monthly top-N insights."""
from __future__ import annotations

from dataclasses import dataclass, field

from .detectors import DETECTORS, Insight, month_bounds
from .statement import Transaction


@dataclass
class Report:
    month: str
    transactions_considered: int
    month_spend: float
    insights: list[Insight] = field(default_factory=list)
    hidden_dismissed: int = 0

    def to_dict(self) -> dict:
        return {"month": self.month, "transactions_considered": self.transactions_considered,
                "month_spend": round(self.month_spend, 2),
                "insights": [i.to_dict() for i in self.insights],
                "hidden_dismissed": self.hidden_dismissed,
                "estimated_monthly_savings": round(sum(i.monthly_savings for i in self.insights), 2)}

    def to_text(self) -> str:
        start, _ = month_bounds(self.month)
        lines = [f"Top {len(self.insights)} insights for {start:%B %Y}",
                 f"({self.transactions_considered} transactions considered, ${self.month_spend:,.2f} spent this month)", ""]
        if not self.insights:
            lines.append("Nothing new this month.")
        for n, i in enumerate(self.insights, 1):
            lines += [f"{n}. {i.title}",
                      f"   {i.detail}",
                      f"   -> {i.action}",
                      f"   ~${i.monthly_savings:,.0f}/month possible | confidence {i.confidence:.0%} | dismiss: {i.key}",
                      ""]
        if self.insights:
            lines.append(f"Possible total: ~${sum(i.monthly_savings for i in self.insights):,.0f}/month "
                         "(estimates; insights can overlap)")
        if self.hidden_dismissed:
            lines.append(f"({self.hidden_dismissed} insight(s) hidden because you dismissed them)")
        return "\n".join(lines)


def latest_month(txs: list[Transaction]) -> str:
    d = max(t.date for t in txs)
    return f"{d.year:04d}-{d.month:02d}"


def top_insights(txs: list[Transaction], month: str | None = None, dismissed: set[str] = frozenset(),
                 n: int = 5) -> Report:
    if not txs:
        raise ValueError("no transactions imported yet")
    month = month or latest_month(txs)
    start, end = month_bounds(month)
    history = [t for t in txs if t.date < end]
    found: list[Insight] = []
    for detect in DETECTORS:
        found.extend(detect(history, month))
    visible = [i for i in found if i.key not in dismissed and i.monthly_savings >= 1]
    visible.sort(key=lambda i: -i.rank_score)
    month_spend = sum(t.amount for t in history if t.date >= start)
    return Report(month, len(history), month_spend, visible[:n],
                  hidden_dismissed=sum(i.key in dismissed for i in found))
