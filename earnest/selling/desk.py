"""The sell desk: items, sell mandates, listings and sales (spec/SPEC.md section 6).

A **sell mandate** is the user's instruction for one item: a floor ("sell if I get $10 or
more"), an optional target asking price, whether the agent may act on its own (auto), which
platforms it may use, and an expiry. The rules mirror buying:

- Only the user sets a floor. A floor the agent proposes waits for one tap from the user.
- An agent may list at or above the floor and accept offers or buyback quotes at or above it
  when the mandate is auto. Anything below the floor is denied and counts as a violation
  (task type "selling"); without auto, offers go to the user.
- eBay listings carry the floor as Best Offer auto-accept, so the platform enforces it too.
- When an item sells anywhere, every other listing for it is ended (no double sales).
- A completed sale at or above the floor earns the agent trust, like a kept purchase.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from ..history import match_level
from ..models import ValidationError
from .platforms import Listing, SellItem, SoldEvent, listing_kit
from .valuation import CONDITIONS, Comp, data, recommend, value

TASK = "selling"
SALE_POINTS = 2.0
VIOLATION = -10.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS sell_items (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, status TEXT NOT NULL,
    data TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sell_mandates (item_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, floor REAL NOT NULL,
    target REAL, auto INTEGER NOT NULL, platforms TEXT NOT NULL, expires_at TEXT NOT NULL, status TEXT NOT NULL,
    created_by TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sell_listings (id INTEGER PRIMARY KEY AUTOINCREMENT, item_id TEXT NOT NULL,
    platform TEXT NOT NULL, external_id TEXT, price REAL NOT NULL, status TEXT NOT NULL, url TEXT,
    auto_accept REAL, agent_id TEXT, kit TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sell_sales (id TEXT PRIMARY KEY, item_id TEXT NOT NULL, user_id TEXT NOT NULL,
    platform TEXT NOT NULL, amount REAL NOT NULL, order_ref TEXT, status TEXT NOT NULL, agent_id TEXT,
    data TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS linked_platforms (user_id TEXT NOT NULL, platform TEXT NOT NULL, at TEXT NOT NULL,
    PRIMARY KEY (user_id, platform));
"""


@dataclass
class SellMandate:
    item_id: str
    user_id: str
    floor: float
    target: float | None
    auto: bool
    platforms: list[str]
    expires_at: datetime
    status: str            # pending | active | done | cancelled
    created_by: str

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["expires_at"] = self.expires_at.isoformat()
        return d


