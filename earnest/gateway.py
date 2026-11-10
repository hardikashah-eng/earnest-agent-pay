"""The gateway: ties together ledger, rules, scoring and a payment backend."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from typing import Callable

from .backends.mock import MockBackend
from .confidence import score as confidence_score
from .history import Interest, PastPurchase, item_tokens, summarize
from .ledger import Ledger
from .merchants import MerchantRegistry
from .models import Decision, Mandate, Outcome, Profile, SpendRequest, ValidationError
from .rules import decide
from .scoring import ASKED_FIRST, PURCHASE_APPROVED, VIOLATION, outcome_events
from .tiers import DEFAULT_TIERS, Tier, tier_for


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Gateway:
    def __init__(self, ledger: Ledger | None = None, backend=None,
                 clock: Callable[[], datetime] = utcnow, tiers: tuple[Tier, ...] = DEFAULT_TIERS,
                 merchants: MerchantRegistry | None = None):
        self.ledger = ledger or Ledger()
        self.backend = backend or MockBackend()
        self.clock = clock
        self.tiers = tiers
        self.merchants = merchants or MerchantRegistry.default()

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

    # --- merchant trust & purchase history ---
    def trust_merchant(self, user_id: str, merchant: str, reason: str = "user") -> None:
        self.ledger.set_merchant_status(user_id, merchant, "trusted", reason, self.clock())

    def distrust_merchant(self, user_id: str, merchant: str, reason: str = "user") -> None:
        self.ledger.set_merchant_status(user_id, merchant, "untrusted", reason, self.clock())

    # --- the user's purchase context: orders, interests, memberships ---
    def import_orders(self, user_id: str, orders, source: str = "manual") -> tuple[int, int]:
        return self.ledger.add_orders(user_id, orders, source)

    def add_interest(self, user_id: str, item: str, merchant: str | None = None,
                     max_price: float | None = None, source: str = "user") -> int:
        return self.ledger.add_interest(user_id, item, merchant, max_price, source, self.clock())

    def add_membership(self, user_id: str, merchant: str, program: str = "member") -> None:
        self.ledger.add_membership(user_id, merchant, program, self.clock())

    def _past_purchases(self, user_id: str) -> list[PastPurchase]:
        past = []
        for p in self.ledger.purchases(user_id):
            past.append(PastPurchase(frozenset(self.merchants.canonical(p["merchant"])),
                                     p["amount"] / max(1, p["quantity"]),
                                     datetime.fromisoformat(p["at"]).date(), "earnest", p["item"], p["verdict"]))
        for o in self.ledger.orders(user_id):
            past.append(PastPurchase(frozenset(self.merchants.canonical(o["merchant"])), o["unit_price"],
                                     date.fromisoformat(o["date"]), "order", o["item"]))
        for t in self.ledger.statement_purchases(user_id):
            past.append(PastPurchase(frozenset(self.merchants.canonical(t["merchant"])), t["amount"],
                                     date.fromisoformat(t["date"]), "statement"))
        return past

    def _interests(self, user_id: str) -> list[Interest]:
        return [Interest(i["item"], frozenset(self.merchants.canonical(i["merchant"])) if i["merchant"] else frozenset(),
                         i["max_price"], i["source"]) for i in self.ledger.interests(user_id)]

    def _memberships_at(self, user_id: str, keys: set[str]) -> list[str]:
        return [m["program"] for m in self.ledger.memberships(user_id)
                if keys & self.merchants.canonical(m["merchant"])]

    def assess(self, user_id: str, merchant: str, *, item: str | None = None, amount: float = 0.0,
               quantity: int = 1, category: str | None = None, mandate: Mandate | None = None):
        """Everything known about a prospective purchase.
        Returns (MerchantCheck, History, is_known_merchant, confidence dict)."""
        check = self.merchants.check(merchant, self.ledger.merchant_overrides(user_id))
        keys = self.merchants.canonical(merchant)
        past = self._past_purchases(user_id)
        hist = summarize(keys, past, item, self.clock().date(), self._interests(user_id),
                         self._memberships_at(user_id, keys))
        known = bool(hist.memberships) or any(keys & p.merchant_keys for p in past)
        extra = []
        if category and self.merchants.high_risk_category(category):
            extra.append(f"high_risk_category:{category}")
        intent_match = bool(item and mandate and item_tokens(item) & item_tokens(mandate.intent))
        conf = confidence_score(
            check, hist, amount=amount, quantity=quantity, item=item, intent_match=intent_match,
            benchmark=mandate.benchmark_price if mandate else None,
            budget_max=mandate.budget_max if mandate else None,
            spent=self.ledger.spent_on_mandate(mandate.id) if mandate else 0.0,
            budget_min=mandate.budget_min if mandate else None, extra_signals=extra)
        return check, hist, known, conf

    def check_merchant(self, user_id: str, merchant: str, item: str | None = None, amount: float = 0.0,
                       category: str | None = None, mandate_id: str | None = None, quantity: int = 1) -> dict:
        """What an agent should know before buying: trust, history, and the confidence score."""
        mandate = self.ledger.get_mandate(mandate_id) if mandate_id else None
        if mandate is not None and mandate.user_id != user_id:
            mandate = None
        return self.assess(user_id, merchant, item=item, amount=amount, quantity=quantity,
                           category=category, mandate=mandate)[3]

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
        check, hist, bought_here, conf = self.assess(
            mandate.user_id, req.merchant, item=req.item, amount=req.amount, quantity=req.quantity,
            category=req.category, mandate=mandate)
        verdict = decide(
            req, mandate, profile, t,
            spent=self.ledger.spent_on_mandate(mandate.id),
            is_new_merchant=not bought_here,
            now=now,
            merchant=check, history=hist,
            high_risk_category=self.merchants.high_risk_category(req.category),
            confidence=conf,
        )
        decision = Decision(request_id, verdict.status, verdict.reasons, t.name, confidence=conf)
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
                 "reason_given": json.loads(r["data"])["reason"],
                 "item": json.loads(r["data"]).get("item"),
                 "confidence": json.loads(r["decision"]).get("confidence")}
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
            decision = Decision(request_id, "denied", [*prior, "user_denied"], t.name,
                                confidence=json.loads(row["decision"]).get("confidence"))
            self.ledger.update_request(request_id, "denied", decision.to_dict())
            return decision

        profile = self.profile(mandate.user_id)
        decision = Decision(request_id, "approved", [*prior, "user_approved"], t.name,
                            self._issue(req, mandate, profile, t, now),
                            json.loads(row["decision"]).get("confidence"))
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
