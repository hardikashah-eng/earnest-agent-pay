import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from earnest import Gateway, ValidationError
from earnest.approvals import MemoryNotifier
from earnest.cli import main as cli_main
from earnest.gifting import (Occasion, Option, Person, Preference, budget_for, is_milestone, parse_ics,
                             parse_md, parse_people_csv, score)
from earnest.simulate import FakeClock

U, A = "u1", "giftbot"
NOW = datetime(2026, 11, 13, 12, tzinfo=timezone.utc)
TODAY = NOW.date()
EX = Path(__file__).resolve().parent.parent / "examples" / "gifts"


def make_gateway(points=0, notifier=None):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock, notifier=notifier)
    gw.set_profile({"user_id": U})
    if points:
        gw.ledger.add_points(U, A, "gift_shopping", "seed", points, None, clock())
    gw.set_gift_policy(U, agent_id=A, lead_days=21)
    return gw, clock


def opt(title, price, merchant="amazon.com", **kw):
    return {"title": title, "merchant": merchant, "price": price, "eta_days": kw.pop("eta_days", 3), **kw}


class TestPure(unittest.TestCase):
    def test_next_date_and_leap_day(self):
        o = Occasion("o", "p", "birthday", 2, 29, 2000)
        self.assertEqual(o.next_on(date(2026, 11, 13)), date(2027, 2, 28))
        self.assertEqual(o.next_on(date(2027, 12, 1)), date(2028, 2, 29))
        self.assertEqual(Occasion("o", "p", "birthday", 11, 13).next_on(TODAY), TODAY)

    def test_describe_and_milestones(self):
        bday = Occasion("o", "p", "birthday", 11, 20, 1996)
        self.assertEqual(bday.describe("Sam", date(2026, 11, 20)), "Sam's 30th birthday")
        self.assertTrue(is_milestone(bday, date(2026, 11, 20)))
        ann = Occasion("o", "p", "anniversary", 11, 28, 2015)
        self.assertEqual(ann.describe("Priya", date(2026, 11, 28)), "11th anniversary with Priya")
        self.assertFalse(is_milestone(ann, date(2026, 11, 28)))
        self.assertEqual(Occasion("o", "p", "birthday", 1, 1).describe("Ana", TODAY), "Ana's birthday")

    def test_budget(self):
        friend = Person("p", "Sam", "friend")
        occ = Occasion("o", "p", "birthday", 11, 20, 1996)
        self.assertEqual(budget_for(friend, occ, date(2026, 11, 20))[0], 60.0)        # 40 x 1.5 for the 30th
        self.assertEqual(budget_for(friend, occ, date(2027, 11, 20))[0], 40.0)
        self.assertEqual(budget_for(Person("p", "S", "friend", budget=55), occ, date(2027, 11, 20))[0], 55.0)
        self.assertEqual(budget_for(friend, occ, date(2027, 11, 20), {"friend": 70})[0], 70.0)
        b, why = budget_for(friend, occ, date(2027, 11, 20), remaining_this_year=12)
        self.assertEqual(b, 12.0)
        self.assertIn("limited by what's left of this year's gift budget", why)

    def test_score(self):
        p = Person("p", "Priya", "spouse")
        prefs = [Preference("Le Creuset dutch oven", "wish", "wishlist"), Preference("pottery", "like", "you"),
                 Preference("scented candles", "dislike", "you")]
        on = date(2026, 11, 28)
        wish, _, probs = score(Option("Le Creuset 3.5qt Dutch Oven", "a.com", 140, eta_days=3), p, prefs, 150, on, TODAY)
        like, _, _ = score(Option("Pottery class", "b.com", 140, digital=True), p, prefs, 150, on, TODAY)
        self.assertEqual(probs, [])
        self.assertGreater(wish, like)
        self.assertIn("disliked:scented candles",
                      score(Option("Scented candles set", "c.com", 40), p, prefs, 150, on, TODAY)[2])
        self.assertTrue(score(Option("Pottery wheel", "c.com", 200), p, prefs, 150, on, TODAY)[2][0]
                        .startswith("over_budget"))
        self.assertTrue(score(Option("Pottery book", "c.com", 50, eta_days=20), p, prefs, 150, on, TODAY)[2][0]
                        .startswith("arrives_late"))
        self.assertIn("already_given:pottery class",
                      score(Option("Pottery class", "b.com", 140), p, prefs, 150, on, TODAY,
                            given_before=["pottery class"])[2])
        self.assertIn("untrusted_merchant",
                      score(Option("Pottery class", "b.com", 140), p, prefs, 150, on, TODAY,
                            merchant_status="untrusted")[2])
        paid, why, _ = score(Option("Pottery class", "b.com", 140), p, prefs, 150, on, TODAY, gift_card_balance=150)
        self.assertGreater(paid, like)
        self.assertIn("paid from a gift card you linked", why)

    def test_option_validation(self):
        for bad in ({"title": "x", "merchant": "a.com"}, {"title": "x", "merchant": "a.com", "price": 0},
                    {"title": "x", "merchant": "a.com", "price": 5, "colour": "red"}, "nope"):
            with self.assertRaises(ValidationError):
                Option.from_dict(bad)

    def test_parsers(self):
        self.assertEqual(parse_md("1990-03-14"), (1990, 3, 14))
        self.assertEqual(parse_md("02-29"), (None, 2, 29))
        for bad in ("14/03/1990", "13-40"):
            with self.assertRaises(ValidationError):
                parse_md(bad)
        rows = parse_people_csv((EX / "people.csv").read_text())
        self.assertEqual([r["name"] for r in rows], ["Priya", "Sam", "Mom", "Ana"])
        self.assertEqual(rows[0]["occasions"], [("birthday", 1988, 3, 14), ("anniversary", 2015, 11, 28)])
        self.assertEqual(rows[1]["auto_buy_max"], 50.0)
        events = parse_ics((EX / "birthdays.ics").read_text())
        self.assertEqual([(e["name"], e["kind"], e["year"]) for e in events],
                         [("Leo", "birthday", 1995), ("Grandpa Joe", "birthday", None)])