class SellDesk:
    def __init__(self, gateway, platforms: dict | None = None, buyback: list | None = None,
                 comp_sources: list | None = None, labels=None):
        self.gw = gateway
        self.db = gateway.ledger.db
        self.db.executescript(SCHEMA)
        self.platforms = platforms or {}
        self.buyback = buyback or []
        self.comp_sources = comp_sources or []
        self.labels = labels            # shipping.ShippoLabels or None
        gateway.token_handlers["sm_"] = (self._resolve_token, self._describe_token)

    # ---------------------------------------------------------------- storage helpers
    def _now(self) -> datetime:
        return self.gw.clock()

    def item(self, item_id: str) -> SellItem:
        row = self.db.execute("SELECT data, status FROM sell_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise ValidationError(f"unknown item: {item_id}")
        it = SellItem(**json.loads(row["data"]))
        it.status = row["status"]
        return it

    def _save_item(self, it: SellItem) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sell_items VALUES (?, ?, ?, ?, ?)",
                            (it.id, it.user_id, it.status, json.dumps(it.to_dict()), self._now().isoformat()))

    def mandate(self, item_id: str) -> SellMandate | None:
        r = self.db.execute("SELECT * FROM sell_mandates WHERE item_id = ?", (item_id,)).fetchone()
        if r is None:
            return None
        return SellMandate(r["item_id"], r["user_id"], r["floor"], r["target"], bool(r["auto"]),
                           json.loads(r["platforms"]), datetime.fromisoformat(r["expires_at"]), r["status"],
                           r["created_by"])

    def _save_mandate(self, m: SellMandate) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO sell_mandates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (m.item_id, m.user_id, m.floor, m.target, int(m.auto), json.dumps(m.platforms),
                             m.expires_at.isoformat(), m.status, m.created_by, self._now().isoformat()))

    def listings(self, item_id: str, status: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM sell_listings WHERE item_id = ?", [item_id]
        if status:
            q += " AND status = ?"
            args.append(status)
        return [dict(r) for r in self.db.execute(q + " ORDER BY id", args)]

    def link_platform(self, user_id: str, platform: str) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO linked_platforms VALUES (?, ?, ?)",
                            (user_id, platform, self._now().isoformat()))

    def linked(self, user_id: str) -> set[str]:
        return {r["platform"] for r in self.db.execute("SELECT platform FROM linked_platforms WHERE user_id = ?",
                                                        (user_id,))}

    def _penalize(self, user_id: str, agent_id: str | None, kind: str, ref: str) -> None:
        if agent_id:
            self.gw.ledger.add_points(user_id, agent_id, TASK, kind, VIOLATION, ref, self._now())

    # ---------------------------------------------------------------- items & valuation
    def new_item(self, user_id: str, title: str, condition: str = "good", category: str | None = None,
                 description: str = "", product_id: str | None = None, photos: list[str] | None = None) -> SellItem:
        from ..products import normalize
        if condition not in CONDITIONS:
            raise ValidationError(f"condition must be one of {CONDITIONS}")
        if not title.strip():
            raise ValidationError("an item needs a title (what is it?)")
        it = SellItem("sell_" + uuid.uuid4().hex[:10], user_id, title.strip(), condition,
                      (category or "").lower() or None, description, normalize(product_id), list(photos or []))
        self._save_item(it)
        return it

    def gather_comps(self, it: SellItem) -> list[Comp]:
        comps: list[Comp] = []
        for src in self.comp_sources:
            try:
                comps += src.comps(it)
            except Exception:
                pass   # one broken source shouldn't block a valuation
        for vendor in self.buyback:
            try:
                q = vendor.quote(it)
            except Exception:
                q = None
            if q:
                comps.append(q)
        for q in self.gw.ledger.price_quotes((self._now() - timedelta(days=90)).date().isoformat()):
            if match_level(it.title, it.product_id, q["item"], q["product_id"]) == "same":
                comps.append(Comp(q["price"], "active", f"quote:{q['source']}", "good"))
        return comps

    def appraise(self, item_id: str) -> dict:
        it = self.item(item_id)
        comps = self.gather_comps(it)
        v = value(comps, it.condition)
        m = self.mandate(item_id)
        opts = recommend(v, it.category, self.linked(it.user_id) | {f"buyback:{b.name}" for b in self.buyback},
                         m.floor if m else None, {n: a.one_click for n, a in self.platforms.items()})
        if it.status == "draft":
            it.status = "valued"
            self._save_item(it)
        return {"item": it.to_dict(), "valuation": v.to_dict(), "options": [o.to_dict() for o in opts],
                "suggested_floor": v.quick_sale, "suggested_price": v.likely}

    # ---------------------------------------------------------------- sell mandates
    def set_floor(self, item_id: str, floor: float, target: float | None = None, auto: bool = True,
                  platforms: list[str] | None = None, days: int = 30, by: str = "user") -> SellMandate:
        """by = "user" (active now) or "agent:<id>" (waits for the user's one tap)."""
        it = self.item(item_id)
        if floor <= 0:
            raise ValidationError("floor must be more than 0")
        if target is not None and target < floor:
            raise ValidationError("target price must be at or above the floor")
        status = "active" if by == "user" else "pending"
        m = SellMandate(item_id, it.user_id, round(floor, 2), round(target, 2) if target else None, auto,
                        list(platforms or []), self._now() + timedelta(days=days), status, by)
        self._save_mandate(m)
        if status == "pending" and self.gw.notifier is not None:
            from ..approvals import ApprovalMessage
            approve, deny = self.gw.approval_links(f"sm_{item_id}")
            rng = f"${floor:.2f}" + (f"-${target:.2f}" if target else "+")
            try:
                self.gw.notifier.notify(ApprovalMessage(
                    f"sm_{item_id}", by.split(":", 1)[-1], "(sell)", floor, it.title,
                    f"Sell {it.title} for {rng}" + (", agent may accept offers on its own" if auto else ""),
                    ["sell_mandate_needs_you"], None, approve, deny))
            except Exception:
                pass
        return m

    def confirm(self, item_id: str, approve: bool) -> SellMandate:
        m = self.mandate(item_id)
        if m is None or m.status != "pending":
            raise ValidationError(f"no sell instruction waiting for {item_id}")
        m.status = "active" if approve else "cancelled"
        self._save_mandate(m)
        return m

    def _resolve_token(self, ref: str, approve: bool, approver: str | None):
        return self.confirm(ref[3:], approve)

    def _describe_token(self, ref: str) -> dict | None:
        m = self.mandate(ref[3:])
        if m is None or m.status != "pending":
            return None
        it = self.item(m.item_id)
        rng = f"${m.floor:.2f}" + (f" (asking ${m.target:.2f})" if m.target else " or more")
        return {"title": f"Sell {it.title} for {rng}?", "amount": m.floor, "merchant": "sell", "item": it.title,
                "reasons": ["agent may accept offers at or above your floor" if m.auto else "you'll approve each offer"],
                "reason_given": f"proposed by {m.created_by}", "confidence": None}

    def _active(self, item_id: str) -> SellMandate:
        m = self.mandate(item_id)
        if m is None or m.status != "active":
            raise ValidationError("no active sell instruction for this item (set a floor first)")
        if self._now() > m.expires_at:
            raise ValidationError("the sell instruction expired; set it again to keep selling")
        return m

    # ---------------------------------------------------------------- listing & offers
    def list_item(self, item_id: str, platform: str, price: float, agent_id: str | None = None) -> dict:
        it, m = self.item(item_id), self._active(item_id)
        if it.status in ("sold", "shipped", "done"):
            raise ValidationError("item already sold")
        if m.platforms and platform not in m.platforms:
            raise ValidationError(f"{platform} isn't one of the platforms you allowed ({', '.join(m.platforms)})")
        if price < m.floor - 1e-9:
            self._penalize(it.user_id, agent_id, "sell_below_floor", item_id)
            raise ValidationError(f"price ${price:.2f} is below the floor ${m.floor:.2f}")
        adapter = self.platforms.get(platform)
        now = self._now().isoformat()
        if adapter is not None and adapter.one_click and platform in self.linked(it.user_id):
            lst = adapter.list(it, price, m.floor, m.auto)
            with self.db:
                self.db.execute("INSERT INTO sell_listings (item_id, platform, external_id, price, status, url, "
                                "auto_accept, agent_id, kit, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                (item_id, platform, lst.external_id, price, "active", lst.url, lst.auto_accept,
                                 agent_id, None, now))
            it.status = "listed"
            self._save_item(it)
            return {"platform": platform, "status": "listed", "url": lst.url, "price": price,
                    "auto_accept_at": lst.auto_accept}
        kit = listing_kit(it, platform, price, m.floor)
        with self.db:
            self.db.execute("INSERT INTO sell_listings (item_id, platform, external_id, price, status, url, "
                            "auto_accept, agent_id, kit, at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (item_id, platform, None, price, "needs_user", None, None, agent_id, json.dumps(kit), now))
        return {"platform": platform, "status": "needs_user", "kit": kit}

    def handle_offer(self, item_id: str, platform: str, amount: float, offer_id: str | None = None,
                     agent_id: str | None = None) -> dict:
        """An offer came in (or a buyback quote is on the table): accept, refuse, or ask the user."""
        it, m = self.item(item_id), self._active(item_id)
        if amount < m.floor - 1e-9:
            return {"action": "declined", "reason": f"below your floor ${m.floor:.2f}"}
        if not m.auto:
            return {"action": "ask_user", "reason": "you asked to approve each offer"}
        lst = next((l for l in self.listings(item_id, "active") if l["platform"] == platform), None)
        adapter = self.platforms.get(platform)
        if lst and adapter is not None and offer_id:
            from .platforms import Offer
            ev = adapter.accept(Listing(platform, lst["external_id"], lst["price"]), Offer(offer_id, amount))
            return {"action": "accepted", "sale": self.record_sale(item_id, ev.platform, ev.amount, ev.order_ref,
                                                                   agent_id)}
        return {"action": "accept", "reason": "at or above your floor; accept it on the platform"}

    def accept_offer(self, item_id: str, platform: str, amount: float, offer_id: str,
                     agent_id: str | None = None) -> dict:
        """The agent accepting an offer explicitly. Below the floor is a violation."""
        it, m = self.item(item_id), self._active(item_id)
        if amount < m.floor - 1e-9:
            self._penalize(it.user_id, agent_id, "sell_below_floor", item_id)
            raise ValidationError(f"${amount:.2f} is below the floor ${m.floor:.2f}")
        if not m.auto:
            raise ValidationError("this item needs your approval for each offer")
        return self.handle_offer(item_id, platform, amount, offer_id, agent_id)

    def accept_buyback(self, item_id: str, vendor: str, agent_id: str | None = None) -> dict:
        it, m = self.item(item_id), self._active(item_id)
        v = next((b for b in self.buyback if b.name == vendor), None)
        if v is None:
            raise ValidationError(f"unknown buyback vendor {vendor}")
        q = v.quote(it)
        if q is None:
            raise ValidationError(f"{vendor} isn't buying this item right now")
        if q.price < m.floor - 1e-9:
            self._penalize(it.user_id, agent_id, "sell_below_floor", item_id)
            raise ValidationError(f"{vendor} offers ${q.price:.2f}, below the floor ${m.floor:.2f}")
        if not m.auto and agent_id:
            raise ValidationError("this item needs your approval for each offer")
        ev = v.accept(it, q)
        return self.record_sale(item_id, ev.platform, ev.amount, ev.order_ref, agent_id)

    # ---------------------------------------------------------------- sales
    def record_sale(self, item_id: str, platform: str, amount: float, order_ref: str | None = None,
                    agent_id: str | None = None, ship_to: dict | None = None) -> dict:
        it = self.item(item_id)
        if it.status in ("sold", "shipped", "done"):
            raise ValidationError("item already sold")
        m = self.mandate(item_id)
        now = self._now()
        lister = agent_id or next((l["agent_id"] for l in self.listings(item_id) if l["platform"] == platform
                                   and l["agent_id"]), None)
        sale_id = "sale_" + uuid.uuid4().hex[:10]
        ended = []
        for l in self.listings(item_id):
            if l["status"] not in ("active", "needs_user"):
                continue
            if l["platform"] == platform:
                new = "sold"
            else:
                new = "ended"
                adapter = self.platforms.get(l["platform"])
                if adapter is not None and l["external_id"]:
                    try:
                        adapter.end(Listing(l["platform"], l["external_id"], l["price"]))
                    except Exception:
                        new = "end_failed"
                ended.append({"platform": l["platform"], "status": new})
            with self.db:
                self.db.execute("UPDATE sell_listings SET status = ? WHERE id = ?", (new, l["id"]))
        from .shipping import plan as ship_plan
        sp = ship_plan(it.category, platform, now.date(), label_api=self.labels is not None)
        with self.db:
            self.db.execute("INSERT INTO sell_sales VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (sale_id, item_id, it.user_id, platform, amount, order_ref, "sold", lister,
                             json.dumps({"floor": m.floor if m else None, "ship_to": ship_to,
                                         "plan": sp.to_dict(), "events": [["sold", now.isoformat()]]}),
                             now.isoformat()))
        it.status = "sold"
        self._save_item(it)
        if m:
            m.status = "done"
            self._save_mandate(m)
        if lister and m and amount >= m.floor - 1e-9:
            self.gw.ledger.add_points(it.user_id, lister, TASK, "sold_at_or_above_floor", SALE_POINTS, sale_id, now)
        out = {"sale_id": sale_id, "item_id": item_id, "platform": platform, "amount": amount,
               "other_listings": ended, "ship_by": sp.ship_by.isoformat(), "shipping": sp.to_dict()}
        manual = [e for e in ended if e["platform"] not in self.platforms]
        if manual:
            out["remove_manually"] = [e["platform"] for e in manual]
        return out

    def sync(self) -> list[dict]:
        """Pull sales from connected platforms (e.g. eBay auto-accepted a Best Offer)."""
        out = []
        for name, adapter in self.platforms.items():
            for ev in adapter.poll_sales():
                row = self.db.execute("SELECT item_id FROM sell_listings WHERE platform = ? AND external_id = ? "
                                      "AND status = 'active'", (name, ev.external_id)).fetchone()
                if row is None and name == "ebay":   # eBay orders carry the SKU (= item id)
                    row = self.db.execute("SELECT id AS item_id FROM sell_items WHERE id = ?",
                                          (ev.external_id,)).fetchone()
                if row:
                    try:
                        out.append(self.record_sale(row["item_id"], name, ev.amount, ev.order_ref,
                                                    ship_to=ev.ship_to))
                    except ValidationError:
                        pass
        return out

    # ---------------------------------------------------------------- shipping & follow-through
    LABEL_MAX_SHARE = 0.25     # an agent may buy a label costing up to 25% of the sale; more asks the user
    STATUSES = ("sold", "label_ready", "shipped", "delivered", "completed", "cancelled")

    def set_ship_from(self, user_id: str, address: dict) -> None:
        for k in ("name", "street1", "city", "state", "zip"):
            if not address.get(k):
                raise ValidationError(f"ship-from address needs {k}")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)",
                            (f"ship_from:{user_id}", json.dumps({"country": "US", **address})))

    def ship_from(self, user_id: str) -> dict | None:
        row = self.db.execute("SELECT value FROM settings WHERE name = ?", (f"ship_from:{user_id}",)).fetchone()
        return json.loads(row["value"]) if row else None

    def sale(self, sale_id: str) -> dict:
        r = self.db.execute("SELECT * FROM sell_sales WHERE id = ?", (sale_id,)).fetchone()
        if r is None:
            raise ValidationError(f"unknown sale: {sale_id}")
        return {**dict(r), "data": json.loads(r["data"])}

    def _update_sale(self, sale_id: str, status: str, **changes) -> dict:
        s = self.sale(sale_id)
        d = s["data"]
        d.update(changes)
        d.setdefault("events", []).append([status, self._now().isoformat()])
        with self.db:
            self.db.execute("UPDATE sell_sales SET status = ?, data = ? WHERE id = ?", (status, json.dumps(d), sale_id))
        return self.sale(sale_id)

    def ship(self, sale_id: str, weight_oz: float | None = None, ship_to: dict | None = None, buy_label: bool = False,
             agent_id: str | None = None) -> dict:
        """The shipping plan for a sale; with a label API and buy_label, buys the cheapest eligible label."""
        from .shipping import plan as ship_plan, profile
        s = self.sale(sale_id)
        if s["status"] not in ("sold", "label_ready"):
            raise ValidationError(f"sale is {s['status']}")
        it = self.item(s["item_id"])
        sp = ship_plan(it.category, s["platform"], datetime.fromisoformat(s["at"]).date(), weight_oz,
                       self.labels is not None)
        out = {"sale_id": sale_id, "plan": sp.to_dict()}
        if not buy_label or sp.label_from != "shippo":
            self._update_sale(sale_id, s["status"], plan=sp.to_dict())
            return out
        to = ship_to or s["data"].get("ship_to")
        frm = self.ship_from(it.user_id)
        if not (to and frm):
            raise ValidationError("buying a label needs the buyer's address and your ship-from address "
                                  "(earnest sell address ...)")
        rates = self.labels.rates(frm, to, sp.dims_in, sp.weight_oz)
        rate = self.labels.cheapest(rates, bool(profile(it.category).get("media_mail")))
        if rate is None:
            raise ValidationError("no shipping rates returned for this package")
        cost = float(rate["amount"])
        if agent_id and cost > self.LABEL_MAX_SHARE * s["amount"]:
            out.update(needs_user=f"cheapest label is ${cost:.2f}, over 25% of the ${s['amount']:.2f} sale")
            return out
        label = self.labels.buy(rate["object_id"])
        label.update(cost=cost, service=(rate.get("servicelevel") or {}).get("name"), carrier=rate.get("provider"))
        self._update_sale(sale_id, "label_ready", plan=sp.to_dict(), label=label)
        out["label"] = label
        return out

    def mark(self, sale_id: str, status: str, tracking: str | None = None) -> dict:
        if status not in ("shipped", "delivered", "completed", "cancelled"):
            raise ValidationError("status must be shipped, delivered, completed or cancelled")
        s = self.sale(sale_id)
        order = self.STATUSES.index
        if s["status"] in ("completed", "cancelled") or (status != "cancelled" and order(status) <= order(s["status"])):
            raise ValidationError(f"sale is already {s['status']}")
        changes = {"tracking": tracking} if tracking else {}
        if status == "cancelled":
            it = self.item(s["item_id"])
            it.status = "valued"
            self._save_item(it)
        return self._update_sale(sale_id, status, **changes)

    def todo(self, user_id: str) -> list[dict]:
        """What needs doing for sales, most urgent first."""
        today = self._now().date()
        out = []
        for s in self.sales(user_id):
            d = json.loads(s["data"]) if isinstance(s["data"], str) else s["data"]
            title = self.item(s["item_id"]).title
            ship_by = date.fromisoformat(d["plan"]["ship_by"])
            if s["status"] in ("sold", "label_ready"):
                days = (ship_by - today).days
                urgency = 0 if days < 0 else 1 if days <= 1 else 2
                what = "overdue: ship today" if days < 0 else f"ship by {ship_by.isoformat()}"
                step = ("drop it off" if s["status"] == "label_ready"
                        else "pack it and get the label: " + d["plan"]["label_note"])
                out.append({"sale_id": s["id"], "item": title, "urgency": urgency, "todo": f"{what}; {step}",
                            "package": d["plan"]["package"], "packing": d["plan"]["packing"]})
            elif s["status"] == "delivered":
                out.append({"sale_id": s["id"], "item": title, "urgency": 3,
                            "todo": "delivered; mark completed once the payout arrives"})
        return sorted(out, key=lambda t: t["urgency"])

    def sales(self, user_id: str) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM sell_sales WHERE user_id = ? ORDER BY at", (user_id,))]

    def items(self, user_id: str) -> list[dict]:
        out = []
        for r in self.db.execute("SELECT id FROM sell_items WHERE user_id = ? ORDER BY created_at", (user_id,)):
            it, m = self.item(r["id"]), self.mandate(r["id"])
            out.append({"id": it.id, "title": it.title, "status": it.status,
                        "floor": m.floor if m else None, "instruction": m.status if m else None,
                        "listings": [{k: l[k] for k in ("platform", "status", "price", "url")}
                                     for l in self.listings(it.id)]})
        return out

    # ---------------------------------------------------------------- one click
    def quick_sell(self, user_id: str, title: str, floor: float, target: float | None = None,
                   condition: str = "good", category: str | None = None, photos: list[str] | None = None,
                   product_id: str | None = None, description: str = "", auto: bool = True,
                   agent_id: str | None = None, kits: int = 2) -> dict:
        """Photo + price range -> listed (or sold to a buyback vendor) in one step.

        Called by the user (CLI) the floor is active at once; called by an agent it waits
        for the user's one tap, and listing happens when the agent calls again after that."""
        it = self.new_item(user_id, title, condition, category, description, product_id, photos)
        appraisal = self.appraise(it.id)
        m = self.set_floor(it.id, floor, target, auto, by=f"agent:{agent_id}" if agent_id else "user")
        out = {"item_id": it.id, "valuation": appraisal["valuation"], "options": appraisal["options"],
               "instruction": m.to_dict(), "actions": []}
        if m.status != "active":
            out["next"] = "waiting for your one-tap approval of the price range"
            return out
        return {**out, **self.go(it.id, agent_id, kits)}

    def go(self, item_id: str, agent_id: str | None = None, kits: int = 2) -> dict:
        """Act on an active instruction: take a buyback at/above the floor, else list on the
        best one-click platform, and prepare kits for the best manual platforms."""
        it, m = self.item(item_id), self._active(item_id)
        appraisal = self.appraise(item_id)
        actions = []
        for o in appraisal["options"]:
            if o["platform"].startswith("buyback:") and o["est_payout"] >= m.floor and m.auto:
                if m.platforms and o["platform"] not in m.platforms:
                    continue
                sale = self.accept_buyback(item_id, o["platform"].split(":", 1)[1], agent_id)
                return {"actions": [{"sold_to": o["platform"], **sale}]}
        v = appraisal["valuation"]
        price = max(m.target or 0, v.get("likely") or 0, m.floor * 1.2)
        price = round(price, 2)
        one_click = [o for o in appraisal["options"] if o["one_click"] and not o["platform"].startswith("buyback:")
                     and (not m.platforms or o["platform"] in m.platforms)]
        if one_click:
            actions.append(self.list_item(item_id, one_click[0]["platform"], price, agent_id))
        manual = [o for o in appraisal["options"] if not o["one_click"]
                  and (not m.platforms or o["platform"] in m.platforms)][:kits]
        for o in manual:
            actions.append(self.list_item(item_id, o["platform"], price, agent_id))
        signup = [{"platform": o["name"], "signup": o["signup"], "est_payout": o["est_payout"]}
                  for o in appraisal["options"] if o.get("signup")]
        return {"actions": actions, "list_price": price, "consider_signing_up": signup[:3]}
