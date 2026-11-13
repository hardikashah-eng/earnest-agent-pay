import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.accounts import Funding, effective_ceiling, pick_funding, raise_offer
from earnest.backends.stripe_issuing import StripeIssuingBackend
from earnest.cli import main as cli_main
from earnest.simulate import FakeClock

U, A = "u1", "shopper"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def make_gateway(points=30, **kw):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock, **kw)
    gw.set_profile({"user_id": U, "novelty": 0.9})
    gw.add_mandate({"id": "m1", "user_id": U, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 500}})
    gw.ledger.add_points(U, A, "shopping", "seed", points, None, clock())   # 30 = established, $200 cap
    return gw, clock


def req(amount=20, merchant="amazon.com", item="AA batteries 24 pack", **kw):
    return {"agent_id": A, "mandate_id": "m1", "amount": amount, "merchant": merchant,
            "category": kw.pop("category", "household"), "reason": "restock", "item": item, **kw}


class TestPickFunding(unittest.TestCase):
    def setUp(self):
        self.allowance = Funding("allowance", "allowance", 40)
        self.target = Funding("g1", "gift_card", 50, "target.com", "4417", merchant_keys=frozenset({"id:target"}))
        self.small_target = Funding("g2", "gift_card", 10, "target.com", "1111", merchant_keys=frozenset({"id:target"}))
        self.prepaid = Funding("p1", "prepaid", 100, last4="9999")

    def test_gift_card_for_the_store_first(self):
        srcs = [self.allowance, self.target, self.prepaid]
        self.assertEqual(pick_funding(srcs, {"id:target"}, 30).id, "g1")
        self.assertEqual(pick_funding(srcs, {"id:amazon"}, 30).id, "allowance")   # gift card is store-locked
        self.assertEqual(pick_funding(srcs, {"id:amazon"}, 80).id, "p1")         # only the prepaid covers it
        self.assertIsNone(pick_funding(srcs, {"id:amazon"}, 150))

    def test_smallest_card_that_covers(self):
        srcs = [self.target, self.small_target]
        self.assertEqual(pick_funding(srcs, {"id:target"}, 8).id, "g2")
        self.assertEqual(pick_funding(srcs, {"id:target"}, 12).id, "g1")

    def test_ceiling_and_offers(self):
        self.assertEqual(effective_ceiling(None, 200), 200)
        self.assertEqual(effective_ceiling(25, 200), 25)       # raises need the user's OK
        self.assertEqual(effective_ceiling(200, 25), 25)       # decreases are automatic
        self.assertEqual(raise_offer(25, 200), {"from": 25, "to": 200})
        self.assertIsNone(raise_offer(200, 200))