class TestPeople(unittest.TestCase):
    def test_add_update_and_preferences(self):
        gw, _ = make_gateway()
        gw.add_person(U, "Sam", "friend", likes=["hiking"], sizes={"shirt": "M"})
        card = gw.add_person(U, "sam", budget=55, likes=["coffee"])   # same person, by name
        self.assertEqual((card["name"], card["relationship"], card["budget"]), ("Sam", "friend", 55))
        self.assertEqual([p["text"] for p in card["preferences"]], ["hiking", "coffee"])
        self.assertEqual(card["sizes"], {"shirt": "M"})
        with self.assertRaises(ValidationError):
            gw.add_gift_preference(U, "Sam", "x", "like", "stalking")
        with self.assertRaises(ValidationError):
            gw.add_gift_preference(U, "Nobody", "x")

    def test_imports(self):
        gw, _ = make_gateway()
        out = gw.import_people(U, (EX / "people.csv").read_text())
        self.assertEqual(out, {"people": 4, "new": 4})
        self.assertEqual(gw.import_people(U, (EX / "people.csv").read_text())["new"], 0)
        self.assertEqual(len(gw.ledger.occasions(U)), 5)   # no duplicates on re-import
        cal = gw.import_calendar(U, (EX / "birthdays.ics").read_text())
        self.assertEqual(cal["new_people"], ["Leo", "Grandpa Joe"])
        self.assertIsNotNone(cal["note"])


class TestPlanning(unittest.TestCase):
    def test_plans_inside_lead_window_once(self):
        gw, clock = make_gateway()
        gw.import_people(U, (EX / "people.csv").read_text())
        plans = gw.plan_gifts(U)
        self.assertEqual(sorted(p["what"] for p in plans),
                         ["11th anniversary with Priya", "Ana's birthday", "Sam's 36th birthday"])
        self.assertEqual(gw.plan_gifts(U), [])   # once per occasion and year
        up = gw.upcoming_gifts(U, 30)
        self.assertEqual(up[0]["what"], "Ana's birthday")
        clock.advance(days=10)   # Ana's birthday (Nov 18) passed unbought
        gw.plan_gifts(U)
        statuses = {p["person_id"]: p["status"] for p in gw.ledger.plans(U)}
        self.assertIn("missed", statuses.values())

    def test_annual_budget(self):
        gw, _ = make_gateway()
        gw.set_gift_policy(U, annual_budget=60)
        gw.add_person(U, "Sam", "friend")
        gw.add_person(U, "Ana", "friend")
        gw.add_occasion(U, "Sam", "birthday", "11-20")
        gw.add_occasion(U, "Ana", "birthday", "11-22")
        plans = sorted(gw.plan_gifts(U), key=lambda p: p["date"])
        self.assertEqual([p["budget"] for p in plans], [40.0, 20.0])
        with self.assertRaises(ValidationError):
            gw.set_gift_policy(U, lead_days=0)


