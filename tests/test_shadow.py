import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.cli import main as cli_main
from earnest.insights import TransactionStore, import_file
from earnest.insights.statement import Transaction
from earnest.shadow import (MATCH_POINTS_CAP, CardTx, agreement, amounts_match, card_profile, match,
                            readiness)
from earnest.simulate import FakeClock

U, A, CARD = "u1", "shopper", "Visa 4417"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "statements"


def tx(i, on, merchant="Target", amount=20.0, category="shopping"):
    return Transaction(f"t{i}", on, merchant.upper(), merchant, category, amount)


def make_gateway(history=8):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock)
    gw.set_profile({"user_id": U})
    gw.add_mandate({"id": "m1", "user_id": U, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 500}})
    store = TransactionStore(gw.ledger.db)
    store.add(U, [tx(i, date(2026, 9, 1) + timedelta(days=7 * i), amount=20 + i) for i in range(history)],
              "manual", CARD)
    return gw, clock, store


def req(amount=22, merchant="target.com", item="paper towels", **kw):
    return {"agent_id": A, "mandate_id": "m1", "amount": amount, "merchant": merchant,
            "category": kw.pop("category", "household"), "reason": "restock", "item": item, **kw}


class TestPure(unittest.TestCase):
    def test_card_profile(self):
        txs = [CardTx(f"t{i}", date(2026, 9, 1) + timedelta(days=7 * i), "Target", "shopping", 20 + i)
               for i in range(6)]
        txs.append(CardTx("big", date(2026, 10, 20), "Best Buy", "shopping", 400))
        p = card_profile(txs)
        self.assertEqual(p["transactions"], 7)
        self.assertEqual(p["merchants"][0]["merchant"], "Target")
        self.assertEqual(p["merchants"][0]["every_days"], 7)
        self.assertEqual([r["merchant"] for r in p["regulars"]], ["Target"])
        self.assertEqual(p["suggested"]["known_merchants"], ["Target"])
        self.assertEqual(p["purchase_size"]["largest"], 400)
        self.assertEqual(card_profile([]), {"transactions": 0})

    def test_match(self):
        keys = lambda m: {m.lower()}
        txs = [CardTx("a", date(2026, 11, 1), "target", "s", 30), CardTx("b", date(2026, 11, 3), "target", "s", 23),
               CardTx("c", date(2026, 11, 30), "target", "s", 22)]
        self.assertEqual(match(date(2026, 11, 2), {"target"}, 22, txs, keys, set()).id, "b")
        self.assertIsNone(match(date(2026, 11, 2), {"target"}, 22, txs, keys, {"b"}))   # already claimed
        self.assertIsNone(match(date(2026, 11, 2), {"amazon"}, 22, txs, keys, set()))
        self.assertTrue(amounts_match(100, 112))
        self.assertFalse(amounts_match(100, 120))
        self.assertTrue(amounts_match(5, 6.5))   # $2 floor

    def test_agreement_and_readiness(self):
        self.assertEqual(agreement("approved", True), "agree")
        self.assertEqual(agreement("denied", True), "disagree")
        self.assertEqual(agreement("escalated", False), "asked")
        self.assertTrue(readiness(10, 9, 1, 0, 0)["ready"])
        self.assertFalse(readiness(10, 8, 2, 0, 0)["ready"])
        self.assertTrue(readiness(0, 0, 0, 5, 0)["ready"])
        self.assertFalse(readiness(20, 20, 0, 9, 1)["ready"])   # a would-be violation blocks it


class TestCardLabels(unittest.TestCase):
    def test_import_with_card_labels_existing_rows(self):
        gw = Gateway()
        store = TransactionStore(gw.ledger.db)
        f = EXAMPLES / "statement-jul-sep.csv"
        import_file(store, f, U)
        self.assertEqual(gw.ledger.cards(U), [])
        import_file(store, f, U, card=CARD)   # same file, now labelled
        cards = gw.ledger.cards(U)
        self.assertEqual(cards[0]["card"], CARD)
        self.assertEqual(cards[0]["transactions"], len(gw.ledger.card_transactions(U, None)))

    def test_old_database_gets_card_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sqlite3
            path = str(Path(tmp) / "old.db")
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE transactions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, date TEXT NOT "
                             "NULL, description TEXT NOT NULL, merchant TEXT NOT NULL, category TEXT NOT NULL, "
                             "amount REAL NOT NULL, source TEXT NOT NULL);")
            db.close()
            from earnest.ledger import Ledger
            led = Ledger(path)
            TransactionStore(led.db)
            self.assertIn("card", {r["name"] for r in led.db.execute("PRAGMA table_info(transactions)")})


