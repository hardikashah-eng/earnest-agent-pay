import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from earnest import Gateway, MerchantRegistry, ValidationError
from earnest.cli import main as cli_main
from earnest.confidence import WEIGHTS, budget, price
from earnest.history import History
from earnest.insights.orders import parse_orders_csv, parse_wishlist_csv
from earnest.ledger import Ledger
from earnest.simulate import FakeClock

ROOT = Path(__file__).resolve().parents[1]
ORDERS = ROOT / "examples" / "orders"
USER = "u1"
REG = MerchantRegistry.default()


def make_gateway(max_budget=100, **profile):
    gw = Gateway(clock=FakeClock(datetime(2026, 11, 1, 12, tzinfo=timezone.utc)))
    gw.set_profile({"user_id": USER, **profile})
    gw.add_mandate({"id": "m1", "user_id": USER, "intent": "restock household batteries and cables",
                    "task_type": "shopping", "budget": {"max": max_budget}})
    return gw


def seed(gw, points):
    gw.ledger.add_points(USER, "a1", "shopping", "seed", points, None, gw.clock())


def req(merchant, amount, item=None, **kw):
    d = {"agent_id": "a1", "mandate_id": "m1", "amount": amount, "merchant": merchant,
         "category": "household", "reason": "restock", **kw}
    if item:
        d["item"] = item
    return d


def with_amazon_orders(gw):
    orders = parse_orders_csv((ORDERS / "amazon-order-history.csv").read_text(), USER)
    gw.import_orders(USER, orders)


class TestOrderImport(unittest.TestCase):
    def test_amazon_export(self):
        orders = parse_orders_csv((ORDERS / "amazon-order-history.csv").read_text(), USER)
        self.assertEqual(len(orders), 4)  # the cancelled standing desk is skipped
        cable = next(o for o in orders if "Cable" in o.item)
        self.assertEqual((cable.merchant, cable.unit_price, cable.quantity, cable.date),
                         ("Amazon.com", 12.99, 2, date(2026, 7, 2)))

    def test_generic_receipts_need_a_retailer(self):
        text = (ORDERS / "costco-receipts.csv").read_text()
        with self.assertRaises(ValidationError):
            parse_orders_csv(text, USER)
        orders = parse_orders_csv(text, USER, retailer="costco.com")
        self.assertEqual({o.merchant for o in orders}, {"costco.com"})

    def test_reimport_does_not_double_count(self):
        gw = make_gateway()
        orders = parse_orders_csv((ORDERS / "costco-receipts.csv").read_text(), USER, "costco.com")
        self.assertEqual(gw.import_orders(USER, orders), (4, 0))
        self.assertEqual(gw.import_orders(USER, orders), (0, 4))

    def test_total_only_columns_give_unit_price(self):
        orders = parse_orders_csv("Order Date,Title,Item Total,Quantity\n2026-01-02,Widget,30.00,3\n",
                                  USER, "walmart.com")
        self.assertEqual(orders[0].unit_price, 10.0)

    def test_wishlist(self):
        items = parse_wishlist_csv((ORDERS / "wishlist.csv").read_text())
        self.assertEqual(items[0].merchant, "amazon.com")
        self.assertEqual(items[1].max_price, 250)


class TestFactors(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(WEIGHTS.values()), 1.0)

    def test_price_in_range_and_jump(self):
        h = History(same_item_prices=[15.99, 16.49])
        self.assertEqual(price(17.5, h, None)[0], 1.0)       # within 10% of the highest
        f, _, sig = price(32.0, h, None)
        self.assertLess(f, 0.3)
        self.assertIn("price_above_previous", sig)
        self.assertEqual(price(10, History(), None)[0], 0.5)  # no reference

    def test_budget_headroom(self):
        self.assertEqual(budget(20, 100, 0)[0], 1.0)
        self.assertEqual(budget(100, 100, 0)[0], 0.6)
        self.assertEqual(budget(60, 100, 50)[0], 0.0)
        self.assertEqual(budget(60, 80, 0, budget_min=50)[0], 1.0)


