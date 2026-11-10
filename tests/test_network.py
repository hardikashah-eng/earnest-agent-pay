import json
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest import ed25519
from earnest.cli import main as cli_main
from earnest.ledger import Ledger
from earnest.portable import _unb64, decode
from earnest.reputation import aggregate, contribution, judge
from earnest.simulate import FakeClock

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def gateway(user, points=0):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": user, "novelty": 0.9})
    gw.add_mandate({"id": f"m_{user}", "user_id": user, "intent": "restock", "task_type": "shopping",
                    "budget": {"max": 10000}})
    if points:
        gw.ledger.add_points(user, "agent_x", "shopping", "seed", points, None, clock())
    return gw, clock


def good_history(gw, user, n=12):
    for i in range(n):
        d = gw.request_spend({"agent_id": "agent_x", "mandate_id": f"m_{user}", "amount": 20, "merchant": "amazon.com",
                              "category": "household", "reason": "restock", "item": f"item {i}"})
        gw.authorize_charge(d.credential.id, 20, "amazon.com", [{"item": f"item {i}", "amount": 20}])
        gw.report_outcome({"request_id": d.request_id, "verdict": "accepted"})


class TestEd25519(unittest.TestCase):
    def test_rfc8032_vector_and_roundtrip(self):
        sk = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        self.assertEqual(ed25519.public_key(sk).hex(),
                         "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        sig = ed25519.sign(sk, b"hello")
        self.assertTrue(ed25519.verify(ed25519.public_key(sk), b"hello", sig))
        self.assertFalse(ed25519.verify(ed25519.public_key(sk), b"hellO", sig))
        self.assertFalse(ed25519.verify(ed25519.public_key(ed25519.new_secret()), b"hello", sig))


class TestPortableTrust(unittest.TestCase):
    def setUp(self):
        self.alice, self.clock_a = gateway("alice", points=50)
        good_history(self.alice, "alice")
        self.token = self.alice.export_trust_record("alice", "agent_x", "shopping")

    def test_record_contents_are_coarse(self):
        claims, kid = decode(self.token, NOW)
        self.assertEqual(claims["sub"], "agent_x")
        self.assertEqual(claims["trust"], {"task_type": "shopping", "tier": "trusted", "purchases": "10-49",
                                           "verified_items": "10-49", "kept_rate": 1.0, "violations_90d": 0})
        payload = _unb64(self.token.split(".")[1]).decode()
        for leak in ("amazon", "alice", "item", "20"):
            self.assertNotIn(leak, payload.replace("verified_items", ""))

    def test_import_gives_a_capped_head_start_once(self):
        bob, _ = gateway("bob")
        with self.assertRaises(ValidationError):
            bob.import_trust_record("bob", self.token)        # issuer not trusted yet
        r = bob.import_trust_record("bob", self.token, trust_issuer=True)
        self.assertEqual((r["points_added"], r["tier_now"]), (20.0, "established"))
        again = bob.import_trust_record("bob", self.token)
        self.assertEqual(again["points_added"], 0.0)

    def test_tampered_or_expired_records_fail(self):
        bob, clock = gateway("bob")
        h, c, s = self.token.split(".")
        claims = json.loads(_unb64(c))
        claims["trust"]["tier"] = "trusted"
        claims["trust"]["violations_90d"] = 0
        claims["sub"] = "agent_evil"
        import base64
        forged = f"{h}.{base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b'=').decode()}.{s}"
        with self.assertRaises(ValidationError):
            bob.import_trust_record("bob", forged, trust_issuer=True)
        clock.advance(hours=24 * 31)
        with self.assertRaises(ValidationError):
            bob.import_trust_record("bob", self.token, trust_issuer=True)

    def test_recent_violations_mean_no_head_start(self):
        d = self.alice.request_spend({"agent_id": "agent_x", "mandate_id": "m_alice", "amount": 20,
                                      "merchant": "amaz0n.com", "category": "household", "reason": "x"})
        self.assertEqual(d.status, "denied")
        token = self.alice.export_trust_record("alice", "agent_x", "shopping")
        bob, _ = gateway("bob")
        self.assertEqual(bob.import_trust_record("bob", token, trust_issuer=True)["points_added"], 0.0)

    def test_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, b, rec = str(Path(tmp) / "a.db"), str(Path(tmp) / "b.db"), str(Path(tmp) / "agent_x.jwt")
            gw = Gateway(Ledger(a))
            gw.set_profile({"user_id": "alice"})
            gw.ledger.add_points("alice", "agent_x", "shopping", "seed", 30, None, gw.clock())
            gw.ledger.close()
            self.assertEqual(cli_main(["--db", a, "trust", "export", rec, "--user", "alice", "--agent", "agent_x",
                                       "--task", "shopping"]), 0)
            self.assertEqual(cli_main(["--db", b, "trust", "import", rec, "--user", "bob", "--trust-issuer"]), 0)
            self.assertEqual(Gateway(Ledger(b)).limits("bob", "agent_x", "shopping")["score"], 10)


class TestReputation(unittest.TestCase):
    def test_contribution_is_noisy_and_anonymous(self):
        c = contribution({"id:amazon": {"purchases": 40, "problems": 1}}, epsilon=1.0, rng=random.Random(1))
        self.assertEqual(set(c), {"v", "contribution_id", "epsilon", "merchants"})
        m = c["merchants"]["id:amazon"]
        self.assertLessEqual(m["problems"], m["purchases"])
        self.assertNotEqual((m["purchases"], m["problems"]), (40, 1))

    def test_aggregate_k_anonymity_and_dedup(self):
        cs = [{"contribution_id": f"c{i}", "merchants": {"shady.example": {"purchases": 4, "problems": 2}}}
              for i in range(4)]
        self.assertEqual(aggregate(cs, k=5)["merchants"], {})
        cs.append({"contribution_id": "c5", "merchants": {"shady.example": {"purchases": 4, "problems": 2}}})
        cs.append(cs[0])  # duplicate
        feed = aggregate(cs, k=5)
        self.assertEqual(feed["merchants"]["shady.example"]["contributors"], 5)
        self.assertEqual(feed["merchants"]["shady.example"]["problem_rate"], 0.5)

    def test_judge(self):
        self.assertEqual(judge({"contributors": 12, "purchases": 100, "problems": 40, "problem_rate": 0.4}), "bad")
        self.assertEqual(judge({"contributors": 12, "purchases": 100, "problems": 2, "problem_rate": 0.02}), "good")
        self.assertIsNone(judge({"contributors": 6, "purchases": 100, "problems": 40, "problem_rate": 0.4}))

    def test_feed_changes_decisions(self):
        gw, _ = gateway("carol", points=30)
        gw.import_reputation_feed({"merchants": {
            "shady.example": {"contributors": 20, "purchases": 200, "problems": 90, "problem_rate": 0.45},
            "solid-shop.example": {"contributors": 30, "purchases": 400, "problems": 4, "problem_rate": 0.01},
            "id:amazon": {"contributors": 30, "purchases": 400, "problems": 160, "problem_rate": 0.4}}})
        d = gw.request_spend({"agent_id": "agent_x", "mandate_id": "m_carol", "amount": 5, "merchant": "shady.example",
                              "category": "household", "reason": "x"})
        self.assertEqual(d.status, "escalated")
        self.assertIn("merchant_reputation:bad:0.45", d.reasons)
        good = gw.check_merchant("carol", "solid-shop.example", "x", 5)
        self.assertEqual(good["factors"]["authenticity"]["score"], 0.6)
        amazon = gw.check_merchant("carol", "amazon.com", "x", 5)
        self.assertEqual(amazon["merchant"]["status"], "verified")   # verified stays verified
        self.assertIn("merchant_reputation:bad:0.40", amazon["signals"])
        gw.trust_merchant("carol", "shady.example")
        self.assertEqual(gw.check_merchant("carol", "shady.example", "x", 5)["merchant"]["status"], "user_trusted")

    def test_local_stats(self):
        gw, _ = gateway("dave", points=30)
        good_history(gw, "dave", n=3)
        d = gw.request_spend({"agent_id": "agent_x", "mandate_id": "m_dave", "amount": 9, "merchant": "walmart.com",
                              "category": "household", "reason": "x", "item": "fan"})
        gw.report_outcome({"request_id": d.request_id, "verdict": "rejected"})
        self.assertEqual(gw.merchant_stats("dave"), {"id:amazon": {"purchases": 3, "problems": 0},
                                                     "id:walmart": {"purchases": 1, "problems": 1}})


if __name__ == "__main__":
    unittest.main()
