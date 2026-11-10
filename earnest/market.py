"""Market prices: is this a good price compared with other stores right now?

The user's own history says what *they* paid. Market quotes say what the same item costs
elsewhere this week. Quotes come from:
- a price connector (anything implementing `PriceSource`, e.g. a price-tracker API),
- a CSV the user imports (`earnest prices import`), or
- the agent, which can report prices it saw while shopping (MCP `report_price`).

Only quotes from verified merchants within MAX_AGE_DAYS count. Agent-reported quotes can only
*lower* confidence (a cheaper store exists); they can never make a price look good, so an agent
can't talk its way into an overpriced purchase.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from statistics import median
from typing import Protocol

from .history import match_level

MAX_AGE_DAYS = 7
CHEAPER_BY = 0.15        # another verified store at least 15% cheaper is worth flagging
BEST_WITHIN = 0.02       # within 2% of the best quote counts as the best price


@dataclass(frozen=True)
class PriceQuote:
    merchant: str
    item: str
    price: float
    on: date
    source: str = "user"           # user | connector:<name> | agent:<id>
    product_id: str | None = None
    url: str | None = None

    @property
    def trusted(self) -> bool:
        return not self.source.startswith("agent:")


class PriceSource(Protocol):
    name: str

    def quotes(self, item: str, product_id: str | None) -> list[PriceQuote]: ...


@dataclass
class MarketView:
    best_price: float
    best_merchant: str
    median_price: float
    quotes: int
    trusted_best: float | None     # best among non-agent quotes

    def to_dict(self) -> dict:
        return {"best_price": self.best_price, "best_merchant": self.best_merchant,
                "median_price": self.median_price, "quotes": self.quotes, "trusted_best": self.trusted_best}


def market_view(quotes: list[PriceQuote], item: str | None, product_id: str | None, today: date,
                is_verified) -> MarketView | None:
    if not item and not product_id:
        return None
    cutoff = today - timedelta(days=MAX_AGE_DAYS)
    same = [q for q in quotes if q.on >= cutoff and is_verified(q.merchant)
            and match_level(item or "", product_id, q.item, q.product_id) == "same"]
    if not same:
        return None
    best = min(same, key=lambda q: q.price)
    trusted = [q.price for q in same if q.trusted]
    return MarketView(best.price, best.merchant, round(median(q.price for q in same), 2), len(same),
                      min(trusted) if trusted else None)


def market_signals(unit: float, merchant_keys: set[str], view: MarketView | None, canonical) -> tuple[list[str], float | None]:
    """(signals, cap or floor for the price factor). Returns a negative number as a cap,
    a positive one as a floor, None for no change."""
    if view is None:
        return [], None
    if unit > view.best_price * (1 + CHEAPER_BY) and not (canonical(view.best_merchant) & merchant_keys):
        return [f"cheaper_elsewhere:{view.best_merchant}:{view.best_price:.2f}"], -0.6
    if view.trusted_best is not None and unit <= view.trusted_best * (1 + BEST_WITHIN):
        return ["best_market_price"], 0.85
    return [], None


def refresh(ledger, source: PriceSource, items: list[tuple[str, str | None]], now: datetime) -> int:
    n = 0
    for item, pid in items:
        for q in source.quotes(item, pid):
            ledger.add_price_quote(q, now)
            n += 1
    return n
