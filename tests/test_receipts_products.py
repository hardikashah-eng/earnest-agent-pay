import mailbox
import sqlite3
import tempfile
import unittest
from datetime import date, datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from earnest import Gateway, SpendRequest, ValidationError
from earnest.history import PastPurchase, summarize
from earnest.insights.orders import parse_orders_csv
from earnest.insights.receipts import extract_items, import_receipts, parse_receipt, read_imap, read_path
from earnest.ledger import Ledger
from earnest.merchants import MerchantRegistry
from earnest.products import asin_from_url, gtin_check_digit_ok, normalize
from earnest.simulate import FakeClock
from earnest.verify import LineItem, check_cart

ROOT = Path(__file__).resolve().parents[1]
RECEIPTS = ROOT / "examples" / "receipts"
USER = "u1"
REG = MerchantRegistry.default()


def receipt(sender="auto-confirm@amazon.com", subject="Your Amazon.com order #112-0000000-7654321",
            body="Widget Pro 3000\nQty: 1\n$19.99\nOrder Total: $19.99\n", dkim="amazon.com",
            when="Mon, 03 Nov 2026 10:00:00 -0800") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Date"] = sender, "you@example.com", subject, when
    if dkim:
        m["Authentication-Results"] = f"mx.example.com; dkim=pass header.d={dkim}; spf=pass"
    m.set_content(body)
    return m.as_bytes()


class TestProducts(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize("B000TEST01"), "asin:B000TEST01")
        self.assertEqual(normalize("036000291452"), "gtin:00036000291452")   # UPC-A
        self.assertIsNone(normalize("036000291453"))                          # bad check digit
        self.assertEqual(normalize("1234567", "sku", "costco.com"), "sku:costco.com:1234567")
        self.assertEqual(normalize("asin:b00mnv8e0c"), "asin:B00MNV8E0C")
        self.assertTrue(gtin_check_digit_ok("4006381333931"))                 # EAN-13
        self.assertEqual(asin_from_url("https://www.amazon.com/x/dp/B00MNV8E0C/ref=y"), "asin:B00MNV8E0C")

    def test_request_validates_product_id(self):
        base = {"agent_id": "a", "mandate_id": "m", "amount": 5, "merchant": "x", "category": "c", "reason": "r"}
        self.assertEqual(SpendRequest.from_dict({**base, "product_id": "B00MNV8E0C"}).product_id, "asin:B00MNV8E0C")
        with self.assertRaises(ValidationError):
            SpendRequest.from_dict({**base, "product_id": "not an id"})

    def test_amazon_export_carries_asins(self):
        orders = parse_orders_csv((ROOT / "examples/orders/amazon-order-history.csv").read_text(), USER)
        self.assertEqual(orders[0].product_id, "asin:B000TEST01")

    def test_ids_decide_same_item(self):
        keys = REG.canonical("amazon.com")
        def p(item, pid):
            return PastPurchase(frozenset(keys), 12.99, date(2026, 10, 1), "order", item, None, pid)
        # different titles, same ID -> same item
        h = summarize(keys, [p("Anker PowerLine III USB C cable", "asin:B0TESTCAB1")],
                      "usb-c charging cord", date(2026, 11, 1), product_id="asin:B0TESTCAB1")
        self.assertEqual(h.same_item, 1)
        # near-identical titles, different IDs (6ft vs 3ft) -> only similar
        h = summarize(keys, [p("Anker USB-C to USB-C Cable 6ft", "asin:B0TESTCAB1")],
                      "Anker USB-C to USB-C Cable 3ft", date(2026, 11, 1), product_id="asin:B0TESTCAB3")
        self.assertEqual((h.same_item, h.similar_item), (0, 1))

    def test_cart_check_uses_ids(self):
        good = [LineItem("ANKR CBL C-C 6FT BLK", 12.99, product_id="asin:B0TESTCAB1")]
        self.assertTrue(check_cart("Anker USB-C cable 6ft", good, "asin:B0TESTCAB1")[0])
        bad = [LineItem("Anker USB-C cable 6ft", 12.99, product_id="asin:B0OTHER000")]
        self.assertFalse(check_cart("Anker USB-C cable 6ft", bad, "asin:B0TESTCAB1")[0])

    def test_old_database_gets_product_id_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "old.db")
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE orders (id TEXT PRIMARY KEY, user_id TEXT NOT NULL, merchant TEXT NOT NULL, "
                       "item TEXT NOT NULL, unit_price REAL NOT NULL, quantity INTEGER NOT NULL, date TEXT NOT NULL, "
                       "source TEXT NOT NULL)")
            db.execute("INSERT INTO orders VALUES ('o', 'u1', 'amazon.com', 'x', 1, 1, '2026-01-01', 'm')")
            db.commit()
            db.close()
            ledger = Ledger(path)
            self.assertIsNone(ledger.orders(USER)[0]["product_id"])
            ledger.close()


