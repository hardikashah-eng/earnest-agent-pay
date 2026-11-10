"""Ship it the easy way (spec/SPEC.md section 6).

For each sale: what box, what it weighs, how to pack it, which service is cheapest for it
(Media Mail for books and recorded media), where the label comes from, the ship-by date and how
to drop it off.

Labels:
- the platform's own label when it gives one (Poshmark, Mercari, buyback vendors, eBay),
- otherwise Shippo (set SHIPPO_API_KEY): real rates, cheapest eligible service, PDF label and
  tracking number.

Without a label API the costs shown are approximate retail rates from data/shipping.json.
Stdlib only; the Shippo transport is injectable for tests.
"""
from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Callable

DATA = Path(__file__).resolve().parents[1] / "data" / "shipping.json"
Transport = Callable[[str, str, dict | None], dict]


@lru_cache(maxsize=1)
def data() -> dict:
    return json.loads(DATA.read_text(encoding="utf-8"))


def profile(category: str | None) -> dict:
    p = data()["profiles"]
    return p.get(category or "", p["default"])


def approx_rate(service: str, weight_oz: float) -> float | None:
    r = data()["approx_rates"][service]
    if "first_lb" in r:
        extra = max(0, -(-weight_oz // 16) - 1)
        return round(r["first_lb"] + extra * r["per_extra_lb"], 2)
    for max_oz, price in r["bands_oz"]:
        if weight_oz <= max_oz:
            return price
    return None


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


@dataclass
class ShipPlan:
    package: str
    dims_in: list[float]
    weight_oz: float
    service: str
    service_name: str
    est_cost: float | None
    cost_is_estimate: bool
    label_from: str               # platform | buyback | shippo | you
    label_note: str
    packing: list[str]
    ship_by: date
    drop_off: str
    fragile: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["ship_by"] = self.ship_by.isoformat()
        return d


def plan(category: str | None, platform: str, sold_on: date, weight_oz: float | None = None,
         label_api: bool = False) -> ShipPlan:
    prof = profile(category)
    w = weight_oz or prof["weight_oz"]
    options = []
    if prof.get("media_mail"):
        options.append("usps_media_mail")
    options += ["usps_ground_advantage", "usps_priority"]
    priced = [(approx_rate(s, w), s) for s in options]
    priced = [(c, s) for c, s in priced if c is not None]
    cost, service = min(priced) if priced else (None, "usps_priority")
    notes = []
    if service == "usps_media_mail":
        notes.append("Media Mail: books and recorded media only, no ads or other items in the package")
    labels = data()["platform_labels"]
    key = "buyback" if platform.startswith("buyback:") else platform
    if key in ("poshmark", "mercari", "buyback"):
        label_from, label_note = ("buyback" if key == "buyback" else "platform"), labels[key]
        cost = 0.0 if key in ("poshmark", "buyback") else cost
    elif label_api:
        label_from, label_note = "shippo", "earnest buys the cheapest eligible label through Shippo"
    elif key == "ebay":
        label_from, label_note = "platform", labels["ebay"]
    else:
        label_from, label_note = "you", "Buy the label at the post office or on usps.com (Click-N-Ship)"
    sold = sold_on if isinstance(sold_on, date) else sold_on.date()
    return ShipPlan(prof["box"], prof["dims_in"], w, service, data()["approx_rates"][service]["name"], cost,
                    label_from != "shippo", label_from, label_note, prof["packing"],
                    add_business_days(sold, data()["handling_business_days"]), data()["drop_off"],
                    bool(prof.get("fragile")), notes)


# ------------------------------------------------------------------ Shippo

def shippo_transport(api_key: str, base: str = "https://api.goshippo.com") -> Transport:
    def call(method: str, path: str, body: dict | None) -> dict:
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"ShippoToken {api_key}",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    return call


class ShippoLabels:
    """Rates and labels through Shippo. Addresses are dicts with name, street1, city, state, zip, country."""

    def __init__(self, transport: Transport):
        self.call = transport

    @classmethod
    def from_env(cls) -> "ShippoLabels | None":
        key = os.environ.get("SHIPPO_API_KEY")
        return cls(shippo_transport(key)) if key else None

    def rates(self, ship_from: dict, ship_to: dict, dims_in: list[float], weight_oz: float) -> list[dict]:
        r = self.call("POST", "/shipments/", {
            "address_from": ship_from, "address_to": ship_to, "async": False,
            "parcels": [{"length": str(dims_in[0]), "width": str(dims_in[1]), "height": str(dims_in[2]),
                         "distance_unit": "in", "weight": str(weight_oz), "mass_unit": "oz"}]})
        return r.get("rates", [])

    @staticmethod
    def cheapest(rates: list[dict], media_mail_ok: bool) -> dict | None:
        ok = [r for r in rates if media_mail_ok or "media_mail" not in (r.get("servicelevel") or {}).get("token", "")]
        return min(ok, key=lambda r: float(r["amount"])) if ok else None

    def buy(self, rate_id: str) -> dict:
        t = self.call("POST", "/transactions/", {"rate": rate_id, "label_file_type": "PDF", "async": False})
        if t.get("status") != "SUCCESS":
            raise RuntimeError(f"label purchase failed: {t.get('messages')}")
        return {"label_url": t["label_url"], "tracking_number": t["tracking_number"],
                "tracking_url": t.get("tracking_url_provider")}
