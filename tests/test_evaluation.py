import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.cli import main as cli_main
from earnest.evaluation import (DEFAULT_POLICY, GOALS, EvalInputs, evaluate, gate_reasons, grade, net_value,
                                normalize_weights, price_score, time_score, utility_score)
from earnest.simulate import FakeClock

U, A = "u1", "shopper"
NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
GOOD = {"lifespan_months": 60, "uses_per_month": 12, "hours_saved_per_year": 40, "solves": 9, "core": True,
        "benefits": {"stress": 2, "health": 1}, "resale_value": 80}


def policy(**kw):
    return {**DEFAULT_POLICY, **kw}


def make_gateway(points=60, **eval_policy):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": U})
    gw.add_mandate({"id": "m1", "user_id": U, "intent": "kitchen and home", "task_type": "shopping",
                    "budget": {"max": 1000}})
    gw.ledger.add_points(U, A, "shopping", "seed", points, None, clock())
    if eval_policy:
        gw.set_eval_policy(U, **eval_policy)
    return gw, clock


def req(amount=449, item="countertop dishwasher", evaluation=GOOD, **kw):
    d = {"agent_id": A, "mandate_id": "m1", "amount": amount, "merchant": "amazon.com", "category": "home",
         "reason": "save time", "item": item, **kw}
    if evaluation is not None:
        d["evaluation"] = evaluation
    return d


class TestRubric(unittest.TestCase):
    def test_worked_example(self):
        # the rubric's own example: 8, 9, 7, 6 at 30/25/30/15 -> 7.65
        r = evaluate(100, EvalInputs.from_dict({"scores": {"price": 8, "time_saved": 9, "utility": 7,
                                                           "added_value": 6}}), policy())
        self.assertEqual(r["total"], 7.65)
        self.assertEqual([r["metrics"][m]["weighted"] for m in ("price", "time_saved", "utility", "added_value")],
                         [2.4, 2.25, 2.1, 0.9])
        self.assertTrue(r["passes"])
        self.assertFalse(evaluate(100, EvalInputs.from_dict({"scores": {"price": 8, "time_saved": 9, "utility": 7,
                                                                        "added_value": 6}}),
                                  policy(threshold=8))["passes"])

    def test_goals_shift_weight(self):
        self.assertEqual(GOALS["save_money"]["price"], 40)
        self.assertEqual(GOALS["save_time"]["time_saved"], 40)
        for g in GOALS.values():
            self.assertEqual(sum(g.values()), 100)
        cheap_slow = EvalInputs.from_dict({"scores": {"price": 10, "time_saved": 2, "utility": 7, "added_value": 6}})
        self.assertGreater(evaluate(10, cheap_slow, policy(weights=GOALS["save_money"]))["total"],
                           evaluate(10, cheap_slow, policy(weights=GOALS["save_time"]))["total"])

    def test_price_cost_per_use(self):
        daily = EvalInputs(lifespan_months=24, uses_per_month=30)          # 720 uses
        rare = EvalInputs(lifespan_months=12, uses_per_month=0.25)         # 3 uses
        self.assertGreater(price_score(60, daily, 30, None)[0], price_score(60, rare, 30, None)[0])
        self.assertIn("per use", price_score(60, daily, 30, None)[1])
        above_market = price_score(60, daily, 30, 40)[0]
        self.assertLess(above_market, price_score(60, daily, 30, None)[0])
        self.assertEqual(price_score(60, EvalInputs(total_uses=0), 30, None)[0], 1.0)
        self.assertEqual(price_score(60, EvalInputs(), 30, 60)[0], 5.5)    # only the market to go on
        self.assertIsNone(price_score(60, EvalInputs(), 30, None)[0])

    def test_time_saved_pays_for_itself(self):
        self.assertEqual(time_score(100, EvalInputs(hours_saved_per_year=2), 50)[0], 7.0)   # $100 of time
        self.assertEqual(time_score(100, EvalInputs(hours_saved_per_year=4), 50)[0], 10.0)
        self.assertEqual(time_score(100, EvalInputs(hours_saved_per_year=0), 50)[0], 1.0)

    def test_utility(self):
        self.assertGreater(utility_score(EvalInputs(uses_per_month=30, solves=9, core=True))[0],
                           utility_score(EvalInputs(uses_per_month=0.5, solves=4, core=False))[0])
        self.assertIsNone(utility_score(EvalInputs(core=True))[0])

    def test_net_value(self):
        i = EvalInputs(hours_saved_per_year=10, added_value_usd=50, resale_value=30)
        self.assertEqual(net_value(200, i, 25), 10 * 25 + 50 + 30 - 200)
        self.assertIsNone(net_value(200, EvalInputs(solves=5), 25))

    def test_coverage_and_gate(self):
        save_time = policy(weights=GOALS["save_time"])   # price 20 + utility 25: 45% backed by inputs
        r = evaluate(180, EvalInputs.from_dict({"uses_per_month": 0.5, "lifespan_months": 36, "solves": 4}), save_time)
        self.assertEqual(r["coverage"], 0.45)
        self.assertFalse(r["passes"])
        reasons = gate_reasons(r, save_time)
        self.assertTrue(reasons[0].startswith("eval:incomplete"))
        self.assertTrue(any(x.startswith("eval:below_threshold") for x in reasons))
        big = evaluate(500, EvalInputs.from_dict({"scores": {"price": 9, "time_saved": 9, "utility": 9,
                                                             "added_value": 9}, "hours_saved_per_year": 1}),
                       policy())
        self.assertEqual(gate_reasons(big, policy()), ["eval:negative_net_value:-470.00"])

    def test_validation(self):
        for bad in ({"solves": 11}, {"benefits": {"wealth": 2}}, {"benefits": {"stress": 5}},
                    {"scores": {"vibes": 9}}, {"core": "yes"}, {"uses_per_month": -1}, {"mood": 1}, "x"):
            with self.assertRaises(ValidationError, msg=bad):
                EvalInputs.from_dict(bad)
        with self.assertRaises(ValidationError):
            normalize_weights({"price": 50, "time_saved": 50, "utility": 50, "added_value": 0})
        with self.assertRaises(ValidationError):
            normalize_weights({"price": 100})

    def test_grade(self):
        self.assertEqual(grade(9.0, 0.2), "A")
        self.assertEqual(grade(9.0, 2.0), "B")   # overrates: one letter down
        self.assertEqual(grade(4.0, None), "F")
        self.assertIsNone(grade(None, None))


