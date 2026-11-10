import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.approvals import MemoryNotifier
from earnest.cli import main as cli_main
from earnest.selling import (Comp, EbayComps, EbayConfig, EbayInventoryAdapter, MockBuyback, MockMarketplace,
                             SellDesk, SellItem, StaticComps, recommend, value)
from earnest.selling.valuation import fee, payout
from earnest.simulate import FakeClock

U = "user_demo"
NOW = datetime(2026, 11, 10, 12, tzinfo=timezone.utc)
HOBBIT = "gtin:09780547928227"


def comps(*prices, kind="sold", condition="good"):
    return [Comp(p, kind, "test", condition) for p in prices]


def make_desk(comp_list=None, buyback=None, link=True):
    notes = MemoryNotifier()
    gw = Gateway(clock=FakeClock(NOW), notifier=notes)
    market = MockMarketplace()
    desk = SellDesk(gw, {"mockbay": market}, buyback or [], [StaticComps(comp_list or comps(12, 14, 15, 16, 18))])
    if link:
        desk.link_platform(U, "mockbay")
    return gw, desk, market, notes


def score(gw, agent="a1"):
    return gw.limits(U, agent, "selling")["score"]


class TestValuation(unittest.TestCase):
    def test_condition_and_asking_prices(self):
        v = value(comps(10, 10, 10, 10, 10), "like_new")
        self.assertEqual((v.likely, v.confidence), (11.0, "high"))
        v = value(comps(20, 20, 20, 20, kind="active"), "good")
        self.assertEqual(v.likely, 17.0)                      # asks discounted 15%
        self.assertIn("no sold prices", v.notes[0])
        self.assertEqual(value([], "good").likely, None)

    def test_buyback_sets_the_quick_sale_floor(self):
        v = value(comps(10, 12, 14, 16) + [Comp(13.5, "buyback", "buyback:x")], "good")
        self.assertEqual(v.quick_sale, 13.5)
        self.assertEqual(value([Comp(6, "buyback", "buyback:x")]).quick_sale, 6)

    def test_fees_and_payouts(self):
        self.assertEqual(fee("ebay", 100, "electronics"), 13.65)
        self.assertEqual(fee("ebay", 8, "toys"), round(8 * 0.1325 + 0.30, 2))
        self.assertEqual(fee("ebay", 20, "books"), round(20 * 0.153 + 0.40, 2))
        self.assertEqual(fee("poshmark", 12, "apparel"), 2.95)
        self.assertEqual(fee("facebook", 50, "home", shipped=False), 0.0)
        self.assertEqual(payout("poshmark", 50, "apparel"), 40.0)       # buyer pays shipping

    def test_recommendations(self):
        v = value(comps(14, 15, 16, 15, 15))
        opts = recommend(v, "books", linked={"ebay"})
        names = [o.platform for o in opts]
        self.assertNotIn("poshmark", names)                    # not for books
        ebay = next(o for o in opts if o.platform == "ebay")
        self.assertTrue(ebay.one_click)
        self.assertIsNone(ebay.signup)
        self.assertTrue(next(o for o in opts if o.platform == "mercari").signup.startswith("https://"))
        sofa = recommend(value(comps(200, 220, 240)), "furniture", linked=set())
        fb = next(o for o in sofa if o.platform == "facebook")
        self.assertEqual(fb.est_payout, fb.list_price)          # local pickup: no fee, no shipping
        self.assertEqual(sofa[0].platform, "facebook")


class FakeEbay:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if "get_category_suggestions" in path:
            return {"categorySuggestions": [{"category": {"categoryId": "261186"}}]}
        if path == "/sell/inventory/v1/offer":
            return {"offerId": "OFF1"}
        if path.endswith("/publish"):
            return {"listingId": "1234567890"}
        if path.startswith("/sell/fulfillment"):
            return {"orders": [{"orderId": "O-1", "lineItems": [{"sku": "sell_x", "total": {"value": "11.00"}}]}]}
        if path.startswith("/buy/browse"):
            return {"itemSummaries": [{"price": {"value": "20.00"}, "condition": "Very Good", "title": "Hobbit"}]}
        if path.startswith("/buy/marketplace_insights"):
            return {"itemSales": [{"lastSoldPrice": {"value": "14.00"}, "condition": "Good"}]}
        return {}


