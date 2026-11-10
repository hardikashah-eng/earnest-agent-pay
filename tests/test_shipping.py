import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.cli import main as cli_main
from earnest.selling import Comp, MockMarketplace, SellDesk, StaticComps
from earnest.selling.platforms import EbayConfig, EbayInventoryAdapter
from earnest.selling.shipping import ShippoLabels, add_business_days, approx_rate, plan
from earnest.simulate import FakeClock

U = "user_demo"
NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)   # a Friday
BUYER = {"name": "Pat Buyer", "street1": "1 Main St", "city": "Austin", "state": "TX", "zip": "78701", "country": "US"}
ME = {"name": "Me", "street1": "9 Oak Ave", "city": "San Diego", "state": "CA", "zip": "92101"}


class FakeShippo:
    def __init__(self, rates=None, fail=False):
        self.calls, self.fail = [], fail
        self._rates = rates or [
            {"object_id": "r_media", "amount": "4.10", "provider": "USPS", "servicelevel": {"token": "usps_media_mail", "name": "Media Mail"}},
            {"object_id": "r_ground", "amount": "6.20", "provider": "USPS", "servicelevel": {"token": "usps_ground_advantage", "name": "Ground Advantage"}},
            {"object_id": "r_pri", "amount": "9.90", "provider": "USPS", "servicelevel": {"token": "usps_priority", "name": "Priority"}}]

    def __call__(self, method, path, body):
        self.calls.append((method, path, body))
        if path == "/shipments/":
            return {"rates": self._rates}
        if path == "/transactions/":
            if self.fail:
                return {"status": "ERROR", "messages": ["address invalid"]}
            return {"status": "SUCCESS", "label_url": f"https://labels.example/{body['rate']}.pdf",
                    "tracking_number": "9400TEST", "tracking_url_provider": "https://tools.usps.com/x"}
        raise AssertionError(path)


def sold_desk(category="books", amount=12.0, labels=None, title="The Hobbit paperback"):
    gw = Gateway(clock=FakeClock(NOW))
    market = MockMarketplace()
    desk = SellDesk(gw, {"mockbay": market}, [], [StaticComps([Comp(14, "sold", "t")] * 5)], labels)
    desk.link_platform(U, "mockbay")
    r = desk.quick_sell(U, title, floor=10, category=category, agent_id=None)
    ext = desk.listings(r["item_id"], "active")[0]["external_id"]
    market.receive_offer(ext, amount)
    market.sales[-1].ship_to = BUYER
    sale = desk.sync()[0]
    return gw, desk, sale


class TestPlans(unittest.TestCase):
    def test_books_go_media_mail(self):
        p = plan("books", "mockbay", date(2026, 11, 13))
        self.assertEqual((p.service, p.est_cost, p.label_from), ("usps_media_mail", 4.63, "you"))
        self.assertIn("Media Mail", p.notes[0])
        self.assertEqual(p.ship_by, date(2026, 11, 17))           # Fri + 2 business days = Tue

    def test_platform_labels(self):
        self.assertEqual(plan("apparel", "poshmark", date(2026, 11, 13)).est_cost, 0.0)
        self.assertEqual(plan("books", "buyback:bookbuyer", date(2026, 11, 13)).label_from, "buyback")
        self.assertEqual(plan("electronics", "ebay", date(2026, 11, 13)).label_from, "platform")
        e = plan("electronics", "ebay", date(2026, 11, 13), label_api=True)
        self.assertEqual((e.label_from, e.service, e.fragile), ("shippo", "usps_ground_advantage", True))
        self.assertIn("Factory reset", e.packing[0])

    def test_rates_and_dates(self):
        self.assertEqual(approx_rate("usps_ground_advantage", 3), 5.40)
        self.assertEqual(approx_rate("usps_media_mail", 40), round(4.63 + 2 * 0.80, 2))
        self.assertIsNone(approx_rate("usps_priority", 500))
        self.assertEqual(add_business_days(date(2026, 11, 14), 1), date(2026, 11, 16))


class TestShippo(unittest.TestCase):
    def test_cheapest_respects_media_mail_rules(self):
        rates = FakeShippo()._rates
        self.assertEqual(ShippoLabels.cheapest(rates, True)["object_id"], "r_media")
        self.assertEqual(ShippoLabels.cheapest(rates, False)["object_id"], "r_ground")

    def test_label_purchase_and_failure(self):
        fake = FakeShippo()
        label = ShippoLabels(fake).buy("r_ground")
        self.assertEqual(label["tracking_number"], "9400TEST")
        with self.assertRaises(RuntimeError):
            ShippoLabels(FakeShippo(fail=True)).buy("r_ground")