class TestShadowMode(unittest.TestCase):
    def test_start_needs_card_history(self):
        gw, _, _ = make_gateway()
        with self.assertRaises(ValidationError):
            gw.start_shadow(U, A, "Amex 1000")
        out = gw.start_shadow(U, A, CARD)
        self.assertEqual(out["regulars"], ["Target"])

    def test_read_only(self):
        gw, clock, _ = make_gateway()
        gw.ledger.add_points(U, A, "shopping", "seed", 30, None, clock())
        gw.open_account(U, A, allowance=50)   # even with an account, shadow spends nothing
        gw.start_shadow(U, A, CARD)
        before = gw.ledger.score(U, A, "shopping", clock())
        d = gw.request_spend(req(22))
        self.assertEqual(d.status, "shadow")
        self.assertEqual(d.shadow["would_be"], "approved")
        self.assertIsNone(d.credential)
        self.assertIsNone(d.payment)
        self.assertEqual(gw.account(U, A)["balance"], 50)
        self.assertEqual(gw.ledger.spent_on_mandate("m1"), 0)
        d = gw.request_spend(req(600))   # would be a violation...
        self.assertEqual((d.status, d.shadow["would_be"], d.shadow["violation"]), ("shadow", "denied", True))
        self.assertEqual(gw.ledger.score(U, A, "shopping", clock()), before)   # ...but costs nothing
        self.assertEqual(gw.account(U, A)["status"], "active")
        self.assertEqual(gw.pending(U), [])

    def test_match_review_report_live(self):
        gw, clock, store = make_gateway()
        gw.start_shadow(U, A, CARD)
        ids = []
        for i in range(5):
            ids.append(gw.request_spend(req(22 + i, item=f"thing {i}")).request_id)
            store.add(U, [tx(f"real{i}", clock().date() + timedelta(days=1), amount=22 + i)], "manual", CARD)
            clock.advance(days=3)
        no = gw.request_spend(req(22, merchant="bestbuy.com")).request_id   # never bought
        matches = gw.shadow_match(U)
        self.assertEqual(len(matches), 5)
        self.assertEqual(gw.shadow_match(U), [])   # once each
        self.assertEqual(sum(m["points"] for m in matches), 5)
        report = gw.shadow_report(U, A)
        self.assertEqual(report["matched_real_purchases"], 5)
        self.assertTrue(report["ready"])
        self.assertIn("shadow live", report["next"])
        self.assertEqual(gw.shadow_review(no, False)["result"] in ("agree", "asked"), True)
        self.assertEqual(len(gw.shadow_pending(U)), 5)
        out = gw.go_live(U, A, allowance=20)
        self.assertEqual(out["account"]["allowance"]["balance"], 20)
        self.assertEqual(gw.request_spend(req(10)).status, "approved")   # live now
        with self.assertRaises(ValidationError):
            gw.go_live(U, A)

    def test_match_points_are_capped(self):
        gw, clock, store = make_gateway()
        gw.start_shadow(U, A, CARD)
        for i in range(20):
            gw.request_spend(req(22, item=f"x{i}"))
            store.add(U, [tx(f"r{i}", clock().date(), amount=22)], "manual", CARD)
            clock.advance(days=1)
        gw.shadow_match(U)
        self.assertAlmostEqual(gw.ledger.points_of_kind(U, A, "shopping", "shadow_match"), MATCH_POINTS_CAP)
        self.assertEqual(gw.limits(U, A, "shopping")["tier"], "new")   # shadow alone never reaches established

    def test_review_only_shadow_requests(self):
        gw, _, _ = make_gateway()
        d = gw.request_spend(req(5))
        with self.assertRaises(ValidationError):
            gw.shadow_review(d.request_id, True)

    def test_replay_judges_each_purchase_on_what_was_known_then(self):
        gw, _, store = make_gateway(history=0)
        store.add(U, [tx(1, date(2026, 10, 1), "Costco", 15, "groceries"),
                      tx(2, date(2026, 10, 8), "Costco", 15, "groceries"),
                      tx(3, date(2026, 10, 9), "Corner Bakery", 8, "food")], "manual", CARD)
        r = gw.shadow_replay(U, CARD, days=None)
        self.assertEqual(r["purchases"], 3)
        self.assertEqual(r["tier"], "new")
        # the first visit to an unknown store needs approval, even though later rows know it
        bakery = [e for e in r["examples"]["asked"] if e["merchant"] == "Corner Bakery"]
        self.assertTrue(bakery and "new_merchant" in bakery[0]["reasons"])
        self.assertEqual(set(r["automatic_by_tier"]), {"new", "established", "trusted"})
        self.assertGreaterEqual(r["automatic_by_tier"]["trusted"], r["automatic_by_tier"]["new"])
        self.assertEqual(gw.ledger.db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 0)   # nothing recorded

    def test_replay_on_sample_statements(self):
        gw = Gateway(clock=FakeClock(NOW))
        store = TransactionStore(gw.ledger.db)
        for f in ("statement-jul-sep.csv", "statement-sep-oct.csv"):
            import_file(store, EXAMPLES / f, U, card=CARD)
        r = gw.shadow_replay(U, CARD, days=60)
        self.assertEqual(r["would_go_through"] + r["would_ask_you"] + r["would_block"], r["purchases"])
        self.assertLess(r["automatic_by_tier"]["new"], r["automatic_by_tier"]["established"])


class TestShadowCli(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--db", str(Path(tmp) / "e.db")]
            f = str(EXAMPLES / "statement-jul-sep.csv")
            self.assertEqual(cli_main(base + ["import", f, "--user", U, "--card", CARD]), 0)
            self.assertEqual(cli_main(base + ["shadow", "cards", "--user", U]), 0)
            self.assertEqual(cli_main(base + ["shadow", "profile", "--user", U, "--card", CARD]), 0)
            self.assertEqual(cli_main(base + ["shadow", "replay", "--user", U, "--card", CARD]), 0)
            self.assertEqual(cli_main(base + ["shadow", "start", "--user", U, "--agent", A, "--card", CARD]), 0)
            self.assertEqual(cli_main(base + ["shadow", "report", "--user", U, "--agent", A]), 0)
            self.assertEqual(cli_main(base + ["shadow", "start", "--user", U, "--agent", A, "--card", "nope"]), 2)
            with self.assertRaises(SystemExit):
                cli_main(base + ["shadow", "start", "--user", U, "--agent", A])   # needs --card


if __name__ == "__main__":
    unittest.main()