class TestConfidenceInDecisions(unittest.TestCase):
    def test_order_history_repeat_is_high_confidence_and_routine(self):
        gw = make_gateway()
        seed(gw, 1)  # "new" tier: cap $25, approval over $10
        with_amazon_orders(gw)
        d = gw.request_spend(req("amazon.com", 16.99, "AA batteries 24 pack"))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.confidence["level"], "high")
        self.assertIn("routine_repeat_purchase", d.confidence["signals"])
        self.assertIn("price_in_range", d.confidence["signals"])

    def test_price_jump_is_not_routine(self):
        gw = make_gateway()
        seed(gw, 1)
        with_amazon_orders(gw)
        d = gw.request_spend(req("amazon.com", 24.99, "AA batteries 24 pack"))
        self.assertEqual(d.status, "escalated")
        self.assertIn("above_approval_threshold", d.reasons)
        self.assertIn("price_above_previous", d.confidence["signals"])
        self.assertLess(d.confidence["factors"]["price"]["score"], 0.5)

    def test_routine_never_lifts_the_tier_cap(self):
        gw = make_gateway()
        seed(gw, 1)
        with_amazon_orders(gw)
        d = gw.request_spend(req("amazon.com", 33.0, "AA batteries 24 pack", quantity=2))
        self.assertIn("above_tier_cap", d.reasons)
        self.assertEqual(d.confidence["factors"]["price"]["score"], 1.0)  # $16.50 a unit

    def test_card_statement_and_membership_vouch_for_an_unknown_store(self):
        gw = make_gateway(hard_rules=["no_new_merchants"])
        seed(gw, 1)
        gw.add_membership(USER, "Local Food Co-op", "owner")
        d = gw.request_spend(req("Local Food Co-op", 8))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.confidence["factors"]["authenticity"]["score"], 0.9)

    def test_membership_at_verified_store(self):
        gw = make_gateway()
        gw.add_membership(USER, "costco.com", "executive")
        c = gw.check_merchant(USER, "Costco", "olive oil", 20)
        self.assertEqual(c["factors"]["authenticity"]["score"], 1.0)
        self.assertIn("member:executive", c["signals"])

    def test_wishlist_interest(self):
        gw = make_gateway(max_budget=400)
        gw.add_interest(USER, "Electric Standing Desk 48 inch", "amazon.com", 350, "wishlist")
        c = gw.check_merchant(USER, "amazon.com", "electric standing desk 48in", 329, mandate_id="m1")
        self.assertEqual(c["factors"]["familiarity"]["score"], 0.8)
        self.assertEqual(c["factors"]["price"]["score"], 0.9)
        self.assertIn("shown_interest:wishlist", c["signals"])
        over = gw.check_merchant(USER, "amazon.com", "electric standing desk 48in", 399)
        self.assertEqual(over["factors"]["price"]["score"], 0.3)
        # an interest tied to Amazon doesn't vouch for the same item elsewhere
        self.assertNotIn("interest", gw.check_merchant(USER, "walmart.com", "standing desk 48 inch", 329)["history"])

    def test_same_item_from_another_store_counts(self):
        gw = make_gateway()
        with_amazon_orders(gw)
        c = gw.check_merchant(USER, "target.com", "Brita water filter replacement 6 pack", 23.99)
        self.assertEqual(c["factors"]["familiarity"]["score"], 0.75)
        self.assertEqual(c["factors"]["price"]["score"], 1.0)

    def test_mandate_intent_match(self):
        gw = make_gateway()
        c = gw.check_merchant(USER, "walmart.com", "AAA batteries", 9, mandate_id="m1")
        self.assertEqual(c["factors"]["familiarity"]["score"], 0.55)

    def test_low_confidence_escalates(self):
        gw = make_gateway(min_confidence=0.6, novelty=0.9)
        seed(gw, 30)
        d = gw.request_spend(req("some-new-shop.com", 5))
        self.assertEqual(d.status, "escalated")
        self.assertTrue(any(r.startswith("low_confidence:") for r in d.reasons))
        gw2 = make_gateway(novelty=0.9)  # default 0.35
        seed(gw2, 30)
        self.assertEqual(gw2.request_spend(req("some-new-shop.com", 5)).status, "approved")

    def test_untrusted_scores_zero(self):
        gw = make_gateway()
        with_amazon_orders(gw)
        c = gw.check_merchant(USER, "amaz0n.com", "AA batteries 24 pack", 16)
        self.assertEqual((c["score"], c["level"]), (0.0, "low"))

    def test_other_users_mandate_is_ignored(self):
        gw = make_gateway()
        gw.add_mandate({"id": "m2", "user_id": "someone_else", "intent": "x", "task_type": "t",
                        "budget": {"max": 5}})
        c = gw.check_merchant(USER, "amazon.com", "lamp", 50, mandate_id="m2")
        self.assertEqual(c["factors"]["budget"]["why"], "no mandate")


class TestCli(unittest.TestCase):
    def test_orders_membership_interest(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            run = lambda *a: cli_main(["--db", db, *a, "--user", USER])  # noqa: E731
            self.assertEqual(run("orders", "import", str(ORDERS / "costco-receipts.csv"), "--retailer", "costco.com"), 0)
            self.assertEqual(run("membership", "add", "costco.com", "--program", "executive"), 0)
            self.assertEqual(run("interest", "add", "olive oil 2L", "--max-price", "30"), 0)
            ledger = Ledger(db)
            self.assertEqual(len(ledger.orders(USER)), 4)
            self.assertEqual(ledger.memberships(USER)[0]["program"], "executive")
            self.assertEqual(ledger.interests(USER)[0]["source"], "user")
            ledger.close()


if __name__ == "__main__":
    unittest.main()
