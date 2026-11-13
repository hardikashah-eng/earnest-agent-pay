"""Mock payment backend: simulated scoped, expiring virtual cards. No real money."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from ..models import Credential
from ..verify import LineItem, check_cart


class MockBackend:
    name = "mock"

    def __init__(self):
        self.cards: dict[str, Credential] = {}
        self.spent: dict[str, float] = {}

    def issue(self, *, cap: float, currency: str, merchant: str, lifetime_hours: float,
              single_use: bool, now: datetime, item: str | None = None,
              product_id: str | None = None, account: str | None = None) -> Credential:
        cred = Credential(
            id="card_" + uuid.uuid4().hex[:12], cap=round(cap, 2), currency=currency,
            merchant_lock=merchant, expires_at=now + timedelta(hours=lifetime_hours),
            single_use=single_use, backend=self.name, item_lock=item, product_lock=product_id,
            account=account)
        self.cards[cred.id] = cred
        self.spent[cred.id] = 0.0
        return cred

    def authorize(self, credential_id: str, amount: float, merchant: str, now: datetime,
                  line_items: list[LineItem] | None = None) -> tuple[bool, str]:
        """What a card network would check when the merchant charges the card. When the
        merchant sends cart line items, an item-locked card only pays for the claimed item."""
        cred = self.cards.get(credential_id)
        if cred is None:
            return False, "unknown_card"
        if now > cred.expires_at:
            return False, "expired"
        if merchant != cred.merchant_lock:
            return False, "wrong_merchant"
        if cred.single_use and self.spent[cred.id] > 0:
            return False, "already_used"
        if self.spent[cred.id] + amount > cred.cap + 1e-9:
            return False, "over_cap"
        if cred.item_lock and line_items is not None:
            ok, reason = check_cart(cred.item_lock, line_items, cred.product_lock)
            if not ok:
                return False, reason
        self.spent[cred.id] += amount
        return True, "approved"
