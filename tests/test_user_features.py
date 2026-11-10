import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.approvals import MemoryNotifier
from earnest.cli import main as cli_main
from earnest.insights.orders import Order
from earnest.ledger import Ledger
from earnest.returns import reminders, window
from earnest.simulate import FakeClock

U = "user_demo"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def make_gateway(points=30, user=U, **kw):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock, **kw)
    gw.set_profile({"user_id": user, "novelty": 0.9})
    gw.add_mandate({"id": "m1", "user_id": user, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 1000}})
    gw.ledger.add_points(user, "a1", "shopping", "seed", points, None, clock())
    return gw, clock


def req(amount=20, merchant="amazon.com", item="AA batteries 24 pack", mandate="m1", **kw):
    return {"agent_id": "a1", "mandate_id": mandate, "amount": amount, "merchant": merchant,
            "category": kw.pop("category", "household"), "reason": "restock", "item": item, **kw}


def score(gw, user=U, task="shopping"):
    return gw.limits(user, "a1", task)["score"]


class TestReturns(unittest.TestCase):
    def test_windows(self):
        self.assertEqual(window("amazon").days, 30)
        self.assertEqual(window("walmart", "electronics").days, 30)
        self.assertEqual(window("target", "household", ["Circle Card"]).days, 120)
        self.assertEqual(window("bestbuy", "electronics", ["My Best Buy Plus"]).days, 60)
        self.assertTrue(window("costco").open_ended)
        self.assertEqual(window("costco", "tv").days, 90)
        self.assertFalse(window("nike").known)

    def test_reminders(self):
        today = date(2026, 11, 20)
        ps = [{"merchant": "amazon.com", "merchant_id": "amazon", "item": "lamp", "bought": date(2026, 11, 1),
               "verdict": "rejected", "request_id": "r1"},
              {"merchant": "apple.com", "merchant_id": "apple", "item": "case", "bought": date(2026, 11, 10)},
              {"merchant": "apple.com", "merchant_id": "apple", "item": "old", "bought": date(2026, 10, 1)},
              {"merchant": "walmart.com", "merchant_id": "walmart", "item": "fan", "bought": date(2026, 11, 1)}]
        r = reminders(ps, today)
        self.assertEqual([(x.item, x.priority) for x in r], [("lamp", "return_now"), ("case", "soon")])

    def test_confidence_and_mandate(self):
        gw, _ = make_gateway()
        self.assertIn("returnable:30", gw.check_merchant(U, "amazon.com", "lamp", 20)["signals"])
        gw.add_mandate({"id": "m2", "user_id": U, "intent": "gift", "task_type": "shopping",
                        "budget": {"max": 100}, "requires_returnable": True})
        d = gw.request_spend(req(merchant="nike.com", mandate="m2"))
        self.assertIn("return_policy_unknown", d.reasons)
        d = gw.request_spend(req(merchant="amazon.com", mandate="m2"))
        self.assertTrue(gw.ap2_intent_mandate(d.request_id)["intent_mandate"]["requires_refundability"])


class TestReorder(unittest.TestCase):
    def with_history(self, gw):
        gw.import_orders(U, [Order(f"o{i}", d, "Amazon.com", "Amazon Basics AA Batteries, 24 Pack", 16.0 + i)
                             for i, d in enumerate([date(2026, 2, 1), date(2026, 5, 3), date(2026, 8, 1)])])

    def test_predicts_next_restock(self):
        gw, _ = make_gateway()
        self.with_history(gw)
        p = gw.reorder_predictions(U)[0]
        self.assertEqual((p["every_days"], p["next_due"], p["usual_merchant"]), (90, "2026-10-30", "amazon"))
        self.assertEqual(p["typical_price"], 17.0)
        self.assertEqual(gw.reorder_predictions(U, within_days=-10), [])

    def test_standing_mandate(self):
        gw, clock = make_gateway(points=1)  # "new" tier: cap $25, approval over $10
        self.with_history(gw)
        m = gw.add_standing_mandate(U, "AA batteries 24 pack", 20, every_days=60, task_type="shopping")
        ok = gw.request_spend(req(18, mandate=m.id))
        self.assertEqual(ok.status, "approved")              # over $10 but pre-approved
        self.assertIn("standing_mandate", ok.confidence["signals"])
        too_soon = gw.request_spend(req(18, mandate=m.id))
        self.assertIn("standing:too_soon", too_soon.reasons)
        clock.advance(hours=24 * 61)
        self.assertIn("standing:over_price", gw.request_spend(req(24, mandate=m.id)).reasons)
        wrong = gw.request_spend(req(18, item="Gift card", mandate=m.id))
        self.assertEqual(wrong.status, "denied")
        self.assertIn("standing:item_mismatch", wrong.reasons)


