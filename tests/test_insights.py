import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from earnest.cli import main
from earnest.insights import (TransactionStore, clean_merchant, import_file, parse_csv, sync_folder,
                              sync_source, top_insights)
from earnest.insights.detectors import detect_fees, detect_price_creep, find_recurring
from earnest.insights.statement import Transaction, transaction_id
from earnest.models import ValidationError

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "examples" / "statements"
U = "u1"


def store():
    return TransactionStore(sqlite3.connect(":memory:"))


def tx(d, desc, amount, n=1):
    merchant, cat = clean_merchant(desc)
    return Transaction(transaction_id(U, d, desc, amount, n), d, desc, merchant, cat, amount)


class TestParsing(unittest.TestCase):
    def test_signed_amount_negative_charges(self):
        txs = parse_csv("Transaction Date,Description,Amount\n"
                        "07/02/2026,WHOLEFDS MKT #10234 AUSTIN TX,-50.00\n"
                        "07/03/2026,PAYMENT THANK YOU,1000.00\n"
                        "07/04/2026,AMAZON.COM REFUND,12.00\n"
                        "07/05/2026,NETFLIX.COM 866-579-7172 CA,-15.49\n", U)
        # payment skipped; the positive refund is a credit and skipped
        self.assertEqual([(t.merchant, t.amount) for t in txs], [("Whole Foods", 50.0), ("Netflix", 15.49)])
        self.assertEqual(txs[0].category, "groceries")

    def test_signed_amount_positive_charges(self):
        txs = parse_csv("Date,Description,Amount\n2026-07-02,ALDI 72031,42.10\n2026-07-03,REFUND,-5\n", U)
        self.assertEqual([(t.merchant, t.amount) for t in txs], [("Aldi", 42.10)])

    def test_debit_credit_columns_and_money_formats(self):
        txs = parse_csv('Date,Description,Debit,Credit\n2026-07-02,SHELL OIL 57444,"$1,045.50",\n'
                        "2026-07-03,PAYMENT THANK YOU,,500\n", U)
        self.assertEqual([(t.category, t.amount) for t in txs], [("fuel", 1045.50)])

    def test_missing_columns(self):
        with self.assertRaises(ValidationError):
            parse_csv("When,What\n2026-01-01,x\n", U)

    def test_merchant_cleanup(self):
        self.assertEqual(clean_merchant("DOORDASH*THAI KITCHEN"), ("DoorDash", "delivery"))
        self.assertEqual(clean_merchant("UBER *EATS PENDING"), ("Uber Eats", "delivery"))
        self.assertEqual(clean_merchant("UBER TRIP HELP.UBER.COM"), ("Uber", "transport"))
        self.assertEqual(clean_merchant("SQ *BLUE BOTTLE #4421 OAKLAND CA")[0], "Blue Bottle Oakland")
        self.assertEqual(clean_merchant("LATE FEE")[1], "fees")

    def test_identical_purchases_same_day_are_kept(self):
        txs = parse_csv("Date,Description,Amount\n2026-07-02,STARBUCKS,5.00\n2026-07-02,STARBUCKS,5.00\n", U)
        self.assertEqual(len({t.id for t in txs}), 2)


class TestImport(unittest.TestCase):
    def test_overlapping_exports_dedupe(self):
        s = store()
        a = import_file(s, SAMPLES / "statement-jul-sep.csv", U)
        b = import_file(s, SAMPLES / "statement-sep-oct.csv", U)
        self.assertGreater(a.added, 0)
        self.assertGreater(b.duplicates, 0)  # September is in both files, in different formats
        again = import_file(s, SAMPLES / "statement-sep-oct.csv", U)
        self.assertEqual(again.added, 0)
        self.assertEqual(len(s.all(U)), a.added + b.added)

    def test_folder_sync_only_processes_new_files(self):
        s = store()
        with tempfile.TemporaryDirectory() as inbox:
            (Path(inbox) / "a.csv").write_bytes((SAMPLES / "statement-jul-sep.csv").read_bytes())
            first = sync_folder(s, inbox, U)
            self.assertEqual([r.name for r in first], ["a.csv"])
            self.assertEqual(sync_folder(s, inbox, U), [])
            (Path(inbox) / "b.csv").write_bytes((SAMPLES / "statement-sep-oct.csv").read_bytes())
            (Path(inbox) / "broken.csv").write_text("nope\n1\n")
            later = {r.name: r for r in sync_folder(s, inbox, U)}
            self.assertGreater(later["b.csv"].added, 0)
            self.assertIsNotNone(later["broken.csv"].error)

    def test_connector_source(self):
        class FakeBank:
            name = "fakebank"
            def fetch(self, user_id, since):
                return [tx(date(2026, 7, 1), "ALDI 1", 40.0), tx(date(2026, 7, 2), "ALDI 1", 41.0)]
        s = store()
        self.assertEqual(sync_source(s, FakeBank(), U).added, 2)
        self.assertEqual(sync_source(s, FakeBank(), U).added, 0)


