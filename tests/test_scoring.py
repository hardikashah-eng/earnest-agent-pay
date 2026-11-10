import random
import unittest
from datetime import datetime, timedelta, timezone

from earnest import Gateway
from earnest.confidence import WEIGHTS
from earnest.insights.orders import Order
from earnest.learning import MIN_SAMPLES, Sample, learn
from earnest.simulate import FakeClock

USER = "u1"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def make_gateway(**profile):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": USER, "novelty": 0.9, **profile})
    gw.add_mandate({"id": "m1", "user_id": USER, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 5000}})
    gw.ledger.add_points(USER, "a1", "shopping", "seed", 70, None, clock())
    return gw, clock


def req(merchant="amazon.com", amount=20, item="AA batteries 24 pack", **kw):
    return {"agent_id": "a1", "mandate_id": "m1", "amount": amount, "merchant": merchant,
            "category": "household", "reason": "restock", "item": item, **kw}


class TestLearning(unittest.TestCase):
    def synthetic(self, n, seed=7):
        rng = random.Random(seed)
        out = []
        for _ in range(n):
            f = tuple(round(rng.random(), 2) for _ in range(4))
            # this user keeps things they're familiar with; price barely matters
            p = 0.1 + 0.85 * f[2]
            out.append(Sample(f, rng.random() < p))
        return out

    def test_not_enough_data(self):
        r = learn(self.synthetic(MIN_SAMPLES - 1))
        self.assertEqual((r["status"], r["weights"]), ("not_enough_data", WEIGHTS))
        r = learn([Sample((1, 1, 1, 1), True)] * 20)
        self.assertEqual(r["status"], "not_enough_data")  # never seen a rejection

    def test_learns_what_matters_to_this_user(self):
        r = learn(self.synthetic(300))
        self.assertEqual(r["status"], "learned")
        self.assertGreater(r["weights"]["familiarity"], WEIGHTS["familiarity"] + 0.1)
        self.assertLessEqual(r["heldout_brier"]["learned"], r["heldout_brier"]["default"])
        self.assertAlmostEqual(sum(r["weights"].values()), 1.0, places=2)
        high = next(b for b in r["calibration"] if b["band"] == "high")
        low = next(b for b in r["calibration"] if b["band"] == "low")
        self.assertGreater(high["kept_rate"], low["kept_rate"])

    def test_small_samples_stay_near_defaults(self):
        r = learn(self.synthetic(12))
        cand = r["candidate_weights"]
        self.assertLess(max(abs(cand[k] - WEIGHTS[k]) for k in WEIGHTS), 0.15)

    def test_gateway_fit_uses_verdicts(self):
        gw, clock = make_gateway()
        rng = random.Random(3)
        for i in range(40):
            known = i % 2 == 0
            merchant = "amazon.com" if known else f"shop{i}.example"
            d = gw.request_spend(req(merchant=merchant, amount=10 + i, item=f"thing {i}"))
            if d.status != "approved":
                d = gw.resolve(d.request_id, approve=True)
            verdict = "accepted" if (known or rng.random() < 0.2) else "rejected"
            gw.report_outcome({"request_id": d.request_id, "verdict": verdict})
            clock.advance(hours=1)
        report = gw.fit_confidence(USER)
        self.assertEqual(report["samples"], 40)
        self.assertEqual(report["status"], "learned")
        self.assertGreater(report["weights"]["authenticity"], WEIGHTS["authenticity"])
        self.assertEqual(gw.check_merchant(USER, "amazon.com", "x", 5)["weights_source"], "learned")
        gw.ledger.set_confidence_weights(USER, None, clock())
        self.assertEqual(gw.check_merchant(USER, "amazon.com", "x", 5)["weights_source"], "default")


class TestMarket(unittest.TestCase):
    def test_cheaper_elsewhere_caps_price_factor(self):
        gw, _ = make_gateway()
        gw.report_price("walmart.com", "Amazon Basics AA batteries 24 pack", 12.00)
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 18.00)
        self.assertIn("cheaper_elsewhere:walmart.com:12.00", c["signals"])
        self.assertLessEqual(c["factors"]["price"]["score"], 0.6)
        self.assertEqual(c["market"]["best_merchant"], "walmart.com")

    def test_frugal_users_get_asked(self):
        frugal, _ = make_gateway(price_sensitivity=0.9)
        frugal.report_price("target.com", "AA batteries 24 pack", 12.00)
        d = frugal.request_spend(req(amount=18))
        self.assertEqual(d.status, "escalated")
        self.assertIn("cheaper_elsewhere:target.com:12.00", d.reasons)
        relaxed, _ = make_gateway(price_sensitivity=0.3)
        relaxed.report_price("target.com", "AA batteries 24 pack", 12.00)
        self.assertEqual(relaxed.request_spend(req(amount=18)).status, "approved")

    def test_agent_quotes_never_make_a_price_look_good(self):
        gw, _ = make_gateway()
        gw.report_price("walmart.com", "AA batteries 24 pack", 40.00, source="agent:a1")
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 39.00)
        self.assertNotIn("best_market_price", c["signals"])
        gw.report_price("walmart.com", "AA batteries 24 pack", 40.00, source="user")
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 39.00)
        self.assertIn("best_market_price", c["signals"])

    def test_stale_and_unverified_quotes_ignored(self):
        gw, clock = make_gateway()
        gw.report_price("walmart.com", "AA batteries 24 pack", 5.00)
        gw.report_price("cheap-batteries.example", "AA batteries 24 pack", 4.00)
        clock.advance(hours=24 * 8)
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 18.00)
        self.assertNotIn("market", c)

    def test_market_is_a_price_reference_without_history(self):
        gw, _ = make_gateway()
        for m, p in [("walmart.com", 20), ("target.com", 22), ("bestbuy.com", 21)]:
            gw.report_price(m, "Logitech MX Master 3S mouse", p)
        self.assertEqual(gw.check_merchant(USER, "amazon.com", "Logitech MX Master 3S mouse", 21)
                         ["factors"]["price"]["score"], 0.85)

    def test_consider_waiting(self):
        gw, _ = make_gateway()
        gw.import_orders(USER, [Order("o1", (NOW - timedelta(days=30)).date(), "amazon.com",
                                      "AA batteries 24 pack", 14.0)])
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 18.00)
        self.assertIn("consider_waiting:14.00", c["signals"])
        gw.add_mandate({"id": "m2", "user_id": USER, "intent": "restock", "task_type": "shopping",
                        "budget": {"max": 50}, "deadline": (NOW + timedelta(days=2)).date().isoformat()})
        c = gw.check_merchant(USER, "amazon.com", "AA batteries 24 pack", 18.00, mandate_id="m2")
        self.assertFalse(any(s.startswith("consider_waiting") for s in c["signals"]))


if __name__ == "__main__":
    unittest.main()
