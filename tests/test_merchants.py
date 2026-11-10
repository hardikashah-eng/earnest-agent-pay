import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from earnest import Gateway, MerchantRegistry, SpendRequest
from earnest.cli import main as cli_main
from earnest.history import PastPurchase, item_similarity, summarize
from earnest.insights import TransactionStore
from earnest.insights.statement import Transaction
from earnest.merchants import DATA_FILE, host_of, merchant_keys
from earnest.simulate import FakeClock

USER = "u1"
REG = MerchantRegistry.default()


def make_gateway(**profile):
    gw = Gateway(clock=FakeClock(datetime(2026, 11, 1, 12, tzinfo=timezone.utc)))
    gw.set_profile({"user_id": USER, **profile})
    gw.add_mandate({"id": "m1", "user_id": USER, "intent": "household stuff", "task_type": "shopping",
                    "budget": {"max": 500}})
    return gw


def req(merchant="shop.example", amount=5, **kw):
    return {"agent_id": "a1", "mandate_id": "m1", "amount": amount, "merchant": merchant,
            "category": kw.pop("category", "shopping"), "reason": "test", **kw}


def score(gw):
    return gw.limits(USER, "a1", "shopping")["score"]


def seed(gw, points):
    gw.ledger.add_points(USER, "a1", "shopping", "seed", points, None, gw.clock())


class TestNormalization(unittest.TestCase):
    def test_host_of(self):
        self.assertEqual(host_of("https://www.Amazon.com/dp/B0?x=1"), "amazon.com")
        self.assertEqual(host_of("smile.amazon.com"), "smile.amazon.com")
        self.assertIsNone(host_of("Trader Joe's"))
        self.assertIsNone(host_of("shop"))

    def test_keys_match_across_shapes(self):
        self.assertTrue(merchant_keys("amazon.com") & merchant_keys("Amazon"))
        self.assertTrue(REG.canonical("AMZN Mktp") & REG.canonical("https://amazon.com/x"))