class TestGateway(unittest.TestCase):
    def test_scores_without_gate(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req())
        self.assertEqual(d.status, "approved")
        self.assertGreater(d.evaluation["total"], 9)
        self.assertTrue(d.evaluation["passes"])
        weak = gw.request_spend(req(180, "electric wine opener", {"uses_per_month": 0.5, "lifespan_months": 36,
                                                                   "solves": 4, "core": False}))
        self.assertEqual(weak.status, "approved")   # gate off: scored, not stopped
        self.assertFalse(weak.evaluation["passes"])
        self.assertIsNone(gw.request_spend(req(20, "towels", None)).evaluation)

    def test_gate(self):
        gw, _ = make_gateway(gate=True)
        self.assertEqual(gw.request_spend(req()).status, "approved")
        d = gw.request_spend(req(180, "electric wine opener", {"uses_per_month": 0.5, "lifespan_months": 36,
                                                                "solves": 4, "core": False}))
        self.assertEqual(d.status, "escalated")
        self.assertTrue(any(r.startswith("eval:below_threshold") for r in d.reasons))
        d = gw.request_spend(req(20, "towels", None))
        self.assertEqual((d.status, d.reasons), ("escalated", ["eval:missing"]))
        self.assertTrue(d.evaluation["missing"])
        ok = gw.resolve(d.request_id, approve=True)   # you can still say yes
        self.assertEqual(ok.status, "approved")
        self.assertEqual(gw.ledger.score(U, A, "shopping", NOW), 64.0)   # +2 per approved purchase; no penalty

    def test_gate_skips_gifts_and_standing(self):
        gw, _ = make_gateway(gate=True)
        gw.add_mandate({"id": "g1", "user_id": U, "intent": "gift for Sam", "task_type": "shopping", "mode": "gift",
                        "budget": {"max": 100}})
        d = gw.request_spend({"agent_id": A, "mandate_id": "g1", "amount": 40, "merchant": "amazon.com",
                              "category": "gifts", "reason": "gift", "item": "book"})
        self.assertNotIn("eval:missing", d.reasons)

    def test_market_price_feeds_price_score(self):
        gw, _ = make_gateway()
        gw.add_mandate({"id": "m2", "user_id": U, "intent": "kitchen", "task_type": "shopping",
                        "budget": {"max": 1000}, "benchmark_price": 300})
        d = gw.request_spend({**req(), "mandate_id": "m2"})
        est = gw.purchase_eval(d.request_id)["estimate"]
        self.assertIn("above the $300.00 market price", est["metrics"]["price"]["why"])

    def test_review_and_trust(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req())
        with self.assertRaises(ValidationError):
            gw.review_purchase(d.request_id, {})
        close = gw.review_purchase(d.request_id, {**GOOD, "hours_saved_per_year": 35})
        self.assertEqual(close["comparison"]["verdict"], "about right")
        self.assertEqual(close["trust_events"], [{"kind": "eval_calibrated", "points": 1.0}])
        again = gw.review_purchase(d.request_id, {**GOOD, "hours_saved_per_year": 30})
        self.assertEqual(again["trust_events"], [])   # only the first review counts for trust
        d2 = gw.request_spend(req(300, "smart blender", {**GOOD}))
        bad = gw.review_purchase(d2.request_id, {"uses_per_month": 0.2, "lifespan_months": 12, "hours_saved_per_year": 0,
                                                 "solves": 3, "core": False})
        self.assertEqual(bad["comparison"]["verdict"], "overrated")
        self.assertEqual(bad["trust_events"], [{"kind": "eval_overclaim", "points": -2.0}])
        denied = gw.request_spend(req(5000))
        with self.assertRaises(ValidationError):
            gw.review_purchase(denied.request_id, GOOD)

    def test_agent_report(self):
        gw, _ = make_gateway(threshold=7.5)
        self.assertIn("hasn't bought anything", gw.agent_eval(U, A)["summary"])
        a = gw.request_spend(req()).request_id
        b = gw.request_spend(req(180, "electric wine opener", {"uses_per_month": 0.5, "lifespan_months": 36,
                                                                "solves": 4, "core": False,
                                                                "hours_saved_per_year": 0})).request_id
        gw.request_spend(req(15, "towels", None))
        gw.review_purchase(a, {**GOOD, "hours_saved_per_year": 20, "benefits": {"stress": 1}, "resale_value": 0})
        rep = gw.agent_eval(U, A)
        self.assertEqual((rep["purchases"], rep["evaluated"], rep["reviewed"]), (3, 2, 1))
        self.assertEqual(rep["met_threshold"], {"count": 1, "of": 2, "threshold": 7.5})
        self.assertEqual(len(rep["unevaluated"]), 1)
        self.assertGreater(rep["estimate_bias"], 0)
        self.assertIn("added_value", rep["bias_by_metric"])
        self.assertIn("1 of 2 purchases met your 7.5 bar", rep["summary"])
        self.assertIn(rep["grade"], "ABCDF")
        self.assertEqual(gw.purchase_eval(b)["review"], None)

    def test_policy(self):
        gw, _ = make_gateway()
        p = gw.set_eval_policy(U, goal="save_money", hourly_value=40)
        self.assertEqual((p["weights"]["price"], p["hourly_value"]), (40, 40))
        p = gw.set_eval_policy(U, weights={"price": 25, "time_saved": 25, "utility": 25, "added_value": 25})
        self.assertEqual(p["goal"], "custom")
        for bad in (dict(goal="yolo"), dict(threshold=11), dict(hourly_value=0)):
            with self.assertRaises(ValidationError):
                gw.set_eval_policy(U, **bad)

    def test_score_purchase_dry_run(self):
        gw, _ = make_gateway(gate=True)
        r = gw.score_purchase(U, 449, GOOD)
        self.assertTrue(r["passes"])
        self.assertEqual(r["would_come_to_you"], [])
        self.assertEqual(gw.ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)


class TestEvalCli(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--db", str(Path(tmp) / "e.db")]
            self.assertEqual(cli_main(base + ["eval", "policy", "--user", U, "--goal", "save_time", "--hourly-value",
                                              "50", "--gate", "on"]), 0)
            self.assertEqual(cli_main(base + ["eval", "policy", "--user", U, "--weights",
                                              "price=25,time_saved=25,utility=25,added_value=25"]), 0)
            self.assertEqual(cli_main(base + ["eval", "policy", "--user", U, "--weights", "price=90"]), 2)
            self.assertEqual(cli_main(base + ["eval", "score", "--user", U, "--price", "449", "--lifespan-months",
                                              "60", "--uses-per-month", "12", "--hours-saved", "40", "--solves", "9",
                                              "--core", "yes", "--benefit", "stress=2", "--resale", "80"]), 0)
            self.assertEqual(cli_main(base + ["eval", "agent", "--user", U, "--agent", A]), 0)
            self.assertEqual(cli_main(base + ["eval", "review", "req_nope", "--solves", "5"]), 2)


if __name__ == "__main__":
    unittest.main()
