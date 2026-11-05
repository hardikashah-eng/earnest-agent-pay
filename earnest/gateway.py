"""The gateway: ties together ledger, rules, scoring and a payment backend."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Callable

from .backends.mock import MockBackend
from .ledger import Ledger
from .models import Decision, Mandate, Outcome, Profile, SpendRequest, ValidationError
from .rules import decide
from .scoring import ASKED_FIRST, PURCHASE_APPROVED, VIOLATION, outcome_events
from .tiers import DEFAULT_TIERS, Tier, tier_for


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Gateway:
    def __init__(self, ledger: Ledger | None = None, backend=None,
                 clock: Callable[[], datetime] = utcnow, tiers: tuple[Tier, ...] = DEFAULT_TIERS):
        self.ledger = ledger or Ledger()
        self.backend = backend or MockBackend()
        self.clock = clock
        self.tiers = tiers

    # --- setup (user side) ---
    def set_profile(self, profile: Profile | dict) -> Profile:
        p = profile if isinstance(profile, Profile) else Profile.from_dict(profile)
        self.ledger.put_profile(p)
        return p

    def add_mandate(self, mandate: Mandate | dict) -> Mandate:
        m = mandate if isinstance(mandate, Mandate) else Mandate.from_dict(mandate)
        self.ledger.put_mandate(m)
        return m

    def profile(self, user_id: str) -> Profile:
        return self.ledger.get_profile(user_id) or Profile(user_id=user_id)

    # --- limits ---
    def tier(self, user_id: str, agent_id: str, task_type: str) -> tuple[float, Tier]:
        score = self.ledger.score(user_id, agent_id, task_type, self.clock())
        return score, tier_for(score, self.tiers)

    def limits(self, user_id: str, agent_id: str, task_type: str) -> dict:
        score, t = self.tier(user_id, agent_id, task_type)
        p = self.profile(user_id)
        return {
            "user_id": user_id, "agent_id": agent_id, "task_type": task_type,
            "score": round(score, 2), "tier": t.name, "cap": t.cap,
            "approval_over": round(t.effective_approval_over(p.autonomy), 2),
            "credential_lifetime_hours": t.lifetime_hours,
            "single_use": t.single_use or p.privacy >= 0.8,
            "new_merchants_ok": t.approve_new_merchants or p.novelty >= 0.8,
        }

    # --- agent side ---
    def request_spend(self, request: SpendRequest | dict) -> Decision:
        req = request if isinstance(request, SpendRequest) else SpendRequest.from_dict(request)
        now = self.clock()
        request_id = "req_" + uuid.uuid4().hex[:12]

        mandate = self.ledger.get_mandate(req.mandate_id)
        if mandate is None:
            # Not scored: we don't know whose trust this would touch.
            decision = Decision(request_id, "denied", ["unknown_mandate"], "n/a")
            self.ledger.add_request(request_id, "unknown", None, req, "denied", decision.to_dict(), now)
            return decision

        profile = self.profile(mandate.user_id)
        score, t = self.tier(mandate.user_id, req.agent_id, mandate.task_type)
        verdict = decide(
            req, mandate, profile, t,
            spent=self.ledger.spent_on_mandate(mandate.id),
            is_new_merchant=not self.ledger.known_merchant(mandate.user_id, req.merchant),
            now=now,
        )
        decision = Decision(request_id, verdict.status, verdict.reasons, t.name)
        if verdict.status == "approved":
            decision.credential = self._issue(req, mandate, profile, t, now)

        self.ledger.add_request(request_id, mandate.user_id, mandate.task_type, req,
                                decision.status, decision.to_dict(), now)

        key = (mandate.user_id, req.agent_id, mandate.task_type)
        if verdict.violation:
            self.ledger.add_points(*key, "violation", VIOLATION, request_id, now)
        if verdict.asked_first_rewarded:
            self.ledger.add_points(*key, "asked_first", ASKED_FIRST, request_id, now)
        if verdict.status == "approved":
            self.ledger.add_points(*key, "purchase_approved", PURCHASE_APPROVED, request_id, now)
        return decision

    # --- user side ---
    def pending(self, user_id: str) -> list[dict]:
        return [{"request_id": r["id"], "agent_id": r["agent_id"], "mandate_id": r["mandate_id"],
                 "merchant": r["merchant"], "amount": r["amount"],
                 "reasons": json.loads(r["decision"])["reasons"],
                 "reason_given": json.loads(r["data"])["reason"]}
                for r in self.ledger.pending(user_id)]

    def resolve(self, request_id: str, approve: bool) -> Decision:
        """The user approves or denies an escalated request."""
        row = self.ledger.get_request(request_id)
        if row is None:
            raise ValidationError(f"unknown request: {request_id}")
        if row["status"] != "escalated":
            raise ValidationError(f"request {request_id} is {row['status']}, not awaiting approval")
        now = self.clock()
        req = SpendRequest.from_dict(json.loads(row["data"]))
        mandate = self.ledger.get_mandate(req.mandate_id)
        _, t = self.tier(row["user_id"], req.agent_id, mandate.task_type)
        prior = json.loads(row["decision"])["reasons"]

        if not approve:
            decision = Decision(request_id, "denied", [*prior, "user_denied"], t.name)
            self.ledger.update_request(request_id, "denied", decision.to_dict())
            return decision

        profile = self.profile(mandate.user_id)
        decision = Decision(request_id, "approved", [*prior, "user_approved"], t.name,
                            self._issue(req, mandate, profile, t, now))
        self.ledger.update_request(request_id, "approved", decision.to_dict())
        self.ledger.add_points(mandate.user_id, req.agent_id, mandate.task_type,
                               "purchase_approved", PURCHASE_APPROVED, request_id, now)
        return decision

    def report_outcome(self, outcome: Outcome | dict) -> list[dict]:
        """The user's verdict on a completed purchase. Returns the score events it produced."""
        o = outcome if isinstance(outcome, Outcome) else Outcome.from_dict(outcome)
        row = self.ledger.get_request(o.request_id)
        if row is None:
            raise ValidationError(f"unknown request: {o.request_id}")
        if row["status"] != "approved":
            raise ValidationError(f"request {o.request_id} was not approved; nothing to report")
        if self.ledger.has_outcome(o.request_id):
            raise ValidationError(f"outcome already reported for {o.request_id}")
        now = self.clock()
        mandate = self.ledger.get_mandate(row["mandate_id"])
        profile = self.profile(row["user_id"])
        self.ledger.add_outcome(o, now)
        events = []
        for kind, pts in outcome_events(o, row["amount"], mandate, profile):
            self.ledger.add_points(row["user_id"], row["agent_id"], mandate.task_type, kind, pts, o.request_id, now)
            events.append({"kind": kind, "points": pts})
        return events

    # --- internals ---
    def _issue(self, req: SpendRequest, mandate: Mandate, profile: Profile, t: Tier, now: datetime):
        return self.backend.issue(
            cap=req.amount, currency=mandate.currency, merchant=req.merchant,
            lifetime_hours=t.lifetime_hours, single_use=t.single_use or profile.privacy >= 0.8, now=now)