class TestEbay(unittest.TestCase):
    def setUp(self):
        self.fake = FakeEbay()
        self.ebay = EbayInventoryAdapter(self.fake, EbayConfig("PAY", "RET", "FUL", "home"))
        self.item = SellItem("sell_x", U, "The Hobbit paperback", "good", "books", "", HOBBIT,
                             ["https://img.example/hobbit.jpg"])

    def test_listing_carries_the_floor_as_auto_accept(self):
        lst = self.ebay.list(self.item, 15.0, 10.0, auto=True)
        inv = next(c for c in self.fake.calls if c[0] == "PUT")
        self.assertEqual(inv[2]["product"]["isbn"], ["9780547928227"])
        self.assertEqual(inv[2]["condition"], "USED_GOOD")
        offer = next(c for c in self.fake.calls if c[1] == "/sell/inventory/v1/offer")[2]
        terms = offer["listingPolicies"]["bestOfferTerms"]
        self.assertEqual(terms["autoAcceptPrice"], {"value": "10.00", "currency": "USD"})
        self.assertEqual(terms["autoDeclinePrice"]["value"], "8.00")
        self.assertEqual(offer["categoryId"], "261186")
        self.assertEqual((lst.auto_accept, lst.url), (10.0, "https://www.ebay.com/itm/1234567890"))

    def test_no_auto_accept_when_user_approves_each_offer(self):
        self.ebay.list(self.item, 15.0, 10.0, auto=False)
        offer = next(c for c in self.fake.calls if c[1] == "/sell/inventory/v1/offer")[2]
        self.assertNotIn("autoAcceptPrice", offer["listingPolicies"]["bestOfferTerms"])

    def test_needs_public_photos(self):
        self.item.photos = ["/home/me/hobbit.jpg"]
        with self.assertRaises(ValueError):
            self.ebay.list(self.item, 15.0, 10.0, auto=True)

    def test_sales_and_comps(self):
        self.assertEqual(self.ebay.poll_sales()[0].amount, 11.0)
        c = EbayComps(self.fake, sold_access=True).comps(self.item)
        self.assertEqual([(x.kind, x.price, x.condition) for x in c],
                         [("active", 20.0, "like_new"), ("sold", 14.0, "good")])
        self.assertIn("gtin=9780547928227", next(p for _, p, _ in self.fake.calls if p.startswith("/buy/browse")))


