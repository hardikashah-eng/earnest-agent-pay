"""Stripe Issuing backend (test mode): real virtual cards, enforced by real-time authorization.

How earnest maps onto Stripe Issuing:
- `issue()` creates a virtual card for the user's cardholder with an all-time spending limit
  equal to the approved amount. Earnest's own limits (merchant lock, expiry, single use,
  item lock) go in the card's metadata.
- Stripe sends an `issuing_authorization.request` webhook for every charge and waits up to
  2 seconds for `{"approved": true|false}`. `handle_authorization_request` makes that call
  from the card metadata, the same checks the mock backend runs.
- Single-use cards are cancelled after their first approved charge.

Safety: this backend refuses live keys (sk_live_ / rk_live_). earnest v0.x does not move
real money; test mode only. No dependency on the stripe package: requests go through a
`transport` callable so the backend is testable without network or keys.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..models import Credential
from ..verify import LineItem, check_cart

API = "https://api.stripe.com"
STRIPE_VERSION = "2025-03-31.basil"
SIGNATURE_TOLERANCE_S = 300

Transport = Callable[[str, str, dict], dict]   # (method, path, form params) -> JSON response


class StripeConfigError(RuntimeError):
    pass


def _flatten(params: dict, prefix: str = "") -> list[tuple[str, str]]:
    """Stripe's form encoding: {"a": {"b": [{"c": 1}]}} -> a[b][0][c]=1."""
    out: list[tuple[str, str]] = []
    for k, v in params.items():
        key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            out += _flatten(v, key)
        elif isinstance(v, list):
            for i, item in enumerate(v):
                out += _flatten(item, f"{key}[{i}]") if isinstance(item, dict) else [(f"{key}[{i}]", str(item))]
        elif isinstance(v, bool):
            out.append((key, "true" if v else "false"))
        elif v is not None:
            out.append((key, str(v)))
    return out


def http_transport(api_key: str) -> Transport:
    def call(method: str, path: str, params: dict) -> dict:
        data = urllib.parse.urlencode(_flatten(params)).encode() if params else None
        req = urllib.request.Request(API + path, data=data, method=method, headers={
            "Authorization": f"Bearer {api_key}", "Stripe-Version": STRIPE_VERSION})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    return call


def verify_signature(payload: bytes, header: str, secret: str, now: float | None = None) -> bool:
    """Check a Stripe-Signature header ("t=...,v1=...") against the webhook signing secret."""
    pairs = [p.split("=", 1) for p in header.split(",") if "=" in p]
    parts = dict(pairs)
    sigs = [v for k, v in pairs if k == "v1"]
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now or time.time()) - ts) > SIGNATURE_TOLERANCE_S:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, s) for s in sigs)


def merchant_matches(lock: str, merchant_data: dict) -> bool:
    """Card networks report merchant names like 'AMAZON.COM*2K4L1' or 'AMZN Mktp US'. Match
    the lock (a domain or name) against the descriptor's name and URL."""
    from ..insights.statement import clean_merchant
    from ..merchants import MerchantRegistry
    reg = MerchantRegistry.default()
    want = reg.canonical(lock)
    for field in ("url", "name"):
        v = (merchant_data or {}).get(field)
        if v and want & (reg.canonical(v) | reg.canonical(clean_merchant(v)[0])):
            return True
    return False


class StripeIssuingBackend:
    name = "stripe_issuing"

    def __init__(self, cardholder_id: str, api_key: str | None = None, transport: Transport | None = None):
        key = api_key or os.environ.get("STRIPE_API_KEY", "")
        if transport is None:
            if not key:
                raise StripeConfigError("set STRIPE_API_KEY (a test key, sk_test_...)")
            transport = http_transport(key)
        if key.startswith(("sk_live_", "rk_live_")):
            raise StripeConfigError("live Stripe keys are refused: earnest only runs in test mode")
        self.cardholder_id = cardholder_id
        self.call = transport
        self.last_decision: str | None = None

    def issue(self, *, cap: float, currency: str, merchant: str, lifetime_hours: float,
              single_use: bool, now: datetime, item: str | None = None) -> Credential:
        expires = now + timedelta(hours=lifetime_hours)
        metadata = {"earnest_merchant_lock": merchant, "earnest_expires_at": expires.isoformat(),
                    "earnest_single_use": "1" if single_use else "0", "earnest_cap": f"{cap:.2f}"}
        if item:
            metadata["earnest_item_lock"] = item[:500]
        card = self.call("POST", "/v1/issuing/cards", {
            "cardholder": self.cardholder_id, "currency": currency.lower(), "type": "virtual",
            "status": "active", "metadata": metadata,
            "spending_controls": {"spending_limits": [
                {"amount": int(round(cap * 100)), "interval": "all_time"}]},
        })
        return Credential(card["id"], round(cap, 2), currency, merchant, expires, single_use, self.name, item)

    def cancel(self, card_id: str) -> None:
        self.call("POST", f"/v1/issuing/cards/{card_id}", {"status": "canceled"})

    def decide(self, authorization: dict, now: datetime | None = None,
               line_items: list[LineItem] | None = None) -> tuple[bool, str]:
        """The approve/decline decision for an issuing_authorization.request."""
        now = now or datetime.now(timezone.utc)
        card = authorization.get("card") or {}
        meta = card.get("metadata") or {}
        if "earnest_merchant_lock" not in meta:
            return False, "not_an_earnest_card"
        if now > datetime.fromisoformat(meta["earnest_expires_at"]):
            return False, "expired"
        if not merchant_matches(meta["earnest_merchant_lock"], authorization.get("merchant_data") or {}):
            return False, "wrong_merchant"
        pending = authorization.get("pending_request") or {}
        amount = (pending.get("amount") or 0) / 100
        if amount > float(meta.get("earnest_cap", "0")) + 1e-9:
            return False, "over_cap"
        if meta.get("earnest_item_lock") and line_items is not None:
            ok, reason = check_cart(meta["earnest_item_lock"], line_items)
            if not ok:
                return False, reason
        return True, "approved"

    def handle_webhook(self, payload: bytes, signature: str, secret: str,
                       now: datetime | None = None) -> tuple[int, dict, dict]:
        """Returns (status, headers, body) to send back to Stripe."""
        if not verify_signature(payload, signature, secret):
            return 400, {}, {"error": "bad signature"}
        event = json.loads(payload)
        headers = {"Stripe-Version": STRIPE_VERSION, "Content-Type": "application/json"}
        if event.get("type") != "issuing_authorization.request":
            if event.get("type") == "issuing_authorization.created":
                auth = event["data"]["object"]
                meta = (auth.get("card") or {}).get("metadata") or {}
                if auth.get("approved") and meta.get("earnest_single_use") == "1":
                    self.cancel(auth["card"]["id"])
            return 200, headers, {"received": True}
        ok, reason = self.decide(event["data"]["object"], now)
        self.last_decision = reason  # for logs; Stripe's response body only takes the decision
        return 200, headers, {"approved": ok}

    # Same signature as MockBackend.authorize, for code that charges cards directly in tests.
    def authorize(self, credential_id: str, amount: float, merchant: str, now: datetime,
                  line_items: list[LineItem] | None = None) -> tuple[bool, str]:
        card = self.call("GET", f"/v1/issuing/cards/{credential_id}", {})
        return self.decide({"card": card, "merchant_data": {"name": merchant},
                            "pending_request": {"amount": int(round(amount * 100))}}, now, line_items)
