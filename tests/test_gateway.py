import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from earnest import DEFAULT_TIERS, Gateway, Mandate, Profile, SpendRequest, ValidationError, tier_for
from earnest.scoring import decay, outcome_events
from earnest.models import Outcome
from earnest.simulate import FakeClock, simulate

ROOT = Path(__file__).resolve().parents[1]
USER = "u1"


def make_gateway(**profile):
    clock = FakeClock(datetime(2026, 11, 1, 12, tzinfo=timezone.utc))
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": USER, **profile})
    return gw, clock


def mandate(gw, mid="m1", **kw):
    d = {"id": mid, "user_id": USER, "intent": "buy a tool", "task_type": "software",
         "budget": {"max": kw.pop("max", 100)}, **kw}
    return gw.add_mandate(d)


def req(mid="m1", amount=5, merchant="shop", agent="a1", **kw):
    return {"agent_id": agent, "mandate_id": mid, "amount": amount, "merchant": merchant,
            "category": "software", "reason": "test", **kw}


def build_trust(gw, points, agent="a1"):
    gw.ledger.add_points(USER, agent, "software", "seed", points, None, gw.clock())


class TestTiers(unittest.TestCase):
    def test_tier_boundaries(self):
        self.assertEqual(tier_for(-0.1).name, "restricted")
        self.assertEqual(tier_for(0).name, "new")
        self.assertEqual(tier_for(19.9).name, "new")
        self.assertEqual(tier_for(20).name, "established")
        self.assertEqual(tier_for(60).name, "trusted")

    def test_defaults_match_spec_file(self):
        spec = json.loads((ROOT / "spec" / "trust_tiers.json").read_text())["tiers"]
        for code, s in zip(DEFAULT_TIERS, spec):
            self.assertEqual(code.name, s["name"])
            self.assertEqual(code.min_score, s["min_score"])
            self.assertEqual(code.cap, s["cap"])
            self.assertEqual(code.lifetime_hours, s["lifetime_hours"])
            self.assertEqual(code.approval_over, s["approval_over"])
            self.assertEqual(code.single_use, s["single_use"])
            self.assertEqual(code.approve_new_merchants, s["approve_new_merchants"])


class TestDecisions(unittest.TestCase):
    def test_new_agent_small_known_merchant_is_approved(self):
        gw, _ = make_gateway()
        mandate(gw)
        build_trust(gw, 1)
        # first purchase at a merchant escalates; approve it so the merchant is known
        first = gw.request_spend(req(amount=5))
        self.assertEqual(first.status, "escalated")
        self.assertIn("new_merchant", first.reasons)
        gw.resolve(first.request_id, approve=True)
        d = gw.request_spend(req(amount=5))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.credential.cap, 5)
        self.assertEqual(d.credential.merchant_lock, "shop")
        self.assertTrue(d.credential.single_use)

    def test_over_budget_is_denied_and_penalized(self):
        gw, _ = make_gateway()
        mandate(gw, max=50)
        d = gw.request_spend(req(amount=60))
        self.assertEqual(d.status, "denied")
        self.assertIn("over_budget", d.reasons)
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], -10)

    def test_budget_counts_earlier_spend(self):
        gw, _ = make_gateway(novelty=0.9)
        mandate(gw, max=30)
        build_trust(gw, 30)
        self.assertEqual(gw.request_spend(req(amount=20)).status, "approved")
        d = gw.request_spend(req(amount=20))
        self.assertEqual(d.status, "denied")
        self.assertIn("over_budget", d.reasons)

    def test_category_blocked_merchant_and_deadline(self):
        gw, clock = make_gateway()
        mandate(gw, allowed_categories=["books"], blocked_merchants=["shop"], deadline="2026-10-31")
        d = gw.request_spend(req())
        self.assertEqual(set(d.reasons), {"category_not_allowed", "merchant_blocked", "mandate_expired"})

    def test_hard_rules(self):
        gw, _ = make_gateway(hard_rules=["no_subscriptions"])
        mandate(gw)
        d = gw.request_spend(req(recurring=True))
        self.assertEqual(d.status, "denied")
        self.assertIn("hard_rule:no_subscriptions", d.reasons)

    def test_above_tier_cap_escalates_without_penalty(self):
        gw, _ = make_gateway()
        mandate(gw)
        d = gw.request_spend(req(amount=80))
        self.assertEqual(d.status, "escalated")
        self.assertIn("above_tier_cap", d.reasons)
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], 0)

    def test_trusted_agent_gets_more_room(self):
        gw, _ = make_gateway()
        mandate(gw, max=1000)
        build_trust(gw, 70)
        d = gw.request_spend(req(amount=400, merchant="brand-new"))
        self.assertEqual(d.status, "approved")
        self.assertEqual(d.tier, "trusted")
        self.assertFalse(d.credential.single_use)
        self.assertEqual(d.credential.expires_at - gw.clock(), timedelta(hours=168))

    def test_autonomy_scales_approval_threshold(self):
        cautious, _ = make_gateway(autonomy=0.0)
        self.assertEqual(cautious.limits(USER, "a1", "software")["approval_over"], 5)
        handsoff, _ = make_gateway(autonomy=1.0)
        self.assertEqual(handsoff.limits(USER, "a1", "software")["approval_over"], 15)

    def test_shortlist_mandate_always_escalates(self):
        gw, _ = make_gateway(novelty=0.9)
        mandate(gw, approval="shortlist", mode="gift")
        build_trust(gw, 100)
        d = gw.request_spend(req(amount=5))
        self.assertEqual(d.status, "escalated")
        self.assertIn("mandate_requires_approval:shortlist", d.reasons)

    def test_unknown_mandate(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req(mid="nope"))
        self.assertEqual((d.status, d.reasons), ("denied", ["unknown_mandate"]))