class TestDetectors(unittest.TestCase):
    def test_recurring_and_price_creep(self):
        txs = [tx(date(2026, m, 5), "NETFLIX.COM", a) for m, a in ((7, 15.49), (8, 15.49), (9, 17.99), (10, 17.99))]
        self.assertEqual([r.merchant for r in find_recurring(txs, date(2026, 11, 1))], ["Netflix"])
        [i] = detect_price_creep(txs, "2026-10")
        self.assertEqual(i.key, "price_creep:Netflix")
        self.assertAlmostEqual(i.monthly_savings, 2.50)

    def test_cancelled_subscription_is_not_recurring(self):
        txs = [tx(date(2026, m, 5), "HULU", 7.99) for m in (5, 6, 7)]
        self.assertEqual(find_recurring(txs, date(2026, 11, 1)), [])

    def test_fees(self):
        txs = [tx(date(2026, 10, 3), "LATE FEE", 39.0), tx(date(2026, 9, 3), "LATE FEE", 39.0)]
        [i] = detect_fees(txs, "2026-10")
        self.assertEqual(i.monthly_savings, 39.0)
        self.assertIn("autopay", i.action)

    def test_sample_report(self):
        s = store()
        for f in sorted(SAMPLES.glob("*.csv")):
            import_file(s, f, U)
        r = top_insights(s.all(U), "2026-10")
        kinds = [i.kind for i in r.insights]
        self.assertEqual(len(kinds), 5)
        for expected in ("retailer_switch", "fees", "delivery", "spike"):
            self.assertIn(expected, kinds)
        self.assertEqual(len(kinds), len(set(kinds)))  # no duplicated insight types in the sample
        scores = [i.rank_score for i in r.insights]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_dismissed_insights_are_hidden(self):
        s = store()
        for f in sorted(SAMPLES.glob("*.csv")):
            import_file(s, f, U)
        s.dismiss(U, "fees:2026-10")
        r = top_insights(s.all(U), "2026-10", s.dismissed(U))
        self.assertNotIn("fees", [i.kind for i in r.insights])
        self.assertEqual(r.hidden_dismissed, 1)

    def test_earlier_month_ignores_later_data(self):
        s = store()
        for f in sorted(SAMPLES.glob("*.csv")):
            import_file(s, f, U)
        everything = s.all(U)
        r = top_insights(everything, "2026-08")
        self.assertEqual(r.transactions_considered, sum(t.date < date(2026, 9, 1) for t in everything))
        self.assertIn("fees:2026-08", [i.key for i in r.insights])
        self.assertNotIn("fees:2026-10", [i.key for i in r.insights])


class TestCli(unittest.TestCase):
    def test_import_and_insights_commands(self):
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / "e.db")
            self.assertEqual(main(["--db", db, "import", str(SAMPLES / "statement-jul-sep.csv"), "--user", U]), 0)
            self.assertEqual(main(["--db", db, "insights", "--user", U, "--json"]), 0)
            self.assertEqual(main(["--db", db, "dismiss-insight", "fees:2026-08", "--user", U]), 0)
            self.assertEqual(main(["--db", db, "insights", "--user", "nobody"]), 2)


if __name__ == "__main__":
    unittest.main()
