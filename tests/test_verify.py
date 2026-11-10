import unittest
from datetime import date, datetime, timezone

from earnest import Gateway
from earnest.insights.orders import Order
from earnest.simulate import FakeClock
from earnest.verify import LineItem, OrderLine, check_cart, reconcile

USER = "u1"


def make_gateway():
    clock = FakeClock(datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": USER, "novelty": 0.9})
    gw.add_mandate({"id": "m1", "user_id": USER, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 500}})
    gw.ledger.add_points(USER, "a1", "shopping", "seed", 30, None, clock())
    return gw, clock


def buy(gw, item="AA batteries 24 pack", amount=18, merchant="amazon.com"):
    return gw.request_spend({"agent_id": "a1", "mandate_id": "m1", "amount": amount, "merchant": merchant,
                             "category": "household", "reason": "restock", "item": item})


def score(gw):
    return gw.limits(USER, "a1", "shopping")["score"]


class TestCart(unittest.TestCase):
    def test_cart_rules(self):
        ok = [LineItem("Amazon Basics AA Batteries, 24 Pack", 16.99), LineItem("Sales tax", 1.4)]
        self.assertEqual(check_cart("AA batteries 24 pack", ok), (True, "item_verified"))
        self.assertEqual(check_cart("AA batteries 24 pack", [LineItem("Amazon eGift Card", 18)])[1],
                         "item_mismatch:not_in_cart")
        extra = [LineItem("AA batteries 24 pack", 10), LineItem("Bluetooth speaker", 8)]
        self.assertEqual(check_cart("AA batteries 24 pack", extra)[1], "item_mismatch:extra_items")

    def test_reconcile_rules(self):
        d = date(2026, 11, 1)
        same = [OrderLine("Amazon Basics AA Batteries, 24 Pack", 17.5, date(2026, 11, 2))]
        self.assertEqual(reconcile("AA batteries 24 pack", 18, d, same, d)[0], "verified")
        other = [OrderLine("Amazon eGift Card", 18, date(2026, 11, 1))]
        self.assertEqual(reconcile("AA batteries 24 pack", 18, d, other, d)[0], "mismatch")
        self.assertEqual(reconcile("AA batteries 24 pack", 18, d, [], d)[0], "pending")
        self.assertEqual(reconcile("AA batteries 24 pack", 18, d, [], date(2026, 11, 20))[0], "unverified")


class TestGatewayVerification(unittest.TestCase):
    def test_card_declines_a_cart_without_the_claimed_item(self):
        gw, _ = make_gateway()
        d = buy(gw)
        self.assertEqual(d.credential.item_lock, "AA batteries 24 pack")
        r = gw.authorize_charge(d.credential.id, 18, "amazon.com", [{"item": "Amazon eGift Card", "amount": 18}])
        self.assertFalse(r["approved"])
        self.assertEqual(r["verification"]["status"], "mismatch")
        self.assertEqual(score(gw), 30 + 2 - 10)

    def test_matching_cart_is_verified_and_rewarded(self):
        gw, _ = make_gateway()
        d = buy(gw)
        r = gw.authorize_charge(d.credential.id, 18, "amazon.com",
                                [{"item": "Amazon Basics AA Batteries 24 Pack", "amount": 16.6},
                                 {"item": "Tax", "amount": 1.4}])
        self.assertTrue(r["approved"])
        self.assertEqual(score(gw), 30 + 2 + 1)

    def test_reconcile_against_order_history(self):
        gw, clock = make_gateway()
        good, bad = buy(gw), buy(gw, item="USB-C cable 6ft", amount=25)
        gw.import_orders(USER, [
            Order("o1", date(2026, 11, 2), "Amazon.com", "Amazon Basics AA Batteries, 24 Pack", 18.0),
            Order("o2", date(2026, 11, 2), "Amazon.com", "Amazon Gift Card", 25.0)])
        results = {r["request_id"]: r["status"] for r in gw.reconcile(USER)}
        self.assertEqual(results, {good.request_id: "verified", bad.request_id: "mismatch"})
        self.assertEqual(gw.reconcile(USER), [])  # already settled

    def test_mismatch_suspends_routine_pass_and_drops_the_fake_item(self):
        gw, clock = make_gateway()
        gw.import_orders(USER, [Order("o0", date(2026, 9, 1), "Amazon.com", "AA batteries 24 pack", 17.0)])
        self.assertIn("routine_repeat_purchase", buy(gw).confidence["signals"])
        lie = buy(gw, item="Brita filter 6 pack", amount=40)
        gw.authorize_charge(lie.credential.id, 40, "amazon.com", [{"item": "Gift card", "amount": 40}])
        d = buy(gw, amount=17)
        self.assertIn("routine_suspended", d.confidence["signals"])
        hist = gw.check_merchant(USER, "amazon.com", "Brita filter 6 pack", 40)["history"]
        self.assertEqual(hist["same_item"], 0)  # the false claim doesn't count as history
        clock.advance(hours=24 * 91)
        self.assertNotIn("routine_suspended", buy(gw, amount=17).confidence["signals"])

    def test_cards_without_item_or_line_items_behave_as_before(self):
        gw, _ = make_gateway()
        d = gw.request_spend({"agent_id": "a1", "mandate_id": "m1", "amount": 5, "merchant": "amazon.com",
                              "category": "household", "reason": "x"})
        self.assertIsNone(d.credential.item_lock)
        self.assertTrue(gw.authorize_charge(d.credential.id, 5, "amazon.com")["approved"])


if __name__ == "__main__":
    unittest.main()