class TestAskFirst(unittest.TestCase):
    def test_needed_ask_is_rewarded(self):
        gw, _ = make_gateway()
        mandate(gw)
        d = gw.request_spend(req(amount=80, ask_first=True))
        self.assertEqual(d.status, "escalated")
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], 1)

    def test_ask_instead_of_violating_is_rewarded_not_penalized(self):
        gw, _ = make_gateway()
        mandate(gw, max=50)
        d = gw.request_spend(req(amount=60, ask_first=True))
        self.assertEqual(d.status, "escalated")
        self.assertIn("over_budget", d.reasons)
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], 1)

    def test_unneeded_ask_earns_nothing(self):
        gw, _ = make_gateway(novelty=0.9)
        mandate(gw)
        build_trust(gw, 30)
        d = gw.request_spend(req(amount=5, ask_first=True))
        self.assertEqual(d.status, "escalated")
        self.assertEqual(d.reasons, ["agent_asked_first"])
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], 30)


class TestResolveAndOutcomes(unittest.TestCase):
    def test_resolve_and_outcome_flow(self):
        gw, _ = make_gateway(price_sensitivity=1.0)
        mandate(gw, benchmark_price=100, max=100)
        d = gw.request_spend(req(amount=80))
        self.assertEqual(len(gw.pending(USER)), 1)
        d = gw.resolve(d.request_id, approve=True)
        self.assertEqual(d.status, "approved")
        self.assertIsNotNone(d.credential)
        events = gw.report_outcome({"request_id": d.request_id, "verdict": "accepted"})
        kinds = {e["kind"]: e["points"] for e in events}
        self.assertEqual(kinds["outcome_accepted"], 3)
        self.assertAlmostEqual(kinds["efficiency"], 0.8)  # 4 * 0.2 * 1.0
        self.assertNotIn("on_time", kinds)  # frugal user: timeliness weight is 0
        self.assertAlmostEqual(gw.limits(USER, "a1", "software")["score"], 2 + 3 + 0.8)

    def test_outcome_rules(self):
        gw, _ = make_gateway()
        mandate(gw)
        d = gw.request_spend(req(amount=80))
        with self.assertRaises(ValidationError):
            gw.report_outcome({"request_id": d.request_id, "verdict": "accepted"})  # not approved
        gw.resolve(d.request_id, approve=True)
        with self.assertRaises(ValidationError):
            gw.resolve(d.request_id, approve=True)  # already resolved
        gw.report_outcome({"request_id": d.request_id, "verdict": "rejected"})
        with self.assertRaises(ValidationError):
            gw.report_outcome({"request_id": d.request_id, "verdict": "accepted"})  # twice

    def test_denied_escalation_is_not_scored(self):
        gw, _ = make_gateway()
        mandate(gw)
        d = gw.request_spend(req(amount=80))
        gw.resolve(d.request_id, approve=False)
        self.assertEqual(gw.limits(USER, "a1", "software")["score"], 0)

    def test_no_efficiency_without_acceptance(self):
        m = Mandate.from_dict({"id": "m", "user_id": USER, "intent": "x", "task_type": "t",
                               "budget": {"max": 100}, "benchmark_price": 100})
        p = Profile(user_id=USER, price_sensitivity=1.0)
        ev = outcome_events(Outcome("r", "rejected"), 10, m, p)
        self.assertEqual(ev, [("outcome_rejected", -5.0)])

    def test_gift_scores_budget_fit_not_savings(self):
        m = Mandate.from_dict({"id": "m", "user_id": USER, "intent": "gift", "task_type": "gift_shopping",
                               "mode": "gift", "budget": {"min": 50, "max": 80}, "benchmark_price": 100})
        p = Profile(user_id=USER, price_sensitivity=1.0)
        kinds = dict(outcome_events(Outcome("r", "accepted"), 60, m, p))
        self.assertEqual(kinds.get("budget_fit"), 1.0)
        self.assertNotIn("efficiency", kinds)

    def test_convenience_user_cares_about_lateness(self):
        m = Mandate.from_dict({"id": "m", "user_id": USER, "intent": "x", "task_type": "t",
                               "budget": {"max": 100}})
        p = Profile(user_id=USER, price_sensitivity=0.0)
        kinds = dict(outcome_events(Outcome("r", "accepted", on_time=False), 10, m, p))
        self.assertEqual(kinds["late"], -2.0)


