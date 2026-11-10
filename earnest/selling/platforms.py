"""Where items get sold: listing platforms, buyback vendors and comparable-price sources.

- eBay (Inventory API): one-click. The listing is a fixed-price offer with Best Offer, and
  `bestOfferTerms.autoAcceptPrice` set to the user's floor, so eBay itself accepts the first
  offer at or above the floor, even when no agent is online. `autoDeclinePrice` turns away
  lowball offers. Requires the seller's eBay business policies (payment, return, fulfillment),
  an inventory location, and photos at public URLs.
- Buyback vendors (books, phones, games): instant quotes, accept = sold, prepaid label.
  `BuybackVendor` is the interface; connect one with its partner API.
- Mercari, Poshmark, Facebook Marketplace: no public listing API, so earnest prepares a
  ready-to-paste listing kit (title, description, price, photos, steps) and the user posts it.

Comparable prices: eBay Browse API (active listings) works with a standard developer
account; eBay Marketplace Insights (sold items, 90 days) is a limited-release API, so it's
optional. Check eBay's API license terms for your use, including its terms on AI use.

All network access goes through injectable transports so tests run offline.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .valuation import Comp

Transport = Callable[[str, str, dict | None], dict]   # (method, path_with_query, json body) -> json

EBAY_CONDITIONS = {"new": "NEW", "like_new": "LIKE_NEW", "good": "USED_GOOD", "fair": "USED_ACCEPTABLE",
                   "poor": "FOR_PARTS_OR_NOT_WORKING"}
TITLE_LIMITS = {"ebay": 80, "mercari": 80, "poshmark": 50, "facebook": 100}


@dataclass
class SellItem:
    id: str
    user_id: str
    title: str
    condition: str = "good"
    category: str | None = None
    description: str = ""
    product_id: str | None = None          # gtin:/asin: from products.normalize (ISBN-13 is a GTIN)
    photos: list[str] = field(default_factory=list)   # local paths or URLs
    status: str = "draft"

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Listing:
    platform: str
    external_id: str
    price: float
    url: str | None = None
    status: str = "active"                 # active | sold | ended | needs_user
    auto_accept: float | None = None


@dataclass
class Offer:
    id: str
    amount: float


@dataclass
class SoldEvent:
    platform: str
    external_id: str                        # the listing's id
    amount: float
    order_ref: str


class ListingPlatform(Protocol):
    name: str
    one_click: bool

    def list(self, item: SellItem, price: float, floor: float, auto: bool) -> Listing: ...
    def end(self, listing: Listing) -> None: ...
    def offers(self, listing: Listing) -> list[Offer]: ...
    def accept(self, listing: Listing, offer: Offer) -> SoldEvent: ...
    def poll_sales(self) -> list[SoldEvent]: ...


class BuybackVendor(Protocol):
    name: str

    def quote(self, item: SellItem) -> Comp | None: ...
    def accept(self, item: SellItem, quote: Comp) -> SoldEvent: ...


class CompSource(Protocol):
    def comps(self, item: SellItem) -> list[Comp]: ...


# ------------------------------------------------------------------ eBay

def ebay_transport(token: str, base: str = "https://api.ebay.com") -> Transport:
    def call(method: str, path: str, body: dict | None) -> dict:
        req = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                              "Content-Language": "en-US", "Accept-Language": "en-US",
                                              "X-EBAY-C-MARKETPLACE-ID": "EBAY_US"})
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    return call


@dataclass
class EbayConfig:
    payment_policy_id: str
    return_policy_id: str
    fulfillment_policy_id: str
    merchant_location_key: str
    marketplace_id: str = "EBAY_US"
    currency: str = "USD"
    auto_decline_ratio: float = 0.8        # offers under 80% of the floor are declined outright

    @classmethod
    def from_env(cls) -> "EbayConfig":
        e = os.environ
        return cls(e["EBAY_PAYMENT_POLICY_ID"], e["EBAY_RETURN_POLICY_ID"], e["EBAY_FULFILLMENT_POLICY_ID"],
                   e["EBAY_MERCHANT_LOCATION_KEY"])


def _product_ids(product_id: str | None) -> dict:
    if not product_id or not product_id.startswith("gtin:"):
        return {}
    digits = product_id[5:].lstrip("0")
    if len(digits) == 13 and digits.startswith(("978", "979")):
        return {"isbn": [digits]}
    if len(digits) <= 12:
        return {"upc": [digits.zfill(12)]}
    return {"ean": [digits.zfill(13)]}


class EbayInventoryAdapter:
    name, one_click = "ebay", True

    def __init__(self, transport: Transport, config: EbayConfig):
        self.call, self.cfg = transport, config

    def _money(self, v: float) -> dict:
        return {"value": f"{v:.2f}", "currency": self.cfg.currency}

    def category_for(self, title: str) -> str:
        q = urllib.parse.quote(title[:100])
        r = self.call("GET", f"/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q={q}", None)
        sugg = r.get("categorySuggestions") or []
        if not sugg:
            raise ValueError("eBay suggested no category for this title")
        return sugg[0]["category"]["categoryId"]

    def offer_body(self, item: SellItem, price: float, floor: float, auto: bool, category_id: str) -> dict:
        terms = {"bestOfferEnabled": True}
        if auto:
            terms["autoAcceptPrice"] = self._money(floor)
        decline = round(floor * self.cfg.auto_decline_ratio, 2)
        if decline > 0:
            terms["autoDeclinePrice"] = self._money(decline)
        return {
            "sku": item.id, "marketplaceId": self.cfg.marketplace_id, "format": "FIXED_PRICE",
            "availableQuantity": 1, "categoryId": category_id,
            "listingDescription": item.description or item.title,
            "listingPolicies": {"paymentPolicyId": self.cfg.payment_policy_id,
                                "returnPolicyId": self.cfg.return_policy_id,
                                "fulfillmentPolicyId": self.cfg.fulfillment_policy_id,
                                "bestOfferTerms": terms},
            "pricingSummary": {"price": self._money(price)},
            "merchantLocationKey": self.cfg.merchant_location_key,
        }

    def list(self, item: SellItem, price: float, floor: float, auto: bool) -> Listing:
        urls = [p for p in item.photos if p.startswith("https://")]
        if not urls:
            raise ValueError("eBay needs at least one photo at a public https URL; upload the photos "
                             "somewhere public (or add an image host) and try again")
        self.call("PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(item.id)}", {
            "availability": {"shipToLocationAvailability": {"quantity": 1}},
            "condition": EBAY_CONDITIONS.get(item.condition, "USED_GOOD"),
            "product": {"title": item.title[:TITLE_LIMITS["ebay"]], "description": item.description or item.title,
                        "imageUrls": urls[:12], **_product_ids(item.product_id)},
        })
        offer = self.call("POST", "/sell/inventory/v1/offer",
                          self.offer_body(item, price, floor, auto, self.category_for(item.title)))
        pub = self.call("POST", f"/sell/inventory/v1/offer/{offer['offerId']}/publish", {})
        return Listing("ebay", offer["offerId"], price, f"https://www.ebay.com/itm/{pub.get('listingId', '')}",
                       "active", floor if auto else None)

    def end(self, listing: Listing) -> None:
        self.call("POST", f"/sell/inventory/v1/offer/{listing.external_id}/withdraw", {})

    def offers(self, listing: Listing) -> list[Offer]:
        return []   # eBay applies auto-accept / auto-decline itself; others arrive as notifications

    def accept(self, listing: Listing, offer: Offer) -> SoldEvent:
        raise NotImplementedError("accept counter-offers in eBay (Best Offer); auto-accept covers the floor")

    def poll_sales(self) -> list[SoldEvent]:
        r = self.call("GET", "/sell/fulfillment/v1/order?filter=orderfulfillmentstatus:%7BNOT_STARTED%7CIN_PROGRESS%7D", None)
        out = []
        for o in r.get("orders", []):
            for li in o.get("lineItems", []):
                out.append(SoldEvent("ebay", li.get("sku", ""), float(li["total"]["value"]), o["orderId"]))
        return out


class EbayComps:
    """Active listings (Browse API) and, with limited-release access, sold items (Marketplace Insights)."""

    def __init__(self, transport: Transport, sold_access: bool = False, limit: int = 20):
        self.call, self.sold_access, self.limit = transport, sold_access, limit

    def _query(self, item: SellItem) -> str:
        ids = _product_ids(item.product_id)
        gtin = (ids.get("isbn") or ids.get("upc") or ids.get("ean") or [None])[0]
        q = f"gtin={gtin}" if gtin else f"q={urllib.parse.quote(item.title[:100])}"
        return f"{q}&limit={self.limit}"

    def comps(self, item: SellItem) -> list[Comp]:
        out = []
        r = self.call("GET", f"/buy/browse/v1/item_summary/search?{self._query(item)}", None)
        for s in r.get("itemSummaries", []):
            out.append(Comp(float(s["price"]["value"]), "active", "ebay_active",
                            _from_ebay_condition(s.get("condition", "")), None, s.get("title", "")))
        if self.sold_access:
            r = self.call("GET", f"/buy/marketplace_insights/v1_beta/item_sales/search?{self._query(item)}", None)
            for s in r.get("itemSales", []):
                out.append(Comp(float(s["lastSoldPrice"]["value"]), "sold", "ebay_sold",
                                _from_ebay_condition(s.get("condition", "")), None, s.get("title", "")))
        return out


def _from_ebay_condition(c: str) -> str:
    c = c.lower()
    if c.startswith("new") or c == "brand new":
        return "new"
    if "like new" in c or "very good" in c or "excellent" in c:
        return "like_new"
    if "acceptable" in c:
        return "fair"
    if "parts" in c:
        return "poor"
    return "good"


# ------------------------------------------------------------------ manual platforms

def listing_kit(item: SellItem, platform: str, price: float, floor: float | None = None) -> dict:
    """Everything needed to post on a platform without a listing API, in one copy-paste."""
    from .valuation import data
    p = data()["platforms"].get(platform, {"name": platform})
    limit = TITLE_LIMITS.get(platform, 80)
    cond = item.condition.replace("_", " ")
    desc = (item.description or item.title) + f"\n\nCondition: {cond}. Ships quickly."
    if item.product_id and item.product_id.startswith("gtin:"):
        desc += f"\nISBN/UPC: {item.product_id[5:].lstrip('0')}"
    steps = [f"Open {p['name']} and start a new listing" + (f" ({p['signup']})" if p.get("signup") else ""),
             "Add the photos below", "Paste the title and description", f"Set the price to ${price:.2f}"]
    if floor is not None and platform in ("mercari", "poshmark", "facebook"):
        steps.append(f"Accept offers of ${floor:.2f} or more; decline anything lower")
    steps.append("Tell earnest when it sells: earnest sell sold <item_id> --platform " + platform + " --amount X")
    return {"platform": platform, "title": item.title[:limit], "description": desc, "price": round(price, 2),
            "condition": cond, "photos": item.photos, "steps": steps}


# ------------------------------------------------------------------ mocks (tests, demos)

class MockMarketplace:
    """Simulates a one-click platform with auto-accept / auto-decline like eBay's Best Offer."""

    def __init__(self, name: str = "mockbay", decline_ratio: float = 0.8):
        self.name, self.one_click, self.decline_ratio = name, True, decline_ratio
        self.listings: dict[str, Listing] = {}
        self.pending: dict[str, list[Offer]] = {}
        self.sales: list[SoldEvent] = []
        self.floors: dict[str, float] = {}
        self._n = 0

    def list(self, item: SellItem, price: float, floor: float, auto: bool) -> Listing:
        self._n += 1
        lst = Listing(self.name, f"{self.name}-{self._n}", price, f"https://{self.name}.example/{self._n}",
                      "active", floor if auto else None)
        self.listings[lst.external_id], self.pending[lst.external_id] = lst, []
        self.floors[lst.external_id] = floor
        return lst

    def end(self, listing: Listing) -> None:
        self.listings[listing.external_id].status = "ended"

    def receive_offer(self, external_id: str, amount: float) -> str:
        lst = self.listings[external_id]
        if lst.status != "active":
            return "closed"
        if lst.auto_accept is not None and amount >= lst.auto_accept:
            lst.status = "sold"
            self.sales.append(SoldEvent(self.name, external_id, amount, f"order-{external_id}"))
            return "auto_accepted"
        if amount < self.floors[external_id] * self.decline_ratio:
            return "auto_declined"
        self.pending[external_id].append(Offer(f"offer-{len(self.pending[external_id]) + 1}", amount))
        return "pending"

    def offers(self, listing: Listing) -> list[Offer]:
        return list(self.pending.get(listing.external_id, []))

    def accept(self, listing: Listing, offer: Offer) -> SoldEvent:
        listing.status = self.listings[listing.external_id].status = "sold"
        ev = SoldEvent(self.name, listing.external_id, offer.amount, f"order-{listing.external_id}")
        self.sales.append(ev)
        return ev

    def poll_sales(self) -> list[SoldEvent]:
        out, self.sales = self.sales, []
        return out


class MockBuyback:
    def __init__(self, name: str = "mockbuyback", quotes: dict[str, float] | None = None):
        self.name, self.quotes = name, quotes or {}

    def quote(self, item: SellItem) -> Comp | None:
        price = self.quotes.get(item.product_id or "") or self.quotes.get(item.title.lower())
        return Comp(price, "buyback", f"buyback:{self.name}", item.condition) if price else None

    def accept(self, item: SellItem, quote: Comp) -> SoldEvent:
        return SoldEvent(f"buyback:{self.name}", f"{self.name}-{item.id}", quote.price, f"bb-{item.id}")


class StaticComps:
    def __init__(self, comps: list[Comp]):
        self._comps = comps

    def comps(self, item: SellItem) -> list[Comp]:
        return list(self._comps)
