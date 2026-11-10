import hashlib
import hmac
import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

from earnest import Gateway
from earnest.ap2 import verify_attestation
from earnest.approvals import (ChannelNotifier, MemoryNotifier, MultiNotifier, lark_payload, make_token,
                               notifier_from_env, ntfy_request, read_token, slack_payload)
from earnest.backends.stripe_issuing import (STRIPE_VERSION, StripeConfigError, StripeIssuingBackend,
                                             _flatten, verify_signature)
from earnest.models import ValidationError
from earnest.server import serve
from earnest.simulate import FakeClock

USER = "u1"
NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


class FakeStripe:
    def __init__(self):
        self.calls, self.cards = [], {}

    def __call__(self, method, path, params):
        self.calls.append((method, path, params))
        if method == "POST" and path == "/v1/issuing/cards":
            card = {"id": f"ic_{len(self.cards) + 1}", "metadata": params["metadata"], "status": "active"}
            self.cards[card["id"]] = card
            return card
        if method == "GET":
            return self.cards[path.rsplit("/", 1)[-1]]
        if method == "POST" and path.startswith("/v1/issuing/cards/"):
            self.cards[path.rsplit("/", 1)[-1]].update(params)
            return self.cards[path.rsplit("/", 1)[-1]]
        raise AssertionError(path)


def make_gateway(**kw):
    clock = FakeClock(NOW)
    gw = Gateway(clock=clock, **kw)
    gw.set_profile({"user_id": USER})
    gw.add_mandate({"id": "m1", "user_id": USER, "intent": "household restock", "task_type": "shopping",
                    "budget": {"max": 500}})
    gw.ledger.add_points(USER, "a1", "shopping", "seed", 30, None, clock())
    return gw, clock


def req(amount=20, merchant="amazon.com", item="AA batteries 24 pack"):
    return {"agent_id": "a1", "mandate_id": "m1", "amount": amount, "merchant": merchant,
            "category": "household", "reason": "restock", "item": item}