class TestDecayAndCredentials(unittest.TestCase):
    def test_decay_half_life(self):
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertAlmostEqual(decay(40, t0, t0 + timedelta(days=90)), 20)
        self.assertAlmostEqual(decay(-40, t0, t0 + timedelta(days=90)), -20)

    def test_mock_card_enforces_scope(self):
        gw, clock = make_gateway(novelty=0.9)
        mandate(gw)
        build_trust(gw, 1)
        d = gw.request_spend(req(amount=5))
        cid = d.credential.id
        self.assertEqual(gw.backend.authorize(cid, 6, "shop", clock())[1], "over_cap")
        self.assertEqual(gw.backend.authorize(cid, 5, "other", clock())[1], "wrong_merchant")
        self.assertEqual(gw.backend.authorize(cid, 5, "shop", clock())[1], "approved")
        self.assertEqual(gw.backend.authorize(cid, 1, "shop", clock())[1], "already_used")
        d2 = gw.request_spend(req(amount=5))
        clock.advance(hours=2)
        self.assertEqual(gw.backend.authorize(d2.credential.id, 5, "shop", clock())[1], "expired")


class TestValidation(unittest.TestCase):
    def test_rejects_bad_input(self):
        with self.assertRaises(ValidationError):
            Profile.from_dict({"user_id": "u", "price_sensitivity": 2})
        with self.assertRaises(ValidationError):
            Profile.from_dict({"user_id": "u", "mood": "happy"})
        with self.assertRaises(ValidationError):
            SpendRequest.from_dict(req(amount=0))
        with self.assertRaises(ValidationError):
            Mandate.from_dict({"id": "m", "user_id": "u", "intent": "x", "task_type": "t",
                               "budget": {"min": 90, "max": 10}})

    def test_examples_parse(self):
        for path in sorted((ROOT / "examples").glob("*.json")):
            data = json.loads(path.read_text())
            kind = path.name.split("-")[0]
            parser = {"profile": Profile, "mandate": Mandate, "request": SpendRequest}[kind]
            parser.from_dict(data)
        gift = json.loads((ROOT / "spec" / "examples" / "surprise-gift.json").read_text())
        Profile.from_dict(gift["profile"])
        Mandate.from_dict(gift["mandate"])


class TestSimulation(unittest.TestCase):
    def test_faithful_agent_ends_above_sloppy_agent(self):
        tracks, _ = simulate(days=30)
        by = {t.name: t for t in tracks}
        self.assertGreater(by["steady"].scores[-1], 60)
        self.assertLess(by["sloppy"].scores[-1], 0)
        self.assertEqual(by["steady"].violations, 0)


if __name__ == "__main__":
    unittest.main()