class TestFollowups(unittest.TestCase):
    def test_outcome_delivery_price_drop_and_renewal(self):
        gw, clock = make_gateway()
        lamp = gw.request_spend(req(40, item="Desk lamp LED"))
        sub = gw.request_spend(req(9.99, item="Cloud storage plan", recurring=True))
        clock.advance(hours=24 * 11)
        gw.report_price("amazon.com", "Desk lamp LED", 30.0)
        kinds = {(f["kind"], f.get("request_id")) for f in gw.followups(U)["followups"]}
        self.assertIn(("outcome", lamp.request_id), kinds)
        self.assertIn(("delivery", lamp.request_id), kinds)
        drop = next(f for f in gw.followups(U)["followups"] if f["kind"] == "price_drop")
        self.assertEqual(drop["saving"], 10.0)
        clock.advance(hours=24 * 15)  # day 26: renews on day 30
        self.assertIn(("renewal", sub.request_id),
                      {(f["kind"], f.get("request_id")) for f in gw.followups(U)["followups"]})

    def test_auto_accept_after_return_window(self):
        gw, clock = make_gateway()
        d = gw.request_spend(req(18))
        gw.authorize_charge(d.credential.id, 18, "amazon.com", [{"item": "AA batteries 24 pack", "amount": 18}])
        clock.advance(hours=24 * 29)
        self.assertEqual(gw.followups(U)["auto_accepted"], [])
        clock.advance(hours=24 * 2)
        self.assertEqual(gw.followups(U)["auto_accepted"], [d.request_id])
        self.assertIn("outcome_accepted", {e["kind"] for e in gw.ledger.request_events(d.request_id)})
        self.assertEqual(gw.ledger.get_outcome(d.request_id)["verdict"], "accepted")

    def test_auto_accept_can_be_turned_off(self):
        gw, clock = make_gateway()
        gw.set_profile({"user_id": U, "novelty": 0.9, "auto_accept_kept": False})
        d = gw.request_spend(req(18))
        gw.authorize_charge(d.credential.id, 18, "amazon.com", [{"item": "AA batteries 24 pack", "amount": 18}])
        clock.advance(hours=24 * 40)
        self.assertEqual(gw.followups(U)["auto_accepted"], [])


class TestHouseholds(unittest.TestCase):
    def family(self, **kw):
        notes = MemoryNotifier()
        gw, clock = make_gateway(notifier=notes)
        for u in ("user_partner", "user_kid"):
            gw.set_profile({"user_id": u, "novelty": 0.9})
            gw.ledger.add_points(u, "a1", "shopping", "seed", 30, None, clock())
        gw.add_mandate({"id": "kid", "user_id": "user_kid", "intent": "school supplies", "task_type": "shopping",
                        "budget": {"max": 500}})
        h = json.loads((Path(__file__).resolve().parents[1] / "examples/households/family.json").read_text())
        h.update(kw)
        gw.set_household(h["id"], h["name"], h["members"], h.get("monthly_budget"), h.get("second_approval_over"))
        return gw, notes

    def test_child_needs_a_guardian(self):
        gw, notes = self.family()
        d = gw.request_spend(req(12, item="notebooks", mandate="kid"))
        self.assertIn("household:child_needs_guardian", d.reasons)
        self.assertEqual({m.approver for m in notes.sent}, {"user_demo", "user_partner"})
        with self.assertRaises(ValidationError):
            gw.resolve(d.request_id, True, approver="user_kid")
        self.assertIn(d.request_id, {p["request_id"] for p in gw.pending_for("user_partner")})
        self.assertEqual(gw.resolve(d.request_id, True, approver="user_partner").status, "approved")

    def test_allowance_and_household_budget(self):
        gw, _ = self.family()
        d = gw.request_spend(req(30, item="notebooks", mandate="kid"))
        gw.resolve(d.request_id, True, approver="user_demo")
        self.assertIn("household:over_allowance", gw.request_spend(req(15, item="pens", mandate="kid")).reasons)
        gw2, _ = self.family(monthly_budget=100)
        gw2.request_spend(req(90))
        self.assertIn("household:over_monthly_budget", gw2.request_spend(req(20)).reasons)

    def test_two_adults_for_big_purchases(self):
        gw, notes = self.family()
        gw.ledger.add_points(U, "a1", "shopping", "seed", 50, None, gw.clock())  # trusted: no other escalation
        d = gw.request_spend(req(400, item="standing desk"))
        self.assertEqual(d.reasons, ["household:needs_two_adults"])
        first = gw.resolve(d.request_id, True, approver="user_demo")
        self.assertEqual((first.status, first.reasons[-1]), ("escalated", "awaiting_second_approval"))
        self.assertEqual(gw.resolve(d.request_id, True, approver="user_demo").status, "escalated")
        token = next(m for m in notes.sent if m.approver == "user_partner").approve_url.rsplit("/", 1)[1]
        self.assertEqual(gw.resolve_token(token).status, "approved")


class TestDisputes(unittest.TestCase):
    def test_pack_for_a_mismatched_charge(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req(25))
        gw.authorize_charge(d.credential.id, 25, "amazon.com", [{"item": "Gift card", "amount": 25}])
        pack = gw.dispute_pack(d.request_id)
        self.assertIn("different item", pack["suggested_dispute_reason"])
        self.assertEqual(pack["agent_request"]["item"], "AA batteries 24 pack")
        self.assertEqual(len(pack["sha256"]), 64)
        self.assertTrue(any("item check: mismatch" in t["what"] for t in pack["timeline"]))
        self.assertEqual(gw.dispute_pack(d.request_id)["sha256"], pack["sha256"])

    def test_cli_writes_json_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            gw = Gateway(Ledger(db))
            gw.set_profile({"user_id": U, "novelty": 0.9})
            gw.add_mandate({"id": "m1", "user_id": U, "intent": "x", "task_type": "shopping", "budget": {"max": 50}})
            d = gw.request_spend(req(5))
            gw.ledger.close()
            self.assertEqual(cli_main(["--db", db, "dispute", d.request_id, "--out", tmp]), 0)
            self.assertIn("Suggested dispute reason", (Path(tmp) / f"{d.request_id}.md").read_text())


if __name__ == "__main__":
    unittest.main()
