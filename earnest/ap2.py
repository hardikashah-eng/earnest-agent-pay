"""AP2 (Agent Payments Protocol) interop.

AP2 proves to a merchant that a human authorized a purchase, using signed mandates:
an Intent mandate (what the user wants bought, within what limits), a Cart mandate (the exact
cart the merchant offers) and a Payment mandate. earnest decides *whether* an agent may
spend; AP2 carries that decision to merchants. So earnest:

- exports an approved request as an AP2-shaped **Intent mandate**, with an earnest
  attestation (HMAC over the canonical JSON) saying which policy decision it came from, and
- checks a merchant's **Cart mandate** against the approved request before paying: same
  merchant, total within the approved cap, cart not expired, and the claimed item in the
  cart (spec section 3d).

Field names follow the AP2 reference types (IntentMandate, CartMandate / CartContents,
W3C PaymentRequest). The user's own signature on a mandate (AP2 `user_authorization`) comes
from the user's device or wallet and is out of scope here; the earnest attestation travels
alongside it. Pure functions plus HMAC.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime

from .verify import LineItem, check_cart


def canonical(obj: dict) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def attest(obj: dict, secret: bytes, key_id: str = "earnest-local") -> dict:
    return {"alg": "HS256", "kid": key_id,
            "sig": hmac.new(secret, canonical(obj), hashlib.sha256).hexdigest()}


def verify_attestation(obj: dict, attestation: dict, secret: bytes) -> bool:
    if attestation.get("alg") != "HS256":
        return False
    expected = hmac.new(secret, canonical(obj), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, attestation.get("sig", ""))


def intent_mandate(*, description: str, merchant: str, amount: float, currency: str,
                   expires_at: datetime, request_id: str, item: str | None = None,
                   needs_cart_confirmation: bool = False, requires_refundability: bool = False,
                   secret: bytes) -> dict:
    """An AP2 Intent mandate for one approved earnest request."""
    mandate = {
        "user_cart_confirmation_required": needs_cart_confirmation,
        "natural_language_description": description + (f": {item}" if item else ""),
        "merchants": [merchant],
        "skus": None,
        "requires_refundability": requires_refundability,
        "intent_expiry": expires_at.isoformat(),
    }
    limits = {"request_id": request_id, "max_amount": {"currency": currency, "value": round(amount, 2)},
              "item": item}
    return {"intent_mandate": mandate, "earnest": limits,
            "earnest_attestation": attest({"intent_mandate": mandate, "earnest": limits}, secret)}


def cart_line_items(cart_mandate: dict) -> list[LineItem]:
    details = cart_mandate["contents"]["payment_request"]["details"]
    return [LineItem(d["label"], float(d["amount"]["value"])) for d in details.get("display_items", [])]


def check_cart_mandate(cart_mandate: dict, *, merchant_lock: str, cap: float, currency: str,
                       item_lock: str | None, now: datetime) -> tuple[bool, str]:
    """Can the approved request pay this AP2 cart?"""
    from .merchants import MerchantRegistry
    reg = MerchantRegistry.default()
    contents = cart_mandate.get("contents") or {}
    details = (contents.get("payment_request") or {}).get("details") or {}
    total = (details.get("total") or {}).get("amount") or {}
    expiry = contents.get("cart_expiry")
    if expiry and now > datetime.fromisoformat(expiry.replace("Z", "+00:00")):
        return False, "cart_expired"
    if not (reg.canonical(contents.get("merchant_name", "")) & reg.canonical(merchant_lock)):
        return False, "wrong_merchant"
    if str(total.get("currency", currency)).upper() != currency.upper():
        return False, "wrong_currency"
    if float(total.get("value", 0)) > cap + 1e-9:
        return False, "over_cap"
    if item_lock:
        ok, reason = check_cart(item_lock, cart_line_items(cart_mandate))
        if not ok:
            return False, reason
    return True, "cart_ok"