class TestFlow(unittest.TestCase):
    def setUp(self):
        self.notifier = MemoryNotifier()
        self.gw, self.clock = make_gateway(notifier=self.notifier)
        gw = self.gw
        gw.add_person(U, "Priya", "spouse", likes=["pottery", "jazz vinyl"], dislikes=["scented candles"],
                      address="12 Elm St")
        gw.add_gift_preference(U, "Priya", "Le Creuset dutch oven", "wish", "wishlist")
        gw.add_occasion(U, "Priya", "anniversary", "2015-11-28")
        self.plan = gw.plan_gifts(U)[0]["plan"]

    def test_brief_has_no_address(self):
        b = self.gw.gift_brief(self.plan)
        self.assertEqual((b["for"], b["budget"], b["avoid"]), ("Priya", 150.0, ["scented candles"]))
        self.assertIn("Le Creuset dutch oven (wishlist)", b["wishes"])
        self.assertNotIn("12 Elm St", json.dumps(b))

    def test_asks_then_approve_with_stretch(self):
        gw = self.gw
        r = gw.propose_gifts(self.plan, [opt("Le Creuset 3.5qt Dutch Oven", 210, rating=4.7),
                                         opt("Pottery wheel class for two", 140, "classbento.com", digital=True),
                                         opt("Scented candles set", 40, "target.com")], A)
        self.assertEqual((r["status"], r["choice"]["title"]), ("awaiting_approval", "Pottery wheel class for two"))
        self.assertIn("mandate_requires_approval:always", r["reasons"])
        self.assertEqual(self.notifier.sent[0].item, "Pottery wheel class for two")
        self.assertIn("anniversary with Priya", self.notifier.sent[0].reason_given)
        with self.assertRaises(ValidationError):
            gw.approve_gift(self.plan, option=0)   # over budget until you raise it
        gw.set_gift_budget(self.plan, 210)
        out = gw.approve_gift(self.plan, option=0)
        self.assertEqual((out["status"], out["choice"]["title"]), ("approved", "Le Creuset 3.5qt Dutch Oven"))
        self.assertEqual(gw.ledger.get_request(r["request_id"])["status"], "denied")   # the first ask withdrawn
        buy = gw.gift_purchase(self.plan)
        self.assertEqual((buy["ship_to"], buy["gift_message"]), ("12 Elm St", "Happy 11th anniversary!"))
        self.assertEqual(buy["order_by"], "2026-11-24")
        self.assertEqual(buy["credential"]["merchant_lock"], "amazon.com")
        self.assertEqual(gw.gift_tasks(A)[0]["todo"], "buy and send")
        self.assertEqual(gw.gift_ordered(self.plan, A, "112-33")["status"], "ordered")
        fb = gw.gift_feedback(self.plan, "loved")
        self.assertEqual(fb["trust_events"][0]["kind"], "outcome_accepted")
        card = gw.person_card(U, "Priya")
        self.assertEqual(card["given_before"][0]["title"], "Le Creuset 3.5qt Dutch Oven")
        self.assertEqual(card["preferences"][-1]["source"], "feedback")

    def test_none_fit_suggests_stretch(self):
        r = self.gw.propose_gifts(self.plan, [opt("Le Creuset 5.5qt Dutch Oven", 420), opt("Jazz vinyl", 90, eta_days=30)], A)
        self.assertEqual(r["status"], "open")
        self.assertIn("earnest gifts budget", r["stretch"])

    def test_approve_through_the_usual_path_updates_plan(self):
        r = self.gw.propose_gifts(self.plan, [opt("Pottery wheel class for two", 140, "classbento.com", digital=True)], A)
        self.gw.resolve(r["request_id"], approve=True)
        self.assertEqual(self.gw.gift_plan_view(self.plan)["status"], "approved")
        self.assertEqual(self.gw.gift_purchase(self.plan)["order_by"], None)   # digital

    def test_decline_then_skip(self):
        r = self.gw.propose_gifts(self.plan, [opt("Pottery wheel class for two", 140, "classbento.com", digital=True)], A)
        self.gw.resolve(r["request_id"], approve=False)
        self.assertEqual(self.gw.gift_plan_view(self.plan)["status"], "declined")
        self.assertEqual(self.gw.skip_gift(self.plan)["status"], "skipped")
        with self.assertRaises(ValidationError):
            self.gw.propose_gifts(self.plan, [opt("x", 10)], A)

    def test_other_agent_cant_take_plan(self):
        with self.assertRaises(ValidationError):
            self.gw.propose_gifts(self.plan, [opt("x", 10)], "someone_else")

    def test_repeat_gift_ruled_out(self):
        self.gw.ledger.add_gift_history(U, self.gw.ledger.person(U, "Priya")["id"], "Pottery wheel class for two",
                                        2025, 140, None, "you")
        r = self.gw.propose_gifts(self.plan, [opt("Pottery wheel class for two", 140, "classbento.com", digital=True)], A)
        self.assertEqual(r["status"], "open")
        self.assertTrue(r["shortlist"][0]["problems"][0].startswith("already_given"))

    def test_catalog_fallback(self):
        self.gw.load_gift_catalog(json.loads((EX / "popular.json").read_text()))
        r = self.gw.propose_gifts(self.plan)
        self.assertIn(r["status"], ("awaiting_approval", "open"))
        self.assertTrue(all(x["option"]["source"] == "popular" for x in self.gw.ledger.plan(self.plan)["options"]))