class TestRegistry(unittest.TestCase):
    def test_default_data_is_consistent(self):
        data = json.loads(DATA_FILE.read_text())
        ids = [m["id"] for m in data["verified"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertGreaterEqual(len(ids), 30)
        for m in data["verified"]:
            for d in m["domains"]:
                self.assertEqual(host_of(d), d, d)
                # no verified domain may look like a fake of another one
                self.assertEqual(REG.check(d).status, "verified", d)

    def test_verified_merchants(self):
        for m in ["amazon.com", "https://www.bestbuy.com/site/x", "smile.amazon.com", "Amazon",
                  "AMZN Mktp", "Lowe's", "Home Depot"]:
            self.assertEqual(REG.check(m).status, "verified", m)
        self.assertEqual(REG.check("amazon.co.uk").merchant_id, "amazon")

    def test_lookalikes_are_untrusted(self):
        cases = {"amaz0n.com": "amazon", "arnazon.com": "amazon", "amazom.com": "amazon",
                 "amazon-giftcards.shop": "amazon", "amaz0n-deals.com": "amazon",
                 "amazon.evil.com": "amazon", "targett.com": "target", "rnicrosoft.com": "microsoft",
                 "walmart-refund.top": "walmart"}
        for m, brand in cases.items():
            c = REG.check(m)
            self.assertEqual((c.status, c.reasons), ("untrusted", [f"merchant_lookalike:{brand}"]), m)

    def test_brand_on_unlisted_domain_is_suspicious_not_denied(self):
        c = REG.check("ikea.fr")
        self.assertEqual(c.status, "suspicious")
        self.assertIn("merchant_unlisted_domain:ikea", c.reasons)

    def test_suspicious_patterns(self):
        c = REG.check("best-prices.xyz")
        self.assertEqual(c.status, "suspicious")
        self.assertIn("merchant_suspicious_tld:xyz", c.reasons)
        self.assertIn("merchant_suspicious_name:login", REG.check("shop-login.com").reasons)

    def test_short_real_brands_are_not_flagged(self):
        # loves.com is one edit from lowes.com; short names only match on homoglyphs
        for m in ["loves.com", "corner-store.com", "Burger Barn", "diagramtool.com"]:
            self.assertEqual(REG.check(m).status, "unknown", m)

    def test_named_blocklist_entities(self):
        reg = MerchantRegistry({"verified": [], "untrusted": {"entities": [
            {"merchant": "bad-shop.example", "reason": "chargebacks"}]}})
        self.assertEqual(reg.check("https://bad-shop.example/cart").reasons, ["merchant_untrusted:chargebacks"])

    def test_user_lists_win(self):
        c = REG.check("amazon.com", {"amazon.com": ("untrusted", "user")})
        self.assertEqual(c.status, "untrusted")
        c = REG.check("ikea.fr", {"ikea.fr": ("trusted", "user")})
        self.assertEqual(c.status, "user_trusted")

    def test_high_risk_categories(self):
        self.assertTrue(REG.high_risk_category("Gift Cards"))
        self.assertTrue(REG.high_risk_category("crypto"))
        self.assertFalse(REG.high_risk_category("software"))


class TestHistory(unittest.TestCase):
    TODAY = date(2026, 11, 1)

    def past(self, item, verdict="accepted", amount=20.0, merchant="amazon.com", on=date(2026, 10, 1)):
        return PastPurchase(frozenset(REG.canonical(merchant)), amount, on, "earnest", item, verdict)

    def test_item_similarity(self):
        self.assertEqual(item_similarity("AA Batteries 24 pack", "aa battery 24"), 1.0)
        self.assertGreaterEqual(item_similarity("Anker USB-C cable 6ft", "Anker USB-C cable 3ft"), 0.4)
        self.assertLess(item_similarity("USB-C cable", "garden hose"), 0.4)
        # brand words in retail titles don't hide the same item; AA is not AAA
        self.assertGreaterEqual(item_similarity("AA batteries 24 pack", "Amazon Basics AA Batteries, 24 Pack"), 0.8)
        self.assertLess(item_similarity("AAA batteries 24 pack", "Amazon Basics AA Batteries, 24 Pack"), 0.4)

    def test_same_similar_and_other_merchant(self):
        keys = REG.canonical("Amazon")
        past = [self.past("AA batteries 24 pack"), self.past("AAA batteries 24 pack"),
                self.past("AA batteries 24 pack", merchant="target.com")]
        h = summarize(keys, past, "aa batteries 24 pack", self.TODAY)
        self.assertEqual((h.merchant_purchases, h.same_item, h.similar_item, h.same_item_elsewhere),
                         (2, 1, 1, 1))
        self.assertEqual(h.same_item_prices, [20.0, 20.0])

    def test_rejected_item(self):
        keys = REG.canonical("amazon.com")
        h = summarize(keys, [self.past("desk lamp", verdict="rejected")], "desk lamp", self.TODAY)
        self.assertEqual(h.rejected_before, 1)
        self.assertIn("previous_purchase_rejected", h.signals)

    def test_old_prices_are_not_a_reference(self):
        keys = REG.canonical("amazon.com")
        h = summarize(keys, [self.past("desk lamp", on=date(2025, 6, 1))], "desk lamp", self.TODAY)
        self.assertEqual(h.same_item, 1)          # still counts as bought before
        self.assertEqual(h.same_item_prices, [])  # but its price is too old to compare


class TestGatewayDecisions(unittest.TestCase):
    def test_lookalike_is_denied_and_penalized(self):
        gw = make_gateway()
        d = gw.request_spend(req("amaz0n.com"))
        self.assertEqual(d.status, "denied")
        self.assertIn("merchant_lookalike:amazon", d.reasons)
        self.assertEqual(score(gw), -10)
        self.assertEqual(d.confidence["level"], "low")

    def test_asking_first_about_a_lookalike_is_rewarded(self):
        gw = make_gateway()
        d = gw.request_spend(req("amaz0n.com", ask_first=True))
        self.assertEqual(d.status, "escalated")
        self.assertEqual(score(gw), 1)

    def test_suspicious_and_high_risk_escalate_without_penalty(self):
        gw = make_gateway(novelty=0.9)
        seed(gw, 30)
        d = gw.request_spend(req("best-prices.xyz"))
        self.assertEqual(d.status, "escalated")
        self.assertIn("merchant_suspicious_tld:xyz", d.reasons)
        d = gw.request_spend(req("amazon.com", category="gift_cards"))
        self.assertEqual(d.status, "escalated")
        self.assertIn("high_risk_category:gift_cards", d.reasons)
        self.assertEqual(score(gw), 30)

    def test_verified_merchant_skips_new_merchant_check(self):
        gw = make_gateway()
        seed(gw, 1)
        self.assertIn("new_merchant", gw.request_spend(req("unknown-shop.com")).reasons)
        d = gw.request_spend(req("amazon.com"))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.confidence["merchant"]["status"], "verified")

    def test_no_new_merchants_rule_still_applies_to_verified(self):
        gw = make_gateway(hard_rules=["no_new_merchants"])
        d = gw.request_spend(req("amazon.com"))
        self.assertIn("hard_rule:no_new_merchants", d.reasons)

    def test_statement_history_makes_merchant_known(self):
        gw = make_gateway(hard_rules=["no_new_merchants"])
        seed(gw, 1)
        store = TransactionStore(gw.ledger.db)
        store.add(USER, [Transaction("t1", date(2026, 10, 3), "BURGER BARN", "Burger Barn", "food", 12.5),
                         Transaction("t2", date(2026, 10, 9), "AMZN Mktp US*1", "Amazon", "shopping", 30)],
                  "test")
        d = gw.request_spend(req("Burger Barn"))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.confidence["history"]["from_statements"], 1)
        # statement merchant "Amazon" matches a request for amazon.com
        d = gw.request_spend(req("https://www.amazon.com/dp/B0"))
        self.assertEqual(d.status, "approved")
        self.assertIn("bought_here_before:1", d.confidence["signals"])

    def test_repeat_item_raises_confidence(self):
        gw = make_gateway(novelty=0.9)
        seed(gw, 30)
        first = gw.request_spend(req("amazon.com", amount=18, item="AA batteries 24 pack"))
        gw.report_outcome({"request_id": first.request_id, "verdict": "accepted"})
        d = gw.request_spend(req("amazon.com", amount=19, item="aa battery 24 pack"))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.confidence["level"], "high")
        self.assertIn("same_item_bought_here", d.confidence["signals"])

    def test_previously_rejected_item_escalates(self):
        gw = make_gateway(novelty=0.9)
        seed(gw, 30)
        first = gw.request_spend(req("walmart.com", amount=40, item="folding desk lamp"))
        gw.report_outcome({"request_id": first.request_id, "verdict": "rejected"})
        d = gw.request_spend(req("walmart.com", amount=40, item="folding desk lamp"))
        self.assertEqual(d.status, "escalated")
        self.assertIn("previous_purchase_rejected", d.reasons)

    def test_user_distrust_and_trust(self):
        gw = make_gateway(novelty=0.9)
        seed(gw, 30)
        gw.distrust_merchant(USER, "Target")
        d = gw.request_spend(req("target.com"))
        self.assertEqual(d.status, "denied")
        self.assertIn("merchant_untrusted:user_list:user", d.reasons)
        gw.trust_merchant(USER, "ikea.fr")
        self.assertEqual(gw.request_spend(req("ikea.fr")).status, "approved")

    def test_pending_shows_item_and_confidence(self):
        gw = make_gateway()
        gw.request_spend(req("amazon.com", amount=80, item="desk lamp"))
        p = gw.pending(USER)[0]
        self.assertEqual(p["item"], "desk lamp")
        self.assertEqual(p["confidence"]["merchant"]["merchant_id"], "amazon")

    def test_item_is_optional_and_round_trips(self):
        self.assertNotIn("item", SpendRequest.from_dict(req()).to_dict())
        self.assertEqual(SpendRequest.from_dict(req(item="lamp")).to_dict()["item"], "lamp")


class TestCli(unittest.TestCase):
    def test_import_blocklist_and_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            feed = Path(tmp) / "feed.txt"
            feed.write_text("# from my bank\nbad-shop.example\nhttps://worse.example/cart  # phishing\n\n")
            self.assertEqual(cli_main(["--db", db, "merchant", "import-blocklist", str(feed), "--user", USER]), 0)
            gw = Gateway()
            from earnest.ledger import Ledger
            gw.ledger = Ledger(db)
            self.assertEqual(gw.check_merchant(USER, "worse.example")["merchant"]["status"], "untrusted")
            gw.ledger.close()


if __name__ == "__main__":
    unittest.main()