class TestSaleLifecycle(unittest.TestCase):
    def test_sale_comes_with_a_shipping_plan(self):
        gw, desk, sale = sold_desk()
        self.assertEqual(sale["ship_by"], "2026-11-17")
        self.assertEqual(sale["shipping"]["service"], "usps_media_mail")
        self.assertEqual(desk.sale(sale["sale_id"])["data"]["ship_to"], BUYER)

    def test_buy_label_through_shippo(self):
        fake = FakeShippo()
        gw, desk, sale = sold_desk(labels=ShippoLabels(fake))
        with self.assertRaises(ValidationError):
            desk.ship(sale["sale_id"], buy_label=True)           # no ship-from address yet
        desk.set_ship_from(U, ME)
        r = desk.ship(sale["sale_id"], buy_label=True)
        self.assertEqual((r["label"]["cost"], r["label"]["service"]), (4.10, "Media Mail"))
        parcel = next(b for _, p, b in fake.calls if p == "/shipments/")["parcels"][0]
        self.assertEqual((parcel["weight"], parcel["mass_unit"]), ("16", "oz"))
        self.assertEqual(desk.sale(sale["sale_id"])["status"], "label_ready")

    def test_agent_label_spend_is_capped(self):
        expensive = [{"object_id": "r", "amount": "6.00", "provider": "USPS",
                      "servicelevel": {"token": "usps_ground_advantage", "name": "Ground"}}]
        gw, desk, sale = sold_desk(category="toys", amount=12.0, labels=ShippoLabels(FakeShippo(expensive)),
                                   title="Lego Classic 10698")
        desk.set_ship_from(U, ME)
        r = desk.ship(sale["sale_id"], buy_label=True, agent_id="a1")    # $6 > 25% of $12
        self.assertIn("needs_user", r)
        self.assertEqual(desk.sale(sale["sale_id"])["status"], "sold")
        self.assertIn("label", desk.ship(sale["sale_id"], buy_label=True))   # the user can

    def test_status_flow_and_todo(self):
        gw, desk, sale = sold_desk()
        sid = sale["sale_id"]
        self.assertEqual(desk.todo(U)[0]["urgency"], 2)
        gw.clock.advance(hours=24 * 5)                             # past ship-by
        todo = desk.todo(U)[0]
        self.assertEqual(todo["urgency"], 0)
        self.assertIn("overdue", todo["todo"])
        desk.mark(sid, "shipped", "9400X")
        self.assertEqual(desk.todo(U), [])
        with self.assertRaises(ValidationError):
            desk.mark(sid, "shipped")
        desk.mark(sid, "delivered")
        self.assertIn("completed", desk.todo(U)[0]["todo"])
        desk.mark(sid, "completed")
        self.assertEqual([e[0] for e in desk.sale(sid)["data"]["events"]],
                         ["sold", "shipped", "delivered", "completed"])

    def test_cancel_puts_item_back(self):
        gw, desk, sale = sold_desk()
        desk.mark(sale["sale_id"], "cancelled")
        self.assertEqual(desk.item(sale["item_id"]).status, "valued")

    def test_ebay_orders_carry_the_buyer_address(self):
        def fake(method, path, body):
            return {"orders": [{"orderId": "O1", "fulfillmentStartInstructions": [{"shippingStep": {"shipTo": {
                "fullName": "Pat Buyer", "contactAddress": {"addressLine1": "1 Main St", "city": "Austin",
                                                            "stateOrProvince": "TX", "postalCode": "78701",
                                                            "countryCode": "US"}}}}],
                "lineItems": [{"sku": "sell_x", "total": {"value": "12.00"}}]}]}
        ev = EbayInventoryAdapter(fake, EbayConfig("p", "r", "f", "l")).poll_sales()[0]
        self.assertEqual((ev.ship_to["city"], ev.ship_to["zip"]), ("Austin", "78701"))


class TestCli(unittest.TestCase):
    def test_address_and_todo(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "t.db")
            self.assertEqual(cli_main(["--db", db, "sell", "address", "--user", U, "--name", "Me", "--street", "9 Oak",
                                       "--city", "San Diego", "--state", "CA", "--zip", "92101"]), 0)
            self.assertEqual(cli_main(["--db", db, "sell", "todo", "--user", U]), 0)


if __name__ == "__main__":
    unittest.main()