def signed(payload: bytes, secret: str, ts: int | None = None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


class TestStripe(unittest.TestCase):
    def setUp(self):
        self.fake = FakeStripe()
        self.backend = StripeIssuingBackend("ich_1", transport=self.fake)

    def test_refuses_live_keys(self):
        with self.assertRaises(StripeConfigError):
            StripeIssuingBackend("ich_1", api_key="sk_live_abc", transport=self.fake)

    def test_form_encoding(self):
        self.assertIn(("spending_controls[spending_limits][0][amount]", "2000"),
                      _flatten({"spending_controls": {"spending_limits": [{"amount": 2000}]}}))

    def test_gateway_issues_a_capped_virtual_card(self):
        gw, _ = make_gateway(backend=self.backend)
        d = gw.request_spend(req())
        self.assertEqual(d.credential.id, "ic_1")
        params = self.fake.calls[0][2]
        self.assertEqual(params["spending_controls"]["spending_limits"][0], {"amount": 2000, "interval": "all_time"})
        self.assertEqual(params["metadata"]["earnest_merchant_lock"], "amazon.com")
        self.assertEqual(params["metadata"]["earnest_item_lock"], "AA batteries 24 pack")

    def test_authorization_decisions(self):
        cred = self.backend.issue(cap=20, currency="USD", merchant="amazon.com", lifetime_hours=1,
                                  single_use=True, now=NOW, item="AA batteries")
        card = self.fake.cards[cred.id]
        auth = lambda name, cents, now=NOW: self.backend.decide(  # noqa: E731
            {"card": card, "merchant_data": {"name": name}, "pending_request": {"amount": cents}}, now)
        self.assertEqual(auth("AMZN Mktp US*2K4L1", 1800), (True, "approved"))
        self.assertEqual(auth("WALMART.COM", 1800)[1], "wrong_merchant")
        self.assertEqual(auth("AMAZON.COM", 2500)[1], "over_cap")
        self.assertEqual(auth("AMAZON.COM", 1800, NOW + timedelta(hours=2))[1], "expired")

    def test_webhook(self):
        cred = self.backend.issue(cap=20, currency="USD", merchant="amazon.com", lifetime_hours=1,
                                  single_use=True, now=NOW)
        event = {"type": "issuing_authorization.request", "data": {"object": {
            "card": self.fake.cards[cred.id], "merchant_data": {"name": "AMAZON.COM"},
            "pending_request": {"amount": 1500}}}}
        payload = json.dumps(event).encode()
        status, headers, body = self.backend.handle_webhook(payload, signed(payload, "whsec_x"), "whsec_x", NOW)
        self.assertEqual((status, headers["Stripe-Version"], body), (200, STRIPE_VERSION, {"approved": True}))
        self.assertEqual(self.backend.handle_webhook(payload, signed(payload, "whsec_other"), "whsec_x")[0], 400)
        # single-use card is cancelled once the approved authorization is created
        created = json.dumps({"type": "issuing_authorization.created", "data": {"object": {
            "approved": True, "card": self.fake.cards[cred.id]}}}).encode()
        self.backend.handle_webhook(created, signed(created, "whsec_x"), "whsec_x")
        self.assertEqual(self.fake.cards[cred.id]["status"], "canceled")

    def test_signature_age(self):
        p = b"{}"
        self.assertTrue(verify_signature(p, signed(p, "s", 1000), "s", now=1100))
        self.assertFalse(verify_signature(p, signed(p, "s", 1000), "s", now=2000))


class TestAp2(unittest.TestCase):
    def cart(self, items, total, merchant="Amazon", expiry="2026-11-01T13:00:00Z"):
        return {"contents": {"id": "cart_1", "merchant_name": merchant, "cart_expiry": expiry,
                             "user_cart_confirmation_required": False,
                             "payment_request": {"details": {
                                 "display_items": [{"label": l, "amount": {"currency": "USD", "value": v}}
                                                   for l, v in items],
                                 "total": {"label": "Total", "amount": {"currency": "USD", "value": total}}}}}}

    def test_intent_mandate_is_attested(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req())
        m = gw.ap2_intent_mandate(d.request_id)
        self.assertEqual(m["intent_mandate"]["merchants"], ["amazon.com"])
        self.assertEqual(m["earnest"]["max_amount"]["value"], 20)
        secret = gw.ledger.secret("ap2")
        body = {"intent_mandate": m["intent_mandate"], "earnest": m["earnest"]}
        self.assertTrue(verify_attestation(body, m["earnest_attestation"], secret))
        body["earnest"]["max_amount"]["value"] = 999
        self.assertFalse(verify_attestation(body, m["earnest_attestation"], secret))

    def test_cart_checks(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req())
        ok = self.cart([("Amazon Basics AA Batteries 24 Pack", 18.5), ("Tax", 1.5)], 20)
        self.assertEqual(gw.check_ap2_cart(d.request_id, ok)["reason"], "cart_ok")
        self.assertEqual(gw.check_ap2_cart(d.request_id, self.cart([("AA batteries", 30)], 30))["reason"], "over_cap")
        self.assertEqual(gw.check_ap2_cart(d.request_id, self.cart([("AA batteries", 9)], 9, "Walmart"))["reason"],
                         "wrong_merchant")
        late = self.cart([("AA batteries", 9)], 9, expiry="2026-11-01T11:00:00Z")
        self.assertEqual(gw.check_ap2_cart(d.request_id, late)["reason"], "cart_expired")

    def test_cart_without_claimed_item_is_a_violation(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req())
        r = gw.check_ap2_cart(d.request_id, self.cart([("Gift card", 20)], 20))
        self.assertEqual(r["verification"]["status"], "mismatch")
        self.assertEqual(gw.limits(USER, "a1", "shopping")["score"], 30 + 2 - 10)
        gw.check_ap2_cart(d.request_id, self.cart([("Gift card", 20)], 20))  # not penalized twice
        self.assertEqual(gw.limits(USER, "a1", "shopping")["score"], 30 + 2 - 10)

    def test_only_approved_requests(self):
        gw, _ = make_gateway()
        d = gw.request_spend(req(amount=400))
        with self.assertRaises(ValidationError):
            gw.ap2_intent_mandate(d.request_id)


class TestApprovals(unittest.TestCase):
    def test_tokens(self):
        t = make_token(b"k", "req_1", "approve", now=1000)
        self.assertEqual(read_token(b"k", t, now=1001), ("req_1", "approve"))
        for bad, now in [(t, 1000 + 25 * 3600), (t[:-2] + "xx", 1001)]:
            with self.assertRaises(ValueError):
                read_token(b"k", bad, now=now)
        with self.assertRaises(ValueError):
            read_token(b"other", t, now=1001)

    def test_escalation_notifies_with_working_links(self):
        notes = MemoryNotifier()
        gw, _ = make_gateway(notifier=notes, approval_url="https://pay.example")
        d = gw.request_spend(req(amount=400))
        self.assertEqual(d.status, "escalated")
        m = notes.sent[0]
        self.assertTrue(m.approve_url.startswith("https://pay.example/a/"))
        self.assertIn("above_tier_cap", m.reasons)
        self.assertEqual(gw.resolve_token(m.approve_url.rsplit("/", 1)[1]).status, "approved")
        with self.assertRaises(ValidationError):
            gw.resolve_token(m.deny_url.rsplit("/", 1)[1])  # already resolved

    def test_no_notification_for_auto_decisions(self):
        notes = MemoryNotifier()
        gw, _ = make_gateway(notifier=notes)
        gw.request_spend(req(amount=5, merchant="amazon.com"))
        self.assertEqual(notes.sent, [])

    def test_channel_payloads(self):
        notes = MemoryNotifier()
        gw, _ = make_gateway(notifier=notes)
        gw.request_spend(req(amount=400))
        m = notes.sent[0]
        self.assertEqual(slack_payload(m)["blocks"][2]["elements"][0]["url"], m.approve_url)
        self.assertEqual(lark_payload(m)["msg_type"], "interactive")
        body, headers = ntfy_request(m)
        self.assertIn(m.deny_url, headers["Actions"])
        posts = []
        ChannelNotifier("slack", "https://hooks.example/x", post=lambda u, b, h: posts.append((u, json.loads(b))))\
            .notify(m)
        self.assertEqual(posts[0][1]["text"], m.title)

    def test_env_config_and_failing_channels(self):
        n = notifier_from_env({"EARNEST_NOTIFY": "slack:https://a, ntfy:https://ntfy.sh/t"})
        self.assertEqual([c.kind for c in n.notifiers], ["slack", "ntfy"])
        self.assertIsNone(notifier_from_env({}))
        with self.assertRaises(ValueError):
            notifier_from_env({"EARNEST_NOTIFY": "pager:https://x"})

        def boom(*a):
            raise OSError("down")
        multi = MultiNotifier([ChannelNotifier("slack", "https://a", post=boom)])
        gw, _ = make_gateway(notifier=multi)
        self.assertEqual(gw.request_spend(req(amount=400)).status, "escalated")
        self.assertIn("down", multi.errors[0])


class TestServer(unittest.TestCase):
    def test_get_confirms_post_acts(self):
        notes = MemoryNotifier()
        gw, _ = make_gateway(notifier=notes)
        gw.clock = lambda: datetime.now(timezone.utc)  # tokens are checked against real time here
        d = gw.request_spend(req(amount=400))
        server = serve(gw, port=0)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            path = "/a/" + notes.sent[0].approve_url.rsplit("/", 1)[1]
            url = f"http://127.0.0.1:{port}{path}"
            page = urllib.request.urlopen(url).read().decode()
            self.assertIn("Approve this purchase?", page)
            self.assertEqual(gw.pending(USER)[0]["request_id"], d.request_id)  # GET changed nothing
            done = urllib.request.urlopen(urllib.request.Request(url, data=b"", method="POST")).read().decode()
            self.assertIn("Approved", done)
            self.assertEqual(gw.pending(USER), [])
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url)
            self.assertEqual(e.exception.code, 410)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