class TestAccounts(unittest.TestCase):
    def test_open_caps_allowance_at_tier(self):
        gw, _ = make_gateway(points=0)   # new: $25
        acct = gw.open_account(U, A, allowance=100)
        self.assertEqual(acct["allowance"]["balance"], 25)
        self.assertIn("only $25.00 of $100.00", acct["note"])
        with self.assertRaises(ValidationError):
            gw.top_up(U, A, 5)   # already at the ceiling

    def test_allowance_purchase_holds_and_issues_card(self):
        gw, _ = make_gateway()
        gw.open_account(U, A, allowance=50)
        d = gw.request_spend(req(30))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.payment["method"], "allowance")
        self.assertEqual(d.payment["remaining"], 20)
        self.assertEqual(d.credential.account, f"{U}:{A}")
        self.assertEqual(gw.account(U, A)["balance"], 20)

    def test_gift_card_pays_at_its_store_without_a_card(self):
        gw, _ = make_gateway()
        gw.open_account(U, A)
        fid = gw.add_card(U, A, "gift_card", 50, "4417", "target.com")["funding_id"]
        d = gw.request_spend(req(30, "target.com"))
        self.assertEqual(d.status, "approved")
        self.assertIsNone(d.credential)
        self.assertEqual(d.payment["funding_id"], fid)
        self.assertIn("ending 4417", d.payment["pay_with"])
        # it doesn't pay anywhere else
        d = gw.request_spend(req(10, "amazon.com"))
        self.assertEqual(d.status, "escalated")
        self.assertIn("account:insufficient_balance:0.00", d.reasons)

    def test_add_card_validation(self):
        gw, _ = make_gateway()
        gw.open_account(U, A)
        for bad in (dict(kind="gift_card", balance=50, last4="12345", merchant="target.com"),
                    dict(kind="gift_card", balance=50, last4="1234"),
                    dict(kind="gift_card", balance=0, last4="1234", merchant="target.com"),
                    dict(kind="gift_card", balance=50, last4="1234", merchant="amaz0n.com"),
                    dict(kind="cash", balance=50, last4="1234")):
            with self.assertRaises(ValidationError, msg=bad):
                gw.add_card(U, A, **bad)
        notes = gw.add_card(U, A, "prepaid", 300, "9999")["notes"]
        self.assertTrue(any("per-purchase limit" in n for n in notes))
        self.assertTrue(any("any store" in n for n in notes))

    def test_never_stores_more_than_last4(self):
        gw, _ = make_gateway()
        gw.open_account(U, A)
        gw.add_card(U, A, "gift_card", 50, "4417", "target.com")
        rows = gw.ledger.db.execute("SELECT * FROM account_funding WHERE kind = 'gift_card'").fetchall()
        self.assertEqual(rows[0]["last4"], "4417")
        self.assertEqual(set(rows[0].keys()), {"id", "user_id", "agent_id", "kind", "merchant", "last4", "label",
                                               "active", "created_at"})

    def test_insufficient_balance_escalates_and_approval_needs_funds(self):
        gw, _ = make_gateway()
        gw.open_account(U, A, allowance=10)
        d = gw.request_spend(req(30))
        self.assertEqual(d.status, "escalated")
        self.assertIn("account:insufficient_balance:10.00", d.reasons)
        with self.assertRaises(ValidationError):
            gw.resolve(d.request_id, approve=True)
        self.assertEqual(gw.ledger.get_request(d.request_id)["status"], "escalated")   # nothing recorded
        gw.top_up(U, A, 40)
        ok = gw.resolve(d.request_id, approve=True)
        self.assertEqual(ok.status, "approved")
        self.assertEqual(ok.payment["remaining"], 20)

    def test_violation_freezes_and_returns_allowance(self):
        gw, _ = make_gateway()
        gw.open_account(U, A, allowance=50)
        gw.add_card(U, A, "gift_card", 50, "4417", "target.com")
        d = gw.request_spend(req(600))   # over the mandate budget: a violation
        self.assertEqual(d.status, "denied")
        acct = gw.account(U, A)
        self.assertEqual(acct["status"], "frozen")
        self.assertEqual(acct["allowance"]["balance"], 0)
        self.assertEqual(acct["balance"], 50)   # the gift card can't be swept
        d = gw.request_spend(req(5, "target.com"))
        self.assertEqual((d.status, d.reasons), ("denied", ["account_frozen"]))
        self.assertEqual(gw.unfreeze(U, A)["status"], "active")
        self.assertEqual(gw.request_spend(req(5, "target.com")).status, "approved")

    def test_item_mismatch_and_dispute_freeze(self):
        gw, _ = make_gateway()
        gw.open_account(U, A, allowance=50)
        d = gw.request_spend(req(20))
        gw.authorize_charge(d.credential.id, 20, "amazon.com", [{"item": "gift card", "amount": 20}])
        self.assertEqual(gw.account(U, A)["status"], "frozen")
        gw.unfreeze(U, A)
        gw.top_up(U, A, 20)
        d = gw.request_spend(req(10))
        gw.report_outcome({"request_id": d.request_id, "verdict": "disputed"})
        self.assertEqual(gw.account(U, A)["status"], "frozen")

    def test_freeze_all(self):
        gw, clock = make_gateway()
        gw.ledger.add_points(U, "other", "shopping", "seed", 30, None, clock())
        gw.open_account(U, A, allowance=20)
        gw.open_account(U, "other", allowance=20)
        out = gw.freeze(U)
        self.assertEqual({o["agent_id"] for o in out}, {A, "other"})
        self.assertTrue(all(o["allowance_returned"] == 20 for o in out))
        with self.assertRaises(ValidationError):
            gw.top_up(U, A, 5)

    def test_no_account_unchanged(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req(20))
        self.assertEqual(d.status, "approved")
        self.assertIsNone(d.payment)
        self.assertIsNone(d.credential.account)

    def test_tier_drop_lowers_ceiling_and_sweeps(self):
        gw, clock = make_gateway()
        gw.open_account(U, A, allowance=150)
        gw.ledger.add_points(U, A, "shopping", "penalty", -20, None, clock())   # 10 points: new, $25
        acct = gw.account(U, A)
        self.assertEqual(acct["allowance"]["ceiling"]["effective"], 25)
        self.assertEqual(acct["allowance"]["balance"], 25)

    def test_raise_is_opt_in(self):
        gw, clock = make_gateway(points=0)
        gw.open_account(U, A, allowance=25)
        self.assertEqual(gw.account_offers(U), [])
        gw.ledger.add_points(U, A, "shopping", "earned", 30, None, clock())   # now established
        offers = gw.account_offers(U)
        self.assertEqual((offers[0]["raise_from"], offers[0]["raise_to"]), (25, 200))
        self.assertEqual(gw.account(U, A)["allowance"]["ceiling"]["effective"], 25)   # not until accepted
        self.assertEqual(gw.accept_raise(U, A)["allowance"]["ceiling"]["effective"], 200)
        self.assertEqual(gw.top_up(U, A, 100)["added"], 100)

    def test_refill(self):
        gw, clock = make_gateway()
        gw.open_account(U, A, allowance=50, topup_to=50, topup_every_days=7)
        gw.request_spend(req(30))
        self.assertEqual(gw.refill_accounts(U), [])   # topped up today
        clock.advance(days=7)
        out = gw.refill_accounts(U)
        self.assertEqual(out[0]["added"], 30)
        self.assertEqual(gw.account(U, A)["allowance"]["balance"], 50)
        with self.assertRaises(ValidationError):
            gw.open_account(U, "x", topup_to=50)   # needs both

    def test_refund_and_settle(self):
        gw, clock = make_gateway()
        gw.open_account(U, A, allowance=50)
        d = gw.request_spend(req(30))
        gw.authorize_charge(d.credential.id, 18, "amazon.com")
        clock.advance(days=2)   # the card expired
        self.assertEqual(gw.settle_accounts(U), [{"request_id": d.request_id, "released": 12}])
        self.assertEqual(gw.settle_accounts(U), [])   # once
        self.assertEqual(gw.refund(d.request_id)["refunded"], 18)
        with self.assertRaises(ValidationError):
            gw.refund(d.request_id, 1)
        self.assertEqual(gw.account(U, A)["balance"], 50)
        g = gw.add_card(U, A, "gift_card", 40, "4417", "target.com")["funding_id"]
        d = gw.request_spend(req(25, "target.com"))
        self.assertEqual(gw.refund(d.request_id, 10)["to"], g)

    def test_set_and_remove_card(self):
        gw, _ = make_gateway()
        gw.open_account(U, A)
        g = gw.add_card(U, A, "gift_card", 40, "4417", "target.com")["funding_id"]
        gw.set_card_balance(U, A, g, 32.5)
        self.assertEqual(gw.account(U, A)["balance"], 32.5)
        gw.remove_card(U, A, g)
        self.assertEqual(gw.account(U, A)["balance"], 0)
        with self.assertRaises(ValidationError):
            gw.set_card_balance(U, A, f"allowance:{U}:{A}", 5)

    def test_statement(self):
        gw, _ = make_gateway()
        gw.open_account(U, A, allowance=50)
        d = gw.request_spend(req(20))
        st = gw.account_statement(U, A)
        self.assertIn("1 waiting for your verdict", st["summary"])
        gw.report_outcome({"request_id": d.request_id, "verdict": "accepted"})
        st = gw.account_statement(U, A)
        self.assertEqual(st["summary"], "shopper: $30.00 left, 1 purchase(s) in 30 days: all kept")
        self.assertEqual(st["spent"], 20)

    def test_stripe_card_tagged_with_account(self):
        from tests.test_real_money import FakeStripe
        fake = FakeStripe()
        gw, _ = make_gateway(backend=StripeIssuingBackend("ich_1", transport=fake))
        gw.open_account(U, A, allowance=50)
        d = gw.request_spend(req(20))
        self.assertEqual(fake.cards[d.credential.id]["metadata"]["earnest_account"], f"{U}:{A}")


class TestAccountCli(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "e.db")
            base = ["--db", db]
            self.assertEqual(cli_main(base + ["account", "open", "--user", U, "--agent", A, "--allowance", "20"]), 0)
            self.assertEqual(cli_main(base + ["account", "add-card", "--user", U, "--agent", A, "--kind",
                                              "gift_card", "--merchant", "target.com", "--balance", "30",
                                              "--last4", "4417"]), 0)
            self.assertEqual(cli_main(base + ["account", "freeze", "--user", U]), 0)
            self.assertEqual(cli_main(base + ["account", "add-card", "--user", U, "--agent", A, "--kind",
                                              "gift_card", "--merchant", "target.com", "--balance", "30",
                                              "--last4", "44170"]), 2)
            self.assertEqual(cli_main(base + ["account", "statement", "--user", U, "--agent", A]), 0)
            with self.assertRaises(SystemExit):
                cli_main(base + ["account", "topup", "--user", U])   # needs --agent


if __name__ == "__main__":
    unittest.main()
