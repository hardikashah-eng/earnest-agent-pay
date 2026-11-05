"""Mock payment backend: simulated scoped, expiring virtual cards. No real money."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from ..models import Credential


class MockBackend:
    name = "mock"

    def __init__(self):
        self.cards: dict[str, Credential] = {}
        self.spent: dict[str, float] = {}

    def issue(self, *, cap: float, currency: str, merchant: str, lifetime_hours: float,
              single_use: bool, now: datetime) -> Credential:
        cred = Credential(
            id="card_" + uuid.uuid4().hex[:12], cap=round(cap, 2), currency=currency,
            merchant_lock=merchant, expires_at=now + timedelta(hours=lifetime_hours),
            single_use=single_use, backend=self.name)
        self.cards[cred.id] = cred
        self.spent[cred.id] = 0.0
        return cred

    def authorize(self, credential_id: str, amount: float, merchant: str, now: datetime) -> tuple[bool, str]:
        """What a card network would check when the merchant charges the card."""
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
        self.spent[cred.id] += amount
        return True, "approved"