class TestAutoBuy(unittest.TestCase):
    def setUp(self):
        self.gw, _ = make_gateway(points=30)   # an established gift shopper
        gw = self.gw
        gw.open_account(U, A)
        gw.add_card(U, A, "gift_card", 60, "4417", "target.com")
        gw.add_person(U, "Sam", "friend", likes=["hiking", "coffee"], budget=60, auto_buy_max=50)
        gw.add_occasion(U, "Sam", "birthday", "1990-11-20")
        self.plan = gw.plan_gifts(U)[0]["plan"]

    def test_needs_master_switch(self):
        r = self.gw.propose_gifts(self.plan, json.loads((EX / "options-sam.json").read_text()), A)
        self.assertEqual(r["status"], "awaiting_approval")   # policy auto_buy is off

    def test_buys_within_authorization_from_gift_card(self):
        self.gw.set_gift_policy(U, auto_buy=True)
        b = self.gw.gift_brief(self.plan)
        self.assertEqual(b["gift_cards"], [{"store": "target.com", "balance": 60.0}])
        r = self.gw.propose_gifts(self.plan, json.loads((EX / "options-sam.json").read_text()), A)
        self.assertEqual((r["status"], r["choice"]["title"]), ("approved", "Insulated hiking coffee thermos"))
        self.assertIn("paid from a gift card you linked", r["why"])
        pay = self.gw.gift_purchase(self.plan)["payment"]
        self.assertEqual((pay["method"], pay["remaining"]), ("gift_card", 26.0))

    def test_over_authorization_asks(self):
        self.gw.set_gift_policy(U, auto_buy=True)
        r = self.gw.propose_gifts(self.plan, [opt("Hiking boots", 58, "target.com")], A)
        self.assertEqual(r["status"], "awaiting_approval")

    def test_account_must_cover_it(self):
        self.gw.set_gift_policy(U, auto_buy=True)
        r = self.gw.propose_gifts(self.plan, [opt("Pour-over coffee kit", 42)], A)   # amazon: no funds there
        self.assertEqual(r["status"], "awaiting_approval")
        self.assertIn("account:insufficient_balance:0.00", r["reasons"])
        with self.assertRaises(ValidationError):
            self.gw.approve_gift(self.plan)
        self.gw.top_up(U, A, 50)
        self.assertEqual(self.gw.approve_gift(self.plan)["status"], "approved")


class TestGiftsCli(unittest.TestCase):
    def test_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = ["--db", str(Path(tmp) / "e.db")]
            run = lambda *a: cli_main(base + ["gifts", *a, "--user", U])
            self.assertEqual(run("import-people", str(EX / "people.csv")), 0)
            self.assertEqual(run("import-calendar", str(EX / "birthdays.ics")), 0)
            self.assertEqual(run("add-person", "Leo", "--relationship", "sibling", "--likes", "chess;tea"), 0)
            self.assertEqual(run("like", "Leo", "board games", "--pref", "wish", "--source", "wishlist"), 0)
            self.assertEqual(run("policy", "--agent", A, "--lead-days", "21", "--budget-for", "friend=45"), 0)
            self.assertEqual(run("upcoming"), 0)
            self.assertEqual(run("plan"), 0)
            self.assertEqual(run("catalog", str(EX / "popular.json")), 0)
            self.assertEqual(run("person", "Nobody"), 2)
            with self.assertRaises(SystemExit):
                run("add-date", "Leo")   # needs --date


if __name__ == "__main__":
    unittest.main()