class TestReceipts(unittest.TestCase):
    def test_sample_receipts(self):
        amazon = parse_receipt((RECEIPTS / "amazon-order.eml").read_bytes(), USER)
        self.assertEqual([(o.item, o.unit_price, o.quantity, o.product_id) for o in amazon.orders], [
            ("Amazon Basics AA Batteries, 24 Pack", 16.49, 1, "asin:B00MNV8E0C"),
            ("Anker USB-C to USB-C Cable 6ft", 12.99, 2, "asin:B0TESTCAB1")])
        costco = parse_receipt((RECEIPTS / "costco-receipt.eml").read_bytes(), USER)
        self.assertEqual(costco.orders[0].product_id, "sku:costco.com:1234567")
        self.assertEqual((costco.orders[1].unit_price, costco.orders[1].quantity), (26.99, 2))

    def test_forged_and_irrelevant_mail_is_skipped(self):
        cases = {
            "unauthenticated": receipt(dkim=None),
            "unauthenticated ": receipt(dkim="evil.example"),                      # DKIM for another domain
            "not_a_verified_merchant": receipt(sender="deals@amaz0n-offers.com", dkim="amaz0n-offers.com"),
            "refund_or_return": receipt(subject="Your refund for order #112-0000000-7654321"),
            "not_a_receipt": receipt(subject="Deals picked for you"),
            "no_items_found": receipt(body="Thanks for shopping with us."),
        }
        for want, raw in cases.items():
            self.assertEqual(parse_receipt(raw, USER).skipped, want.strip(), want)
        self.assertEqual(len(parse_receipt(receipt(dkim=None), USER, require_dkim=False).orders), 1)

    def test_subdomain_sender_with_parent_dkim(self):
        raw = receipt(sender="orders@email.walmart.com", subject="Thanks for your order", dkim="walmart.com")
        self.assertEqual(parse_receipt(raw, USER).merchant, "walmart.com")

    def test_line_shapes(self):
        items = extract_items("Organic Bananas 3 lb  $2.49\nPaper towels x 2  $9.98\nSubtotal $12.47\n"
                              "Dish soap\n$3.99\nSales Tax $0.50")
        self.assertEqual(items, [("Organic Bananas 3 lb", 2.49, 1, None), ("Paper towels", 4.99, 2, None),
                                 ("Dish soap", 3.99, 1, None)])

    def test_order_and_shipping_emails_count_once(self):
        gw = Gateway()
        msgs = [receipt(), receipt(subject="Your Amazon.com order #112-0000000-7654321 has shipped",
                                   when="Wed, 05 Nov 2026 10:00:00 -0800")]
        r = import_receipts(gw.ledger, msgs, USER)
        self.assertEqual((r["added"], r["duplicates"]), (1, 1))

    def test_read_path_formats(self):
        with tempfile.TemporaryDirectory() as tmp:
            box = mailbox.mbox(str(Path(tmp) / "takeout.mbox"))
            box.add(receipt())
            box.flush()
            box.close()
            self.assertEqual(len(list(read_path(Path(tmp) / "takeout.mbox"))), 1)
            self.assertEqual(len(list(read_path(RECEIPTS))), 2)

    def test_imap_reads_retailer_mail_read_only(self):
        calls = []

        class FakeImap:
            def __init__(self, host): calls.append(("connect", host))
            def login(self, u, p): calls.append(("login", u))
            def select(self, folder, readonly=False): calls.append(("select", readonly))
            def search(self, charset, *criteria):
                return "OK", [b"1" if '"amazon.com"' in criteria else b""]
            def fetch(self, num, what):
                calls.append(("fetch", what))
                return "OK", [(b"1", receipt())]
            def logout(self): calls.append(("logout",))

        msgs = list(read_imap("imap.example.com", "me", "pw", date(2026, 10, 1), ["amazon.com", "costco.com"],
                              connect=FakeImap))
        self.assertEqual(len(msgs), 1)
        self.assertIn(("select", True), calls)
        self.assertIn(("fetch", "(BODY.PEEK[])"), calls)  # doesn't mark mail as read

    def test_receipt_history_drives_confidence(self):
        gw = Gateway(clock=FakeClock(datetime(2026, 11, 10, 12, tzinfo=timezone.utc)))
        gw.set_profile({"user_id": USER})
        gw.add_mandate({"id": "m1", "user_id": USER, "intent": "restock", "task_type": "shopping",
                        "budget": {"max": 100}})
        gw.ledger.add_points(USER, "a1", "shopping", "seed", 1, None, gw.clock())
        import_receipts(gw.ledger, read_path(RECEIPTS), USER)
        d = gw.request_spend({"agent_id": "a1", "mandate_id": "m1", "amount": 16.99, "merchant": "amazon.com",
                              "category": "household", "reason": "restock", "item": "AA batteries",
                              "product_id": "B00MNV8E0C"})
        self.assertEqual(d.status, "approved")
        self.assertIn("routine_repeat_purchase", d.confidence["signals"])
        self.assertEqual(d.credential.product_lock, "asin:B00MNV8E0C")


if __name__ == "__main__":
    unittest.main()