class TestSellDesk(unittest.TestCase):
    def test_agent_proposal_waits_for_one_tap(self):
        gw, desk, market, notes = make_desk()
        r = desk.quick_sell(U, "The Hobbit paperback", floor=10, target=15, category="books", agent_id="a1")
        self.assertEqual(r["instruction"]["status"], "pending")
        self.assertEqual(desk.listings(r["item_id"]), [])
        msg = notes.sent[0]
        self.assertIn("Sell The Hobbit paperback for $10.00-$15.00", msg.reason_given)
        m = gw.resolve_token(msg.approve_url.rsplit("/", 1)[1])
        self.assertEqual(m.status, "active")
        out = desk.go(r["item_id"], "a1")
        self.assertEqual(out["actions"][0]["status"], "listed")
        self.assertEqual(out["actions"][0]["auto_accept_at"], 10)
        self.assertGreaterEqual(out["list_price"], 15)

    def test_auto_accept_at_floor_then_everything_else_ends(self):
        gw, desk, market, _ = make_desk()
        r = desk.quick_sell(U, "The Hobbit paperback", floor=10, category="books")   # user: active at once
        kinds = [a["status"] for a in r["actions"]]
        self.assertEqual(kinds[0], "listed")
        self.assertIn("needs_user", kinds)                       # copy-paste kits for manual platforms
        ext = desk.listings(r["item_id"], "active")[0]["external_id"]
        self.assertEqual(market.receive_offer(ext, 7.0), "auto_declined")
        self.assertEqual(market.receive_offer(ext, 9.0), "pending")
        self.assertEqual(market.receive_offer(ext, 10.0), "auto_accepted")
        sale = desk.sync()[0]
        self.assertEqual((sale["amount"], sale["platform"]), (10.0, "mockbay"))
        self.assertTrue(all(o["status"] == "ended" for o in sale["other_listings"]))
        self.assertIn("remove_manually", sale)
        self.assertEqual(desk.item(r["item_id"]).status, "sold")
        with self.assertRaises(ValidationError):
            desk.record_sale(r["item_id"], "mercari", 12)       # no double sale

    def test_floor_is_enforced_and_violations_cost_trust(self):
        gw, desk, market, _ = make_desk()
        it = desk.new_item(U, "Kindle Paperwhite", "good", "electronics")
        desk.set_floor(it.id, 40)
        with self.assertRaises(ValidationError):
            desk.list_item(it.id, "mockbay", 35, agent_id="a1")
        self.assertEqual(score(gw), -10)
        desk.list_item(it.id, "mockbay", 55, agent_id="a1")
        self.assertEqual(desk.handle_offer(it.id, "mockbay", 30)["action"], "declined")
        with self.assertRaises(ValidationError):
            desk.accept_offer(it.id, "mockbay", 30, "offer-1", agent_id="a1")
        self.assertEqual(score(gw), -20)

    def test_accepting_a_pending_offer_at_floor_earns_trust(self):
        gw, desk, market, _ = make_desk()
        it = desk.new_item(U, "Kindle Paperwhite", "good", "electronics")
        desk.set_floor(it.id, 40)
        desk.list_item(it.id, "mockbay", 55, agent_id="a1")
        ext = desk.listings(it.id, "active")[0]["external_id"]
        market.listings[ext].auto_accept = None                 # platform without auto-accept
        market.receive_offer(ext, 42)
        r = desk.accept_offer(it.id, "mockbay", 42, "offer-1", agent_id="a1")
        self.assertEqual(r["action"], "accepted")
        self.assertEqual(score(gw), 2)

    def test_ask_each_offer_and_platform_limits(self):
        gw, desk, market, _ = make_desk()
        it = desk.new_item(U, "Desk lamp", "good", "home")
        desk.set_floor(it.id, 15, auto=False, platforms=["mercari"])
        self.assertEqual(desk.handle_offer(it.id, "mercari", 20)["action"], "ask_user")
        with self.assertRaises(ValidationError):
            desk.list_item(it.id, "mockbay", 20)
        kit = desk.list_item(it.id, "mercari", 20)["kit"]
        self.assertEqual(kit["price"], 20)
        self.assertIn("Accept offers of $15.00 or more", " ".join(kit["steps"]))

    def test_buyback_at_or_above_floor_sells_instantly(self):
        bb = MockBuyback("bookbuyer", {HOBBIT: 11.0})
        gw, desk, market, _ = make_desk(buyback=[bb])
        r = desk.quick_sell(U, "The Hobbit paperback", floor=10, category="books", product_id="9780547928227")
        self.assertEqual(r["actions"][0]["sold_to"], "buyback:bookbuyer")
        self.assertEqual(desk.item(r["item_id"]).status, "sold")
        r2 = desk.quick_sell(U, "The Hobbit paperback", floor=12, category="books", product_id="9780547928227")
        self.assertEqual(r2["actions"][0]["status"], "listed")  # quote below floor: list instead

    def test_expiry(self):
        gw, desk, market, _ = make_desk()
        it = desk.new_item(U, "Desk lamp", "good", "home")
        desk.set_floor(it.id, 15, days=7)
        gw.clock.advance(hours=24 * 8)
        with self.assertRaises(ValidationError):
            desk.go(it.id)

    def test_unlinked_platforms_suggest_signup(self):
        gw, desk, market, _ = make_desk(link=False)
        r = desk.quick_sell(U, "Nintendo Switch Lite", floor=12, category="electronics")
        self.assertTrue(all(a["status"] == "needs_user" for a in r["actions"]))
        self.assertTrue(r["consider_signing_up"])


class TestOneTapPage(unittest.TestCase):
    def test_sale_approval_page(self):
        import threading
        import urllib.request
        from earnest.server import serve
        gw, desk, market, notes = make_desk()
        gw.clock = lambda: datetime.now(timezone.utc)
        r = desk.quick_sell(U, "The Hobbit paperback", floor=10, category="books", agent_id="a1")
        server = serve(gw, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/a/" + notes.sent[0].approve_url.rsplit("/", 1)[1]
            self.assertIn("Approve this sale?", urllib.request.urlopen(url).read().decode())
            self.assertEqual(desk.mandate(r["item_id"]).status, "pending")
            done = urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST")).read().decode()
            self.assertIn("$10.00 or more", done)
            self.assertEqual(desk.mandate(r["item_id"]).status, "active")
        finally:
            server.shutdown()
            server.server_close()


class TestCli(unittest.TestCase):
    def test_quick_sell_from_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            self.assertEqual(cli_main(["--db", db, "sell", "quick", "--title", "Lego Classic 10698", "--floor", "25",
                                       "--target", "40", "--category", "toys", "--photo", "lego.jpg",
                                       "--user", U]), 0)
            self.assertEqual(cli_main(["--db", db, "sell", "items", "--user", U]), 0)


if __name__ == "__main__":
    unittest.main()
