"""What will this sell for, and where? (spec/SPEC.md section 6)

Identifying the item from a photo is the agent's job (Claude sees the picture and fills in
title, condition, category and any barcode/ISBN). Earnest then prices it from comparables:

- sold listings (best evidence; eBay's sold-items API is limited release, so this source is
  optional),
- active listings (asking prices, discounted because asks aren't sales),
- instant buyback quotes (books, phones, games: a guaranteed price, usually lower),
- price quotes already in earnest (prices.add, agent reports).

Comps in a different condition are adjusted to the item's condition. The result is a likely
price, a quick-sale price and a high price, plus the estimated payout per platform after
fees and shipping.

Pure functions, no I/O.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from functools import lru_cache
from pathlib import Path
from statistics import median

DATA = Path(__file__).resolve().parents[1] / "data" / "selling.json"
CONDITIONS = ("new", "like_new", "good", "fair", "poor")


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Comp:
    price: float
    kind: str                  # sold | active | buyback
    source: str                # ebay_sold | ebay_active | buyback:<vendor> | quote:<source>
    condition: str = "good"
    on: date | None = None
    title: str = ""


@dataclass
class Valuation:
    likely: float | None
    quick_sale: float | None
    high: float | None
    comps: int
    sold_comps: int
    confidence: str            # low | medium | high
    best_buyback: Comp | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "best_buyback"}
        if self.best_buyback:
            out["best_buyback"] = {"price": self.best_buyback.price, "vendor": self.best_buyback.source}
        return out


def _adjust(c: Comp, condition: str) -> float:
    m = data()["condition_multiplier"]
    price = c.price * m.get(condition, 1.0) / m.get(c.condition, 1.0)
    if c.kind == "active":
        price *= data()["active_listing_discount"]
    return price


def _weighted_quantile(pairs: list[tuple[float, float]], q: float) -> float:
    pairs = sorted(pairs)
    total = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= q * total:
            return v
    return pairs[-1][0]


def value(comps: list[Comp], condition: str = "good") -> Valuation:
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}")
    market = [c for c in comps if c.kind in ("sold", "active") and c.price > 0]
    buybacks = [c for c in comps if c.kind == "buyback" and c.price > 0]
    best_bb = max(buybacks, key=lambda c: c.price) if buybacks else None
    notes = []
    if not market:
        if best_bb:
            notes.append("only buyback quotes found; market price unknown")
            return Valuation(None, best_bb.price, None, 0, 0, "low", best_bb, notes)
        return Valuation(None, None, None, 0, 0, "low", None, ["no comparable listings found"])
    pairs = [(_adjust(c, condition), 1.0 if c.kind == "sold" else 0.5) for c in market]
    sold = sum(c.kind == "sold" for c in market)
    likely = _weighted_quantile(pairs, 0.5)
    quick = _weighted_quantile(pairs, 0.25)
    high = _weighted_quantile(pairs, 0.75)
    if best_bb and best_bb.price > quick:
        quick = best_bb.price  # a guaranteed buyback beats a low asking price
    if sold == 0:
        notes.append("no sold prices (only asking prices); estimate is rougher")
    conf = "high" if sold >= 5 else "medium" if len(market) >= 4 else "low"
    return Valuation(round(likely, 2), round(quick, 2), round(high, 2), len(market), sold, conf, best_bb, notes)


# ------------------------------------------------------------------ payouts & platforms

def fee(platform: str, price: float, category: str | None = None, shipped: bool = True) -> float:
    p = data()["platforms"][platform]
    if platform == "facebook" and not shipped:
        return 0.0
    if "flat_under_15" in p and price < 15:
        return p["flat_under_15"]
    pct = p.get("category_fee_pct", {}).get(category or "", p["fee_pct"])
    fixed = p.get("fee_fixed_under_10", p.get("fee_fixed", 0)) if price <= 10 else p.get("fee_fixed", 0)
    return round(max(price * pct / 100 + fixed, p.get("fee_min", 0)), 2)


def shipping_estimate(category: str | None) -> float:
    est = data()["shipping_estimate"]
    return est.get(category or "", est["default"])


def payout(platform: str, price: float, category: str | None, local: bool = False) -> float:
    p = data()["platforms"][platform]
    ship = 0.0 if (local or not p.get("seller_ships")) else shipping_estimate(category)
    return round(price - fee(platform, price, category, shipped=not local) - ship, 2)


@dataclass
class PlatformOption:
    platform: str
    name: str
    list_price: float
    est_payout: float
    linked: bool
    one_click: bool            # API listing with auto-accept at your floor
    note: str
    signup: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def recommend(v: Valuation, category: str | None, linked: set[str], floor: float | None = None,
              connected: dict[str, bool] | None = None) -> list[PlatformOption]:
    """Platforms ranked by estimated payout. Buyback (if quoted) is listed as its own option.
    `connected` = {platform: one_click} for adapters configured in this install; ones without fee
    data are estimated with eBay's (conservative) fees."""
    out: list[PlatformOption] = []
    price = v.likely or v.quick_sale
    bulky = (category or "") in data()["bulky_categories"]
    for name, one_click in (connected or {}).items():
        if price and name not in data()["platforms"]:
            net = round(price - fee("ebay", price, category) - shipping_estimate(category), 2)
            out.append(PlatformOption(name, name, price, net, name in linked, one_click and name in linked,
                                      "connected platform; fees estimated like eBay's"))
    if price:
        for key, p in data()["platforms"].items():
            if "*" not in p["good_for"] and (category or "") not in p["good_for"]:
                continue
            local = key == "facebook" and (bulky or price < 15)
            net = payout(key, price, category, local=local)
            note = "local pickup, no fee or shipping" if local else (
                f"after ~{fee(key, price, category):.2f} fees" +
                (f" and ~{shipping_estimate(category):.2f} shipping" if p.get("seller_ships") else ""))
            api = p["api"] and (connected is None or key in connected)
            out.append(PlatformOption(key, p["name"], price, net, key in linked,
                                      api and key in linked, note,
                                      None if key in linked else p.get("signup")))
    if v.best_buyback:
        bb = v.best_buyback
        out.append(PlatformOption(bb.source, f"Instant buyback ({bb.source.split(':', 1)[-1]})", bb.price,
                                  bb.price, True, True, "guaranteed price, free prepaid label, paid on receipt"))
    if floor is not None:
        for o in out:
            if o.est_payout < floor * 0.75:
                o.note += "; payout likely below your floor after fees"
    return sorted(out, key=lambda o: (-o.est_payout, not o.one_click))
