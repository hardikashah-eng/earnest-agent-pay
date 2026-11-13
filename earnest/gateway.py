"""The gateway: ties together ledger, rules, scoring and a payment backend."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import ap2
from .accounts import ALLOWANCE, BAD_EVENTS, Funding, effective_ceiling, pick_funding, raise_offer, usable_for
from .accounts import ceiling as tier_ceiling
from .approvals import ApprovalMessage, make_token, read_token
from .backends.mock import MockBackend
from .confidence import score as confidence_score
from .learning import FACTORS, Sample, learn
from .reorder import standing_checks
from .returns import window as return_window
from .market import MAX_AGE_DAYS, PriceQuote, market_signals, market_view
from .history import Interest, PastPurchase, item_tokens, summarize
from .ledger import Ledger
from .merchants import MerchantRegistry
from .models import Decision, Mandate, Outcome, Profile, SpendRequest, ValidationError
from .rules import decide
from .scoring import ASKED_FIRST, ITEM_VERIFIED, PURCHASE_APPROVED, VIOLATION, outcome_events
from .verify import MISMATCH_SUSPENDS_DAYS, LineItem, OrderLine, reconcile
from .tiers import DEFAULT_TIERS, Tier, tier_for


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Gateway:
    def __init__(self, ledger: Ledger | None = None, backend=None,
                 clock: Callable[[], datetime] = utcnow, tiers: tuple[Tier, ...] = DEFAULT_TIERS,
                 merchants: MerchantRegistry | None = None, notifier=None,
                 approval_url: str | None = None):
        self.ledger = ledger or Ledger()
        self.backend = backend or MockBackend()
        self.clock = clock
        self.tiers = tiers
        self.merchants = merchants or MerchantRegistry.default()
        self.notifier = notifier
        self.approval_url = (approval_url or "http://localhost:8787").rstrip("/")

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

    def _past_purchases(self, user_id: str, before: date | None = None) -> list[PastPurchase]:
        """Everything the user bought, from all sources. With `before`, only what was bought
        before that day (shadow replays judge each purchase on what was known then)."""
        past = self._all_past_purchases(user_id)
        return past if before is None else [p for p in past if p.on < before]

    def _all_past_purchases(self, user_id: str) -> list[PastPurchase]:
        past = []
        for p in self.ledger.purchases(user_id):
            # an item claim that didn't match the real order says nothing about the item
            item = None if p["verification"] == "mismatch" else p["item"]
            past.append(PastPurchase(frozenset(self.merchants.canonical(p["merchant"])),
                                     p["amount"] / max(1, p["quantity"]),
                                     datetime.fromisoformat(p["at"]).date(), "earnest", item, p["verdict"],
                                     p["product_id"] if item else None))
        for o in self.ledger.orders(user_id):
            past.append(PastPurchase(frozenset(self.merchants.canonical(o["merchant"])), o["unit_price"],
                                     date.fromisoformat(o["date"]), "order", o["item"], None, o["product_id"]))
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
               quantity: int = 1, category: str | None = None, mandate: Mandate | None = None,
               product_id: str | None = None, as_of: date | None = None, spent: float | None = None):
        """Everything known about a prospective purchase.
        Returns (MerchantCheck, History, is_known_merchant, confidence dict).
        With `as_of`, judge it as of that day: only purchases made before it count."""
        check = self.merchants.check(merchant, self.ledger.merchant_overrides(user_id))
        keys = self.merchants.canonical(merchant)
        self._apply_reputation(check, merchant)
        past = self._past_purchases(user_id, as_of)
        hist = summarize(keys, past, item, as_of or self.clock().date(), self._interests(user_id),
                         self._memberships_at(user_id, keys), product_id)
        known = bool(hist.memberships) or any(keys & p.merchant_keys for p in past)
        extra = []
        if category and self.merchants.high_risk_category(category):
            extra.append(f"high_risk_category:{category}")
        rw = return_window(check.merchant_id, category, hist.memberships)
        extra.append(f"returnable:{'open_ended' if rw.open_ended else rw.days}" if rw.known
                     else "return_policy_unknown")
        intent_match = bool(item and mandate and item_tokens(item) & item_tokens(mandate.intent))
        today = as_of or self.clock().date()
        quotes = [PriceQuote(q["merchant"], q["item"], q["price"], date.fromisoformat(q["date"]), q["source"],
                             q["product_id"], q["url"])
                  for q in self.ledger.price_quotes((today - timedelta(days=MAX_AGE_DAYS)).isoformat())]
        market = market_view(quotes, item, product_id, today,
                             lambda m: self.merchants.check(m).status == "verified")
        adjust = market_signals(amount / max(1, quantity), keys, market, self.merchants.canonical)
        days_left = (mandate.deadline - today).days if mandate and mandate.deadline else None
        conf = confidence_score(
            check, hist, amount=amount, quantity=quantity, item=item, intent_match=intent_match,
            benchmark=mandate.benchmark_price if mandate else None,
            budget_max=mandate.budget_max if mandate else None,
            spent=(spent if spent is not None else self.ledger.spent_on_mandate(mandate.id)) if mandate else 0.0,
            budget_min=mandate.budget_min if mandate else None, extra_signals=extra,
            weights=self.ledger.confidence_weights(user_id), market=market, market_adjust=adjust,
            days_to_deadline=days_left)
        conf["returns"] = {"window": rw.label(), "note": rw.note} if rw.known else {"window": rw.label()}
        return check, hist, known, conf

    def check_merchant(self, user_id: str, merchant: str, item: str | None = None, amount: float = 0.0,
                       category: str | None = None, mandate_id: str | None = None, quantity: int = 1,
                       product_id: str | None = None) -> dict:
        """What an agent should know before buying: trust, history, and the confidence score."""
        mandate = self.ledger.get_mandate(mandate_id) if mandate_id else None
        if mandate is not None and mandate.user_id != user_id:
            mandate = None
        from .products import normalize
        return self.assess(user_id, merchant, item=item, amount=amount, quantity=quantity,
                           category=category, mandate=mandate, product_id=normalize(product_id))[3]

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
        shadow_card = self._shadow_card(mandate.user_id, req.agent_id)
        account = None if shadow_card else self.ledger.get_account(mandate.user_id, req.agent_id)
        if account is not None and account["status"] != "active":
            # the user stopped this agent's spending; not scored, like an unknown mandate
            decision = Decision(request_id, "denied", [f"account_{account['status']}"], t.name)
            self.ledger.add_request(request_id, mandate.user_id, mandate.task_type, req, "denied",
                                    decision.to_dict(), now)
            return decision
        check, hist, bought_here, conf = self.assess(
            mandate.user_id, req.merchant, item=req.item, amount=req.amount, quantity=req.quantity,
            category=req.category, mandate=mandate, product_id=req.product_id)
        if self._routine_suspended(mandate.user_id, req.agent_id, mandate.task_type, now):
            conf["signals"].append("routine_suspended")
        extra_deny, extra_esc = self._household_checks(mandate.user_id, req.amount, now)
        spent = self.ledger.spent_on_mandate(mandate.id)
        if mandate.standing:
            # a standing mandate's budget is per cycle, not lifetime
            cycle = timedelta(days=max(1, mandate.standing["min_interval_days"]))
            spent = self.ledger.mandate_spent_since(mandate.id, (now - cycle).isoformat())
            last = self._last_same_item(mandate.user_id, req.item, req.product_id)
            standing_deny = standing_checks(mandate.standing, req.item, req.product_id,
                                            req.amount / max(1, req.quantity), last, now.date())
            extra_deny += standing_deny
            if not standing_deny:
                conf["signals"].append("standing_mandate")
        if mandate.requires_returnable and "return_policy_unknown" in conf["signals"]:
            extra_esc.append("return_policy_unknown")
        evaluation = self._evaluate_request(mandate, req, conf)
        if evaluation is not None and evaluation.get("gate"):
            extra_esc += evaluation["gate"]
        funding = None
        if account is not None:
            self._sync_ceiling(mandate.user_id, req.agent_id, now)
            sources = self._funding(mandate.user_id, req.agent_id)
            keys = self.merchants.canonical(req.merchant)
            funding = pick_funding(sources, keys, req.amount)
            if funding is None:
                extra_esc.append(f"account:insufficient_balance:{usable_for(sources, keys):.2f}")
        verdict = decide(
            req, mandate, profile, t,
            spent=spent,
            is_new_merchant=not bought_here,
            now=now,
            merchant=check, history=hist,
            high_risk_category=self.merchants.high_risk_category(req.category),
            confidence=conf, extra_deny=extra_deny, extra_escalate=extra_esc,
        )
        summary = self._eval_summary(evaluation)
        if shadow_card is not None:
            # read-only: the real decision, but no credential, no money, no points, no notification
            decision = Decision(request_id, "shadow", ["shadow_mode", *verdict.reasons], t.name, confidence=conf,
                                shadow={"would_be": verdict.status, "card": shadow_card,
                                        "violation": verdict.violation}, evaluation=summary)
            self.ledger.add_request(request_id, mandate.user_id, mandate.task_type, req, "shadow",
                                    decision.to_dict(), now)
            self._store_eval(request_id, evaluation, now)
            return decision

        decision = Decision(request_id, verdict.status, verdict.reasons, t.name, confidence=conf,
                            evaluation=summary)
        if verdict.status == "approved":
            self._pay(decision, req, mandate, profile, t, now, request_id, funding)

        self.ledger.add_request(request_id, mandate.user_id, mandate.task_type, req,
                                decision.status, decision.to_dict(), now)
        self._store_eval(request_id, evaluation, now)

        if decision.status == "escalated" and self.notifier is not None:
            self._notify(request_id, req, decision)

        key = (mandate.user_id, req.agent_id, mandate.task_type)
        if verdict.violation:
            self.ledger.add_points(*key, "violation", VIOLATION, request_id, now)
            self._on_bad_event(mandate.user_id, req.agent_id, "violation", request_id, now)
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

    def resolve(self, request_id: str, approve: bool, approver: str | None = None) -> Decision:
        """Approve or deny an escalated request. In a household, `approver` must be an adult; a
        child's request needs an adult other than the child, and purchases over the household's
        second-approval threshold need two different adults."""
        row = self.ledger.get_request(request_id)
        if row is None:
            raise ValidationError(f"unknown request: {request_id}")
        if row["status"] != "escalated":
            raise ValidationError(f"request {request_id} is {row['status']}, not awaiting approval")
        now = self.clock()
        req = SpendRequest.from_dict(json.loads(row["data"]))
        mandate = self.ledger.get_mandate(req.mandate_id)
        _, t = self.tier(row["user_id"], req.agent_id, mandate.task_type)
        prior = [r for r in json.loads(row["decision"])["reasons"] if r != "awaiting_second_approval"]
        funding = self._funding_for_approval(row["user_id"], req) if approve else None
        approver = approver or row["user_id"]
        house = self.ledger.household_for(row["user_id"])
        if house is not None and house.member(row["user_id"]) is not None:
            if not house.can_approve(approver, row["user_id"]):
                raise ValidationError(f"{approver} can't approve purchases for this household "
                                      "(an adult member must approve)")

        if approve and house is not None and house.member(row["user_id"]) is not None:
            self.ledger.add_approval(request_id, approver, now)
            got = {a["approver"] for a in self.ledger.approvals(request_id)}
            if len(got) < house.approvals_needed(row["user_id"], req.amount):
                waiting = Decision(request_id, "escalated", [*prior, "awaiting_second_approval"], t.name,
                                   confidence=json.loads(row["decision"]).get("confidence"))
                self.ledger.update_request(request_id, "escalated", waiting.to_dict())
                return waiting
        elif approve:
            self.ledger.add_approval(request_id, approver, now)

        if not approve:
            decision = Decision(request_id, "denied", [*prior, "user_denied"], t.name,
                                confidence=json.loads(row["decision"]).get("confidence"))
            self.ledger.update_request(request_id, "denied", decision.to_dict())
            self._sync_gift_plan(request_id, decision)
            return decision

        profile = self.profile(mandate.user_id)
        decision = Decision(request_id, "approved", [*prior, "user_approved"], t.name,
                            confidence=json.loads(row["decision"]).get("confidence"),
                            evaluation=json.loads(row["decision"]).get("evaluation"))
        self._pay(decision, req, mandate, profile, t, now, request_id, funding)
        self.ledger.update_request(request_id, "approved", decision.to_dict())
        self.ledger.add_points(mandate.user_id, req.agent_id, mandate.task_type,
                               "purchase_approved", PURCHASE_APPROVED, request_id, now)
        self._sync_gift_plan(request_id, decision)
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
            self._on_bad_event(row["user_id"], row["agent_id"], kind, o.request_id, now)
        return events

    # --- internals ---
    def _issue(self, req: SpendRequest, mandate: Mandate, profile: Profile, t: Tier, now: datetime,
               request_id: str, account: str | None = None):
        extra = {"account": account} if account else {}
        cred = self.backend.issue(
            cap=req.amount, currency=mandate.currency, merchant=req.merchant,
            lifetime_hours=t.lifetime_hours, single_use=t.single_use or profile.privacy >= 0.8, now=now,
            item=req.item, product_id=req.product_id, **extra)
        self.ledger.link_credential(cred.id, request_id)
        return cred

    def _pay(self, decision: Decision, req: SpendRequest, mandate: Mandate, profile: Profile, t: Tier,
             now: datetime, request_id: str, funding: Funding | None) -> None:
        """Pay for an approved purchase: a scoped card, drawn from the agent's allowance when it
        has an account, or the agent's gift card / prepaid card for this store."""
        user = mandate.user_id
        if funding is None:
            decision.credential = self._issue(req, mandate, profile, t, now, request_id)
            return
        if funding.kind == ALLOWANCE:
            decision.credential = self._issue(req, mandate, profile, t, now, request_id,
                                              account=f"{user}:{req.agent_id}")
            self.ledger.add_entry(user, req.agent_id, funding.id, "hold", -req.amount, request_id,
                                  f"card {decision.credential.id} at {req.merchant}", now)
        else:
            self.ledger.add_entry(user, req.agent_id, funding.id, "spend", -req.amount, request_id,
                                  f"{req.merchant}" + (f": {req.item}" if req.item else ""), now)
        decision.payment = {"method": funding.kind, "funding_id": funding.id, "pay_with": funding.describe(),
                            "amount": round(req.amount, 2), "remaining": round(funding.balance - req.amount, 2)}
        if funding.kind == "gift_card":
            decision.payment["note"] = ("pay with the gift card balance on your store account; "
                                        "no card number is issued")
        elif funding.kind == "prepaid":
            decision.payment["note"] = "pay with the prepaid card; it isn't locked to this store"

    def _routine_suspended(self, user_id: str, agent_id: str, task_type: str, now: datetime) -> bool:
        last = self.ledger.last_event(user_id, agent_id, task_type, "item_mismatch")
        return last is not None and now - last < timedelta(days=MISMATCH_SUSPENDS_DAYS)

    # --- households (spec section 3g) ---
    def set_household(self, household_id: str, name: str, members: list[dict],
                      monthly_budget: float | None = None, second_approval_over: float | None = None):
        from .household import Household, Member
        for m in members:
            if m.get("role", "adult") not in ("adult", "child"):
                raise ValidationError("household member role must be adult or child")
        h = Household(household_id, name, [Member(m["user_id"], m.get("role", "adult"), m.get("monthly_allowance"))
                                           for m in members], monthly_budget, second_approval_over)
        if not h.adults:
            raise ValidationError("a household needs at least one adult")
        self.ledger.put_household(h)
        return h

    def _household_checks(self, user_id: str, amount: float, now: datetime) -> tuple[list[str], list[str]]:
        from .household import checks
        house = self.ledger.household_for(user_id)
        if house is None:
            return [], []
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        return checks(house, user_id, amount,
                      self.ledger.approved_spend_since([user_id], month),
                      self.ledger.approved_spend_since([m.user_id for m in house.members], month))

    def pending_for(self, approver: str) -> list[dict]:
        """Requests this person can act on: their own, plus their household's if they're an adult."""
        house = self.ledger.household_for(approver)
        users = [approver]
        if house is not None and house.can_approve(approver, approver):
            users = [m.user_id for m in house.members]
        out = []
        for u in users:
            for p in self.pending(u):
                if approver not in {a["approver"] for a in self.ledger.approvals(p["request_id"])}:
                    out.append({**p, "user_id": u})
        return out

    # --- reorders, follow-ups, returns, disputes (spec section 3f) ---
    def _last_same_item(self, user_id: str, item: str | None, product_id: str | None) -> date | None:
        from .history import match_level
        if not item:
            return None
        dates = [p.on for p in self._past_purchases(user_id)
                 if p.item and p.verdict not in ("rejected", "disputed")
                 and match_level(item, product_id, p.item, p.product_id) == "same"]
        return max(dates) if dates else None

    def reorder_predictions(self, user_id: str, within_days: int | None = None) -> list[dict]:
        from .reorder import predict
        today = self.clock().date()

        def name(p):
            v = next((k for k in p.merchant_keys if k.startswith("id:")), None)
            return v[3:] if v else sorted(p.merchant_keys)[0]
        preds = predict(self._past_purchases(user_id), name, today)
        if within_days is not None:
            preds = [p for p in preds if (p.next_due - today).days <= within_days]
        return [p.to_dict() for p in preds]

    def add_standing_mandate(self, user_id: str, item: str, max_unit_price: float, every_days: int,
                             task_type: str = "household", product_id: str | None = None,
                             per_cycle_budget: float | None = None) -> Mandate:
        import re as _re
        slug = _re.sub(r"[^a-z0-9]+", "-", item.lower()).strip("-")[:40]
        standing = {"item": item, "max_unit_price": max_unit_price, "min_interval_days": every_days}
        if product_id:
            standing["product_id"] = product_id
        return self.add_mandate({
            "id": f"standing_{user_id}_{slug}", "user_id": user_id, "intent": f"Restock {item}",
            "task_type": task_type, "budget": {"max": per_cycle_budget or max_unit_price * 4},
            "standing": standing})

    def _purchase_views(self, user_id: str) -> list[dict]:
        """Approved earnest purchases plus imported orders, with return windows."""
        mships = self.ledger.memberships(user_id)
        out = []
        for p in self.ledger.purchases(user_id):
            keys = self.merchants.canonical(p["merchant"])
            v = self.merchants.find_verified(p["merchant"])
            out.append({**p, "bought": datetime.fromisoformat(p["at"]).date(), "merchant_id": v.id if v else None,
                        "memberships": [m["program"] for m in mships if keys & self.merchants.canonical(m["merchant"])]})
        return out

    def followups(self, user_id: str) -> dict:
        from .followup import purchase_followups, should_auto_accept
        from .history import match_level
        from .returns import reminders
        today = self.clock().date()
        profile = self.profile(user_id)
        views = self._purchase_views(user_id)
        orders = self.ledger.orders(user_id)
        quotes = self.ledger.price_quotes("0000-01-01")
        auto, items = [], []
        for p in views:
            rw = return_window(p["merchant_id"], p.get("category"), p["memberships"])
            deadline = rw.deadline(p["bought"])
            if profile.auto_accept_kept and should_auto_accept(p, today, deadline if rw.known else None):
                self.report_outcome({"request_id": p["request_id"], "verdict": "accepted",
                                     "note": "auto: verified and kept past the return window"})
                auto.append(p["request_id"])
                p["verdict"] = "accepted"
            keys = self.merchants.canonical(p["merchant"])
            later = []
            if p.get("item"):
                later += [o["unit_price"] for o in orders if date.fromisoformat(o["date"]) > p["bought"]
                          and keys & self.merchants.canonical(o["merchant"])
                          and match_level(p["item"], p.get("product_id"), o["item"], o["product_id"]) == "same"]
                later += [q["price"] for q in quotes if date.fromisoformat(q["date"]) > p["bought"]
                          and not q["source"].startswith("agent:")
                          and keys & self.merchants.canonical(q["merchant"])
                          and match_level(p["item"], p.get("product_id"), q["item"], q["product_id"]) == "same"]
            items += purchase_followups(p, today, later, deadline)
        returns = reminders(views + self._order_views(user_id), today)
        return {"auto_accepted": auto,
                "followups": [f.to_dict() for f in sorted(items, key=lambda f: f.priority)],
                "returns": [r.to_dict() for r in returns]}

    def _order_views(self, user_id: str) -> list[dict]:
        out = []
        for o in self.ledger.orders(user_id):
            v = self.merchants.find_verified(o["merchant"])
            out.append({"merchant": o["merchant"], "merchant_id": v.id if v else None, "item": o["item"],
                        "bought": date.fromisoformat(o["date"]), "memberships": []})
        return out

    def return_reminders(self, user_id: str) -> list[dict]:
        from .returns import reminders
        views = self._purchase_views(user_id) + self._order_views(user_id)
        return [r.to_dict() for r in reminders(views, self.clock().date())]

    def dispute_pack(self, request_id: str) -> dict:
        from .disputes import build
        row = self.ledger.get_request(request_id)
        if row is None:
            raise ValidationError(f"unknown request: {request_id}")
        data = json.loads(row["data"])
        mandate = self.ledger.get_mandate(row["mandate_id"])
        keys = self.merchants.canonical(row["merchant"])
        orders = [o for o in self.ledger.orders(row["user_id"]) if keys & self.merchants.canonical(o["merchant"])]
        try:
            intent = self.ap2_intent_mandate(request_id)
        except ValidationError:
            intent = None
        return build(request={**dict(row), "data": data}, mandate=mandate.to_dict() if mandate else None,
                     decision=json.loads(row["decision"]), events=self.ledger.request_events(request_id),
                     approvals=self.ledger.approvals(request_id),
                     verification=self.ledger.verification(request_id),
                     outcome=self.ledger.get_outcome(request_id), orders=orders, intent_mandate=intent)

    # --- portable trust & shared reputation (spec section 3h) ---
    def _issuer_secret(self) -> bytes:
        return self.ledger.secret("issuer_ed25519")

    def export_trust_record(self, user_id: str, agent_id: str, task_type: str, ttl_days: int = 30) -> str:
        from . import portable
        now = self.clock()
        _, t = self.tier(user_id, agent_id, task_type)
        mine = [p for p in self.ledger.purchases(user_id) if p["agent_id"] == agent_id and p["task_type"] == task_type]
        verified = sum(p["verification"] == "verified" for p in mine)
        judged = [p for p in mine if p["verdict"] or p["verification"] == "mismatch"]
        kept = sum(p["verdict"] == "accepted" and p["verification"] != "mismatch" for p in judged)
        bad = self.ledger.count_events_since(user_id, agent_id, task_type, ("violation", "item_mismatch"),
                                             (now - timedelta(days=90)).isoformat())
        kid = portable._b64(portable.ed25519.public_key(self._issuer_secret()))
        claims = portable.claims_for(agent_id, task_type, t.name, len(mine), verified,
                                     kept / len(judged) if judged else None, bad, kid, now, ttl_days)
        return portable.encode(claims, self._issuer_secret())

    def import_trust_record(self, user_id: str, token: str, trust_issuer: bool = False) -> dict:
        from . import portable
        now = self.clock()
        try:
            claims, kid = portable.decode(token, now)
        except ValueError as e:
            raise ValidationError(str(e)) from None
        if kid not in self.ledger.trusted_issuers(user_id):
            if not trust_issuer:
                raise ValidationError(f"issuer {kid[:12]}... isn't one you trust yet; import with trust_issuer "
                                      "(CLI: --trust-issuer) if you know who issued it")
            self.ledger.trust_issuer(user_id, kid, now)
        agent, task = claims["sub"], claims["trust"]["task_type"]
        seed = portable.seed_points(claims)
        marker = f"import:{kid}"
        added = 0.0
        if seed and not self.ledger.has_event(user_id, agent, task, "imported_trust", marker):
            current = self.ledger.score(user_id, agent, task, now)
            added = max(0.0, seed - current)
            self.ledger.add_points(user_id, agent, task, "imported_trust", added, marker, now)
        return {"agent_id": agent, "task_type": task, "issuer": kid, "record": claims["trust"],
                "head_start": seed, "points_added": round(added, 2),
                "tier_now": self.limits(user_id, agent, task)["tier"]}

    def _rep_key(self, merchant: str) -> str:
        from .merchants import host_of, name_key
        v = self.merchants.find_verified(merchant)
        return "id:" + v.id if v else (host_of(merchant) or name_key(merchant))

    def merchant_stats(self, user_id: str) -> dict[str, dict]:
        stats: dict[str, dict] = {}
        for p in self.ledger.purchases(user_id):
            s = stats.setdefault(self._rep_key(p["merchant"]), {"purchases": 0, "problems": 0})
            s["purchases"] += 1
            if p["verification"] in ("mismatch", "unverified") or p["verdict"] in ("rejected", "disputed"):
                s["problems"] += 1
        return stats

    def reputation_contribution(self, user_id: str, epsilon: float = 1.0) -> dict:
        from .reputation import contribution
        return contribution(self.merchant_stats(user_id), epsilon)

    def import_reputation_feed(self, feed: dict) -> int:
        self.ledger.set_reputation_feed(feed.get("merchants", {}), self.clock())
        return len(feed.get("merchants", {}))

    def _apply_reputation(self, check, merchant: str) -> None:
        from .reputation import judge
        entry = self.ledger.reputation({self._rep_key(merchant)})
        verdict = judge(entry)
        if verdict is None or check.status in ("untrusted", "user_trusted"):
            return
        if verdict == "bad":
            tag = f"merchant_reputation:bad:{entry['problem_rate']:.2f}"
            if check.status == "unknown":
                check.status, check.reasons = "suspicious", [tag]
            else:
                check.reasons.append(tag)
        elif check.status == "unknown":
            check.reasons.append("merchant_reputation:good")

    # --- learned confidence weights (spec section 3e) ---
    def fit_confidence(self, user_id: str, save: bool = True) -> dict:
        samples = [Sample(tuple(float(f[k]["score"]) for k in FACTORS), kept)
                   for f, kept in self.ledger.labeled_purchases(user_id)]
        report = learn(samples)
        if save:
            self.ledger.set_confidence_weights(
                user_id, report["weights"] if report["status"] == "learned" else None, self.clock())
        return report

    # --- market prices ---
    def report_price(self, merchant: str, item: str, price: float, source: str = "user",
                     product_id: str | None = None, url: str | None = None) -> None:
        from .products import normalize
        self.ledger.add_price_quote(PriceQuote(merchant, item, round(price, 2), self.clock().date(), source,
                                               normalize(product_id), url), self.clock())

    # --- approvals on your phone or in chat ---
    def approval_links(self, request_id: str, approver: str | None = None) -> tuple[str, str]:
        secret, now = self.ledger.secret("approvals"), self.clock().timestamp()
        return tuple(f"{self.approval_url}/a/{make_token(secret, request_id, a, now, approver=approver)}"
                     for a in ("approve", "deny"))

    def _notify(self, request_id: str, req: SpendRequest, decision: Decision) -> None:
        row = self.ledger.get_request(request_id)
        house = self.ledger.household_for(row["user_id"]) if row else None
        approvers = [None]
        if house is not None and house.member(row["user_id"]) is not None:
            approvers = house.adults
        for who in approvers:
            approve, deny = self.approval_links(request_id, who)
            try:
                self.notifier.notify(ApprovalMessage(
                    request_id, req.agent_id, req.merchant, req.amount, req.item, req.reason,
                    decision.reasons, decision.confidence, approve, deny, approver=who))
            except Exception:
                pass  # notification trouble never blocks a decision; `earnest pending` still lists it

    def read_approval_token(self, token: str) -> tuple[str, str]:
        return read_token(self.ledger.secret("approvals"), token, self.clock().timestamp())

    def resolve_token(self, token: str) -> Decision:
        request_id, action, approver = read_token(self.ledger.secret("approvals"), token,
                                                  self.clock().timestamp(), with_approver=True)
        return self.resolve(request_id, approve=action == "approve", approver=approver)

    # --- AP2 interop ---
    def _approved(self, request_id: str) -> tuple[dict, dict, dict]:
        row = self.ledger.get_request(request_id)
        if row is None or row["status"] != "approved":
            raise ValidationError(f"request {request_id} is not approved")
        decision = json.loads(row["decision"])
        return dict(row), json.loads(row["data"]), decision["credential"]

    def ap2_intent_mandate(self, request_id: str) -> dict:
        """The approved request as an AP2 Intent mandate, attested by this gateway."""
        row, data, cred = self._approved(request_id)
        mandate = self.ledger.get_mandate(row["mandate_id"])
        return ap2.intent_mandate(
            description=mandate.intent, merchant=row["merchant"], amount=cred["cap"],
            currency=cred["currency"], expires_at=datetime.fromisoformat(cred["expires_at"]),
            request_id=request_id, item=data.get("item"),
            needs_cart_confirmation=mandate.approval != "auto",
            requires_refundability=mandate.requires_returnable, secret=self.ledger.secret("ap2"))

    def check_ap2_cart(self, request_id: str, cart_mandate: dict) -> dict:
        """Check a merchant's AP2 Cart mandate against the approved request before paying."""
        _, data, cred = self._approved(request_id)
        ok, reason = ap2.check_cart_mandate(
            cart_mandate, merchant_lock=cred["merchant_lock"], cap=cred["cap"], currency=cred["currency"],
            item_lock=cred.get("item_lock"), now=self.clock(), product_lock=cred.get("product_lock"))
        out = {"ok": ok, "reason": reason}
        if reason.startswith("item_mismatch"):
            out["verification"] = self._record_verification(request_id, "mismatch", reason)
        elif ok and cred.get("item_lock"):
            out["verification"] = self._record_verification(request_id, "verified", "AP2 cart matched")
        return out

    # --- item verification (spec section 3d) ---
    def _record_verification(self, request_id: str, status: str, detail: str) -> dict:
        prior = self.ledger.verification(request_id)
        if prior and prior["status"] in ("verified", "mismatch"):
            return {"request_id": request_id, "status": prior["status"], "detail": prior["detail"]}
        row = self.ledger.get_request(request_id)
        now = self.clock()
        self.ledger.set_verification(request_id, status, detail, now)
        key = (row["user_id"], row["agent_id"], row["task_type"])
        if status == "mismatch":
            self.ledger.add_points(*key, "item_mismatch", VIOLATION, request_id, now)
            self._on_bad_event(row["user_id"], row["agent_id"], "item_mismatch", request_id, now)
        elif status == "verified":
            self.ledger.add_points(*key, "item_verified", ITEM_VERIFIED, request_id, now)
        return {"request_id": request_id, "status": status, "detail": detail}

    def authorize_charge(self, credential_id: str, amount: float, merchant: str,
                         line_items: list[dict] | None = None) -> dict:
        """A merchant charges an issued credential, optionally with cart line items.
        (With a real backend this arrives as an authorization webhook.)"""
        items = [LineItem.from_dict(li) for li in line_items] if line_items is not None else None
        ok, reason = self.backend.authorize(credential_id, amount, merchant, self.clock(), items)
        out = {"approved": ok, "reason": reason}
        request_id = self.ledger.request_for_credential(credential_id)
        if request_id and items is not None and reason.startswith("item_mismatch"):
            out["verification"] = self._record_verification(request_id, "mismatch", reason)
        elif request_id and ok and reason == "approved" and items is not None:
            cred = getattr(self.backend, "cards", {}).get(credential_id)
            if cred is not None and cred.item_lock:
                out["verification"] = self._record_verification(request_id, "verified", "cart matched")
        return out

    def reconcile(self, user_id: str) -> list[dict]:
        """Match approved purchases that named an item against the user's order history."""
        today = self.clock().date()
        orders = [(frozenset(self.merchants.canonical(o["merchant"])),
                   OrderLine(o["item"], o["unit_price"] * o["quantity"], date.fromisoformat(o["date"]),
                             o["product_id"]))
                  for o in self.ledger.orders(user_id)]
        results = []
        for p in self.ledger.purchases(user_id):
            if not p["item"] or p["verification"] not in (None, "pending"):
                continue
            keys = self.merchants.canonical(p["merchant"])
            here = [line for k, line in orders if keys & k]
            status, detail = reconcile(p["item"], p["amount"], datetime.fromisoformat(p["at"]).date(),
                                       here, today, p["product_id"])
            if status == "pending" and p["verification"] == "pending":
                continue
            results.append(self._record_verification(p["request_id"], status, detail))
        return results

    # --- agent accounts (spec section 3i) ---
    def _tier_ceiling(self, user_id: str, agent_id: str) -> float:
        tasks = self.ledger.task_types(user_id, agent_id)
        tiers = [self.tier(user_id, agent_id, task)[1] for task in tasks] or [tier_for(0.0, self.tiers)]
        return tier_ceiling(tiers)

    def _funding(self, user_id: str, agent_id: str) -> list[Funding]:
        out = []
        for f in self.ledger.funding(user_id, agent_id):
            keys = frozenset(self.merchants.canonical(f["merchant"])) if f["merchant"] else frozenset()
            out.append(Funding(f["id"], f["kind"], f["balance"], f["merchant"], f["last4"], f["label"], keys))
        return out

    def _account(self, user_id: str, agent_id: str) -> dict:
        acct = self.ledger.get_account(user_id, agent_id)
        if acct is None:
            raise ValidationError(f"{agent_id} has no account; open one with `earnest account open`")
        return acct

    def _allowance(self, user_id: str, agent_id: str) -> Funding:
        return next(f for f in self._funding(user_id, agent_id) if f.kind == ALLOWANCE)

    def _sync_ceiling(self, user_id: str, agent_id: str, now: datetime) -> None:
        """When the agent's tier falls, its allowance ceiling falls with it: the excess goes back."""
        acct = self.ledger.get_account(user_id, agent_id)
        if acct is None:
            return
        cap = effective_ceiling(acct["max_allowance"], self._tier_ceiling(user_id, agent_id))
        allowance = self._allowance(user_id, agent_id)
        if allowance.balance > cap + 1e-9:
            self.ledger.add_entry(user_id, agent_id, allowance.id, "ceiling", cap - allowance.balance, None,
                                  f"returned to you: allowance ceiling is now ${cap:.2f}", now)

    def _funding_for_approval(self, user_id: str, req: SpendRequest) -> Funding | None:
        """When the user approves an escalated request from an agent with an account, the
        account still has to be able to pay for it."""
        acct = self.ledger.get_account(user_id, req.agent_id)
        if acct is None:
            return None
        if acct["status"] != "active":
            raise ValidationError(f"{req.agent_id}'s account is {acct['status']}; "
                                  f"unfreeze it first (earnest account unfreeze --agent {req.agent_id})")
        sources = self._funding(user_id, req.agent_id)
        keys = self.merchants.canonical(req.merchant)
        f = pick_funding(sources, keys, req.amount)
        if f is None:
            raise ValidationError(
                f"{req.agent_id} has ${usable_for(sources, keys):.2f} it could spend at {req.merchant}, "
                f"not ${req.amount:.2f}: top it up or add a gift card first (earnest account topup / add-card)")
        return f

    def open_account(self, user_id: str, agent_id: str, allowance: float = 0.0, max_allowance: float | None = None,
                     topup_to: float | None = None, topup_every_days: int | None = None) -> dict:
        """Give an agent its own balance. `max_allowance` is the most its allowance may hold (default:
        what its current tier allows); it never rises above the tier, and raises need your OK."""
        if (topup_to is None) != (topup_every_days is None):
            raise ValidationError("automatic refills need both --topup-to and --every-days")
        for name, v in (("allowance", allowance), ("max_allowance", max_allowance), ("topup_to", topup_to)):
            if v is not None and v < 0:
                raise ValidationError(f"{name} must be >= 0")
        if topup_every_days is not None and topup_every_days < 1:
            raise ValidationError("every-days must be at least 1")
        now = self.clock()
        approved = max_allowance if max_allowance is not None else self._tier_ceiling(user_id, agent_id)
        self.ledger.put_account(user_id, agent_id, approved, topup_to, topup_every_days, now)
        added = self.top_up(user_id, agent_id, allowance) if allowance else None
        out = self.account(user_id, agent_id)
        if added and added.get("capped"):
            out["note"] = f"only ${added['added']:.2f} of ${allowance:.2f} added: {added['capped']}"
        return out

    def account(self, user_id: str, agent_id: str) -> dict:
        acct = self._account(user_id, agent_id)
        now = self.clock()
        self._sync_ceiling(user_id, agent_id, now)
        sources = self._funding(user_id, agent_id)
        tier_cap = self._tier_ceiling(user_id, agent_id)
        allowance = next(f for f in sources if f.kind == ALLOWANCE)
        refill = None
        if acct["topup_to"] is not None:
            last = datetime.fromisoformat(acct["last_topup"]) if acct["last_topup"] else None
            refill = {"to": acct["topup_to"], "every_days": acct["topup_every_days"],
                      "last": last.isoformat() if last else None,
                      "next": (last + timedelta(days=acct["topup_every_days"])).date().isoformat() if last else "now"}
        return {
            "user_id": user_id, "agent_id": agent_id, "status": acct["status"],
            "status_reason": acct["status_reason"],
            "balance": round(sum(f.balance for f in sources), 2),
            "sources": [f.to_dict() for f in sources],
            "allowance": {"balance": round(allowance.balance, 2),
                          "ceiling": {"approved": acct["max_allowance"], "tier": tier_cap,
                                      "effective": effective_ceiling(acct["max_allowance"], tier_cap)},
                          "refill": refill},
            "raise_offer": raise_offer(acct["max_allowance"], tier_cap),
        }

    def top_up(self, user_id: str, agent_id: str, amount: float, note: str = "top-up") -> dict:
        """Add to the agent's allowance, up to its ceiling. Only the user tops up; never the agent."""
        acct = self._account(user_id, agent_id)
        if acct["status"] != "active":
            raise ValidationError(f"{agent_id}'s account is {acct['status']}; unfreeze it first")
        if amount <= 0:
            raise ValidationError("amount must be > 0")
        now = self.clock()
        self._sync_ceiling(user_id, agent_id, now)
        cap = effective_ceiling(acct["max_allowance"], self._tier_ceiling(user_id, agent_id))
        allowance = self._allowance(user_id, agent_id)
        room = round(cap - allowance.balance, 2)
        if room <= 0:
            raise ValidationError(f"{agent_id}'s allowance is at its ${cap:.2f} ceiling")
        added = round(min(amount, room), 2)
        self.ledger.add_entry(user_id, agent_id, allowance.id, "topup", added, None, note, now)
        self.ledger.update_account(user_id, agent_id, last_topup=now.isoformat())
        out = {"agent_id": agent_id, "added": added, "allowance": round(allowance.balance + added, 2)}
        if added < amount:
            out["capped"] = f"the allowance can hold ${cap:.2f} at {agent_id}'s current limits"
        return out

    def add_card(self, user_id: str, agent_id: str, kind: str, balance: float, last4: str,
                 merchant: str | None = None, label: str | None = None) -> dict:
        """Fund the agent with a gift card (one store) or a prepaid card (anywhere) that you bought.
        Only the last 4 digits are stored; load the card into the agent's own store account."""
        import re as _re
        self._account(user_id, agent_id)
        if kind not in ("gift_card", "prepaid"):
            raise ValidationError("kind must be gift_card or prepaid")
        if not _re.fullmatch(r"\d{4}", last4 or ""):
            raise ValidationError("last4 must be the card's last 4 digits (never the full number or code)")
        if balance <= 0:
            raise ValidationError("balance must be > 0")
        if kind == "gift_card":
            if not merchant:
                raise ValidationError("a gift card needs its store, e.g. --merchant target.com")
            check = self.merchants.check(merchant, self.ledger.merchant_overrides(user_id))
            if check.status == "untrusted":
                raise ValidationError(f"{merchant} is untrusted ({', '.join(check.reasons)})")
        now = self.clock()
        fid = f"{kind}:{uuid.uuid4().hex[:8]}"
        self.ledger.add_funding(fid, user_id, agent_id, kind, merchant if kind == "gift_card" else None,
                                last4, label, now)
        self.ledger.add_entry(user_id, agent_id, fid, "load", balance, None, "card added", now)
        notes = []
        cap = self._tier_ceiling(user_id, agent_id)
        if balance > cap:
            notes.append(f"the card holds more than {agent_id}'s per-purchase limit (${cap:.2f}); "
                         "bigger purchases still ask you")
        if kind == "prepaid":
            notes.append("a prepaid card works at any store, so earnest's rules are the only merchant check")
        return {"funding_id": fid, "kind": kind, "balance": round(balance, 2), "notes": notes}

    def set_card_balance(self, user_id: str, agent_id: str, funding_id: str, balance: float) -> dict:
        """Correct a gift / prepaid card's balance to what the store or issuer shows."""
        f = next((f for f in self._funding(user_id, agent_id) if f.id == funding_id), None)
        if f is None or f.kind == ALLOWANCE:
            raise ValidationError(f"no gift or prepaid card {funding_id} on {agent_id}'s account")
        if balance < 0:
            raise ValidationError("balance must be >= 0")
        if abs(balance - f.balance) > 1e-9:
            self.ledger.add_entry(user_id, agent_id, f.id, "adjust", balance - f.balance, None,
                                  "balance corrected by you", self.clock())
        return {"funding_id": funding_id, "balance": round(balance, 2)}

    def remove_card(self, user_id: str, agent_id: str, funding_id: str) -> None:
        f = next((f for f in self._funding(user_id, agent_id) if f.id == funding_id), None)
        if f is None or f.kind == ALLOWANCE:
            raise ValidationError(f"no gift or prepaid card {funding_id} on {agent_id}'s account")
        self.ledger.set_funding_active(funding_id, False)

    def _sweep(self, user_id: str, agent_id: str, note: str, now: datetime) -> float:
        allowance = self._allowance(user_id, agent_id)
        if allowance.balance > 0:
            self.ledger.add_entry(user_id, agent_id, allowance.id, "sweep", -allowance.balance, None, note, now)
        return round(max(0.0, allowance.balance), 2)

    def freeze(self, user_id: str, agent_id: str | None = None, reason: str = "frozen by you") -> list[dict]:
        """Stop an agent's spending (or every agent's, without `agent_id`). Its allowance goes back to
        you; gift and prepaid cards can't be swept, so take them off its store accounts too."""
        now = self.clock()
        accts = [self._account(user_id, agent_id)] if agent_id else self.ledger.accounts(user_id)
        out = []
        for a in accts:
            if a["status"] != "frozen":
                self.ledger.update_account(user_id, a["agent_id"], status="frozen", status_reason=reason)
            returned = self._sweep(user_id, a["agent_id"], f"returned to you: {reason}", now)
            cards = [f.describe() for f in self._funding(user_id, a["agent_id"]) if f.kind != ALLOWANCE]
            out.append({"agent_id": a["agent_id"], "status": "frozen", "allowance_returned": returned,
                        "cards_still_loaded": cards})
        return out

    def unfreeze(self, user_id: str, agent_id: str) -> dict:
        self._account(user_id, agent_id)
        self.ledger.update_account(user_id, agent_id, status="active", status_reason=None)
        return self.account(user_id, agent_id)

    def _on_bad_event(self, user_id: str, agent_id: str, kind: str, request_id: str | None, now: datetime) -> None:
        acct = self.ledger.get_account(user_id, agent_id)
        if acct is not None and kind in BAD_EVENTS and acct["status"] == "active":
            self.freeze(user_id, agent_id, reason=f"{kind} on {request_id}")

    def refill_accounts(self, user_id: str) -> list[dict]:
        """Run due automatic refills (schedule `earnest account refill` daily)."""
        now = self.clock()
        out = []
        for a in self.ledger.accounts(user_id):
            if a["status"] != "active" or a["topup_to"] is None:
                continue
            last = datetime.fromisoformat(a["last_topup"]) if a["last_topup"] else None
            if last is not None and now - last < timedelta(days=a["topup_every_days"]):
                continue
            self._sync_ceiling(user_id, a["agent_id"], now)
            cap = effective_ceiling(a["max_allowance"], self._tier_ceiling(user_id, a["agent_id"]))
            target = min(a["topup_to"], cap)
            balance = self._allowance(user_id, a["agent_id"]).balance
            if target - balance > 0.005:
                out.append(self.top_up(user_id, a["agent_id"], round(target - balance, 2), note="refill"))
            else:
                self.ledger.update_account(user_id, a["agent_id"], last_topup=now.isoformat())
        return out

    def account_offers(self, user_id: str) -> list[dict]:
        """Agents whose tier now allows a bigger allowance than you approved. Raising is your call."""
        out = []
        for a in self.ledger.accounts(user_id):
            offer = raise_offer(a["max_allowance"], self._tier_ceiling(user_id, a["agent_id"]))
            if offer and a["status"] == "active":
                good = [p for p in self.ledger.purchases(user_id) if p["agent_id"] == a["agent_id"]
                        and p["verdict"] == "accepted"]
                out.append({"agent_id": a["agent_id"], "raise_from": offer["from"], "raise_to": offer["to"],
                            "why": f"{len(good)} purchase(s) you kept; its trust tier now allows "
                                   f"${offer['to']:.0f}",
                            "accept": f"earnest account raise --user {user_id} --agent {a['agent_id']}"})
        return out

    def accept_raise(self, user_id: str, agent_id: str) -> dict:
        acct = self._account(user_id, agent_id)
        offer = raise_offer(acct["max_allowance"], self._tier_ceiling(user_id, agent_id))
        if offer is None:
            raise ValidationError(f"no raise on offer for {agent_id}")
        self.ledger.update_account(user_id, agent_id, max_allowance=offer["to"])
        return self.account(user_id, agent_id)

    def refund(self, request_id: str, amount: float | None = None) -> dict:
        """Money came back for a purchase (a return): credit the source it was paid from."""
        rows = self.ledger.entries_for_request(request_id)
        paid = [r for r in rows if r["kind"] in ("hold", "spend")]
        if not paid:
            raise ValidationError(f"{request_id} wasn't paid from an agent account")
        back = sum(r["amount"] for r in rows if r["kind"] in ("refund", "release"))
        refundable = round(-sum(r["amount"] for r in paid) - back, 2)
        amount = refundable if amount is None else round(amount, 2)
        if amount <= 0 or amount > refundable + 1e-9:
            raise ValidationError(f"refund must be between $0.01 and ${refundable:.2f}")
        p = paid[0]
        self.ledger.add_entry(p["user_id"], p["agent_id"], p["funding_id"], "refund", amount, request_id,
                              "refund", self.clock())
        return {"request_id": request_id, "refunded": amount, "to": p["funding_id"]}

    def settle_accounts(self, user_id: str) -> list[dict]:
        """Release what expired cards didn't spend back to the allowance. Needs a backend that
        reports what each card spent (the mock backend does)."""
        spent_by_card = getattr(self.backend, "spent", None)
        if spent_by_card is None:
            return []
        now = self.clock()
        out = []
        for h in self.ledger.holds(user_id):
            cred = json.loads(h["decision"]).get("credential") or {}
            if not cred or datetime.fromisoformat(cred["expires_at"]) > now:
                continue
            back = sum(r["amount"] for r in self.ledger.entries_for_request(h["request_id"])
                       if r["kind"] in ("release", "refund"))
            unused = round(-h["amount"] - spent_by_card.get(cred["id"], 0.0) - back, 2)
            if unused > 0:
                self.ledger.add_entry(user_id, h["agent_id"], h["funding_id"], "release", unused, h["request_id"],
                                      f"card {cred['id']} expired unused", now)
                out.append({"request_id": h["request_id"], "released": unused})
        return out

    def account_statement(self, user_id: str, agent_id: str, days: int = 30) -> dict:
        self._account(user_id, agent_id)
        now = self.clock()
        since = (now - timedelta(days=days)).isoformat()
        entries = self.ledger.entries(user_id, agent_id, since)
        mine = [p for p in self.ledger.purchases(user_id) if p["agent_id"] == agent_id and p["at"] >= since]
        view = self.account(user_id, agent_id)
        spent = -sum(e["amount"] for e in entries if e["kind"] in ("hold", "spend"))
        back = sum(e["amount"] for e in entries if e["kind"] in ("refund", "release"))
        kept = sum(p["verdict"] == "accepted" for p in mine)
        bad = sum(p["verdict"] in ("rejected", "disputed") for p in mine)
        waiting = len(mine) - kept - bad
        parts = []
        if mine and kept == len(mine):
            parts.append("all kept")
        else:
            parts += [f"{kept} kept"] if kept else []
            parts += [f"{bad} sent back"] if bad else []
            parts += [f"{waiting} waiting for your verdict"] if waiting else []
        summary = (f"{agent_id}: ${view['balance']:.2f} left, {len(mine)} purchase(s) in {days} days"
                   + (": " + ", ".join(parts) if parts else ""))
        if view["status"] != "active":
            summary += f" (frozen: {view['status_reason']})"
        return {"summary": summary, "balance": view["balance"], "status": view["status"],
                "spent": round(spent, 2), "returned": round(back, 2), "purchases": len(mine), "kept": kept,
                "entries": entries}

    # --- shadow mode (spec section 3j) ---
    def _shadow_card(self, user_id: str, agent_id: str) -> str | None:
        e = self.ledger.shadow_enrollment(user_id, agent_id)
        return e["card"] if e and not e["ended_at"] else None

    def _card_txs(self, user_id: str, card: str | None):
        from .shadow import CardTx
        return [CardTx(t["id"], date.fromisoformat(t["date"]), t["merchant"], t["category"], t["amount"])
                for t in self.ledger.card_transactions(user_id, card)]

    def _merchant_name(self, merchant: str) -> str:
        v = self.merchants.find_verified(merchant)
        return v.name if v else merchant

    def card_profile(self, user_id: str, card: str) -> dict:
        """What one card's history says about how you spend: read-only, nothing is applied."""
        from .shadow import card_profile
        txs = self._card_txs(user_id, card)
        if not txs:
            raise ValidationError(f"no transactions for card {card!r}; import a statement with "
                                  f"`earnest import FILE --user {user_id} --card {card!r}`")
        return {"card": card, **card_profile(txs, self._merchant_name)}

    def start_shadow(self, user_id: str, agent_id: str, card: str) -> dict:
        """Put an agent in read-only shadow mode, learning from one card."""
        prof = self.card_profile(user_id, card)
        self.ledger.put_shadow(user_id, agent_id, card, self.clock())
        return {"agent_id": agent_id, "card": card, "mode": "shadow",
                "learned": {k: prof[k] for k in ("transactions", "months", "monthly_spend", "purchase_size")},
                "regulars": [m["merchant"] for m in prof["regulars"]],
                "note": "the agent's requests get earnest's real decision but never a card or any money"}

    def shadow_review(self, request_id: str, would_buy: bool) -> dict:
        """Would you have let the agent buy this? Builds the agreement score."""
        from .shadow import agreement
        row = self.ledger.get_request(request_id)
        if row is None or row["status"] != "shadow":
            raise ValidationError(f"{request_id} isn't a shadow proposal")
        self.ledger.put_shadow_review(request_id, would_buy, self.clock())
        would_be = json.loads(row["decision"])["shadow"]["would_be"]
        return {"request_id": request_id, "earnest_would_have": would_be, "you": "yes" if would_buy else "no",
                "result": agreement(would_be, would_buy)}

    def shadow_pending(self, user_id: str) -> list[dict]:
        out = []
        for r in self.ledger.shadow_requests(user_id):
            if r["would_buy"] is not None:
                continue
            d, data = json.loads(r["decision"]), json.loads(r["data"])
            out.append({"request_id": r["id"], "agent_id": r["agent_id"], "merchant": r["merchant"],
                        "amount": r["amount"], "item": data.get("item"), "reason_given": data["reason"],
                        "earnest_would_have": d["shadow"]["would_be"],
                        "reasons": [x for x in d["reasons"] if x != "shadow_mode"],
                        "matched_purchase": r["tx_date"]})
        return out

    def shadow_match(self, user_id: str) -> list[dict]:
        """Match shadow proposals to what the card shows you went on to buy. Each match (of a
        proposal earnest wouldn't have denied) earns a little trust, capped below `established`."""
        from .shadow import MATCH_POINTS, MATCH_POINTS_CAP, match
        now = self.clock()
        taken = self.ledger.matched_tx_ids(user_id)
        out = []
        cards: dict[str, list] = {}
        for r in self.ledger.shadow_requests(user_id):
            if r["tx_id"]:
                continue
            d = json.loads(r["decision"])
            card = d["shadow"]["card"]
            txs = cards.setdefault(card, self._card_txs(user_id, card))
            t = match(datetime.fromisoformat(r["created_at"]).date(), self.merchants.canonical(r["merchant"]),
                      r["amount"], txs, self.merchants.canonical, taken)
            if t is None:
                continue
            taken.add(t.id)
            self.ledger.put_shadow_match(r["id"], t.id, t.on.isoformat(), t.amount, now)
            pts = 0.0
            if d["shadow"]["would_be"] != "denied":
                earned = self.ledger.points_of_kind(user_id, r["agent_id"], r["task_type"], "shadow_match")
                pts = round(max(0.0, min(MATCH_POINTS, MATCH_POINTS_CAP - earned)), 2)
                if pts:
                    self.ledger.add_points(user_id, r["agent_id"], r["task_type"], "shadow_match", pts, r["id"], now)
            out.append({"request_id": r["id"], "merchant": r["merchant"], "proposed": r["amount"],
                        "you_bought": {"date": t.on.isoformat(), "amount": t.amount}, "points": pts})
        return out

    def shadow_report(self, user_id: str, agent_id: str) -> dict:
        from .shadow import agreement, readiness
        e = self.ledger.shadow_enrollment(user_id, agent_id)
        if e is None:
            raise ValidationError(f"{agent_id} was never in shadow mode")
        now = self.clock()
        rows = self.ledger.shadow_requests(user_id, agent_id)
        would = {"approved": 0, "escalated": 0, "denied": 0}
        counts = {"agree": 0, "disagree": 0, "asked": 0}
        violations = 0
        for r in rows:
            sh = json.loads(r["decision"])["shadow"]
            would[sh["would_be"]] += 1
            if r["would_buy"] is not None:
                counts[agreement(sh["would_be"], bool(r["would_buy"]))] += 1
            if sh.get("violation") and datetime.fromisoformat(r["created_at"]) >= now - timedelta(days=30):
                violations += 1
        reviewed = sum(r["would_buy"] is not None for r in rows)
        matched = sum(bool(r["tx_id"]) for r in rows)
        ready = readiness(reviewed, counts["agree"], counts["disagree"], matched, violations)
        out = {"agent_id": agent_id, "card": e["card"], "mode": "live" if e["ended_at"] else "shadow",
               "since": e["started_at"], "proposals": len(rows), "earnest_would_have": would,
               "matched_real_purchases": matched, "reviewed": reviewed, **counts, **ready}
        if ready["ready"] and not e["ended_at"]:
            try:
                suggested = self.card_profile(user_id, e["card"])["suggested"]["starting_allowance"]
            except ValidationError:
                suggested = 0.0
            start = min(suggested, self._tier_ceiling(user_id, agent_id)) or self._tier_ceiling(user_id, agent_id)
            out["next"] = (f"earnest shadow live --user {user_id} --agent {agent_id} --allowance {start:g}"
                           f"  (a ${start:g} allowance it can't go beyond)")
        return out

    def go_live(self, user_id: str, agent_id: str, allowance: float | None = None) -> dict:
        """End shadow mode. With `allowance`, open an agent account so its spending stays capped."""
        if not self.ledger.end_shadow(user_id, agent_id, self.clock()):
            raise ValidationError(f"{agent_id} isn't in shadow mode")
        out = {"agent_id": agent_id, "mode": "live"}
        if allowance is not None:
            if self.ledger.get_account(user_id, agent_id) is None:
                self.open_account(user_id, agent_id, allowance)
            elif allowance > 0:
                self.top_up(user_id, agent_id, allowance)
            out["account"] = self.account(user_id, agent_id)
        return out

    def shadow_replay(self, user_id: str, card: str, task_type: str = "shopping", agent_id: str | None = None,
                      days: int | None = 90) -> dict:
        """If an agent had made this card's purchases, what would earnest have done? Each purchase
        is judged on what was known the day before it. Read-only: nothing is recorded."""
        from collections import Counter
        from datetime import time as dtime
        txs = self._card_txs(user_id, card)
        if not txs:
            raise ValidationError(f"no transactions for card {card!r}")
        if days:
            start = txs[-1].on - timedelta(days=days)
            txs = [t for t in txs if t.on > start]
        profile = self.profile(user_id)
        t = self.tier(user_id, agent_id, task_type)[1] if agent_id else tier_for(0.0, self.tiers)
        mandate = Mandate(id="shadow_replay", user_id=user_id, intent=f"everyday spending on {card}",
                          task_type=task_type, budget_max=1e12)
        results = {"approved": [], "escalated": [], "denied": []}
        reasons = Counter()
        others = [x for x in self.tiers if x.min_score is not None]
        auto_by_tier = Counter()
        for tx in txs:
            check, hist, known, conf = self.assess(user_id, tx.merchant, amount=tx.amount, category=tx.category,
                                                   mandate=mandate, as_of=tx.on, spent=0.0)
            req = SpendRequest(agent_id or "replay", mandate.id, tx.amount, tx.merchant, tx.category, "replay")

            def judge(tier: Tier):
                return decide(req, mandate, profile, tier, spent=0.0, is_new_merchant=not known,
                              now=datetime.combine(tx.on, dtime(12), tzinfo=timezone.utc), merchant=check,
                              history=hist, high_risk_category=self.merchants.high_risk_category(tx.category),
                              confidence=conf)
            v = judge(t)
            results[v.status].append({"date": tx.on.isoformat(), "merchant": tx.merchant, "amount": tx.amount,
                                      "reasons": v.reasons})
            if v.status != "approved":
                reasons.update({r.split(":")[0] for r in v.reasons})
            for other in others:
                auto_by_tier[other.name] += (v if other == t else judge(other)).status == "approved"
        n = len(txs)
        return {"card": card, "agent_id": agent_id, "tier": t.name, "purchases": n,
                "automatic_by_tier": {x.name: round(auto_by_tier[x.name] / n, 2) for x in reversed(others)},
                "would_go_through": len(results["approved"]), "would_ask_you": len(results["escalated"]),
                "would_block": len(results["denied"]),
                "share_automatic": round(len(results["approved"]) / n, 2) if n else None,
                "top_reasons": [{"reason": r, "count": c} for r, c in reasons.most_common(5)],
                "examples": {"asked": results["escalated"][:5], "blocked": results["denied"][:5]}}

    # --- gift concierge (spec section 3k) ---
    def _person(self, user_id: str, ref: str) -> dict:
        p = self.ledger.person(user_id, ref)
        if p is None:
            raise ValidationError(f"no one called {ref!r}; add them with `earnest gifts add-person`")
        return p

    def gift_policy(self, user_id: str) -> dict:
        from .gifting import DEFAULT_LEAD_DAYS
        return {"agent_id": None, "lead_days": DEFAULT_LEAD_DAYS, "annual_budget": None, "auto_buy": False,
                "relationship_budgets": {}, **self.ledger.gift_policy(user_id)}

    def set_gift_policy(self, user_id: str, **changes) -> dict:
        """Who shops (agent_id), how early (lead_days), the yearly gift budget, budgets by
        relationship, and the master switch for buying without asking (auto_buy)."""
        policy = self.gift_policy(user_id)
        for k, v in changes.items():
            if v is None:
                continue
            if k not in policy:
                raise ValidationError(f"unknown gift policy field: {k}")
            if k == "lead_days" and not (1 <= int(v) <= 90):
                raise ValidationError("lead_days must be 1-90")
            if k == "annual_budget" and float(v) < 0:
                raise ValidationError("annual_budget must be >= 0")
            if k == "relationship_budgets":
                if not isinstance(v, dict) or any(float(x) < 0 for x in v.values()):
                    raise ValidationError("relationship_budgets must map relationships to amounts >= 0")
                v = {**policy["relationship_budgets"], **{str(a).lower(): float(b) for a, b in v.items()}}
            policy[k] = v
        self.ledger.put_gift_policy(user_id, policy)
        return policy

    def add_person(self, user_id: str, name: str, relationship: str | None = None, *, budget: float | None = None,
                   auto_buy_max: float | None = None, address: str | None = None, sizes: dict | None = None,
                   notes: str | None = None, likes: list[str] | None = None, dislikes: list[str] | None = None,
                   wishes: list[str] | None = None) -> dict:
        """Add someone, or update them (matched by name). Unset fields keep their old values."""
        name = (name or "").strip()
        if not name:
            raise ValidationError("name is required")
        for k, v in (("budget", budget), ("auto_buy_max", auto_buy_max)):
            if v is not None and v < 0:
                raise ValidationError(f"{k} must be >= 0")
        old = self.ledger.person(user_id, name) or {}
        row = {"id": old.get("id") or f"p_{uuid.uuid4().hex[:8]}", "user_id": user_id, "name": old.get("name", name),
               "relationship": (relationship or old.get("relationship") or "other").lower(),
               "sizes": {**old.get("sizes", {}), **(sizes or {})}, "notes": notes or old.get("notes"),
               "budget": budget if budget is not None else old.get("budget"),
               "auto_buy_max": auto_buy_max if auto_buy_max is not None else old.get("auto_buy_max"),
               "address": address or old.get("address"), "created_at": old.get("created_at") or self.clock().isoformat()}
        self.ledger.put_person(row)
        for kind, items in (("like", likes), ("dislike", dislikes), ("wish", wishes)):
            for text in items or []:
                self.add_gift_preference(user_id, row["id"], text, kind, "you")
        return self.person_card(user_id, row["id"])

    def person_card(self, user_id: str, ref: str) -> dict:
        p = self._person(user_id, ref)
        return {**{k: p[k] for k in ("id", "name", "relationship", "budget", "auto_buy_max", "sizes", "notes")},
                "address_on_file": bool(p["address"]),
                "occasions": [{k: o[k] for k in ("id", "kind", "month", "day", "year", "label")}
                              for o in self.ledger.occasions(user_id, p["id"])],
                "preferences": self.ledger.prefs(p["id"]), "given_before": self.ledger.gift_history(p["id"])}

    def add_occasion(self, user_id: str, person: str, kind: str, when: str, label: str | None = None,
                     source: str = "you") -> dict:
        from .gifting import KINDS, parse_md
        if kind not in KINDS:
            raise ValidationError(f"kind must be one of {', '.join(KINDS)}")
        p = self._person(user_id, person)
        year, month, day = parse_md(when)
        row = {"id": f"o_{uuid.uuid4().hex[:8]}", "user_id": user_id, "person_id": p["id"], "kind": kind,
               "month": month, "day": day, "year": year, "label": label, "source": source}
        added = self.ledger.put_occasion(row)
        return {"person": p["name"], "kind": kind, "month": month, "day": day, "year": year, "added": added}

    def add_gift_preference(self, user_id: str, person: str, text: str, kind: str = "like",
                            source: str = "you") -> dict:
        """Something a person likes, dislikes or wishes for, and where it came from: `you`,
        `wishlist` (they asked for it), `conversation` (an agent heard it in your chats), `social`
        (a public post or wishlist you pointed to) or `feedback` (how a past gift went)."""
        from .gifting import SOURCES
        if kind not in ("like", "dislike", "wish"):
            raise ValidationError("kind must be like, dislike or wish")
        if source not in SOURCES:
            raise ValidationError(f"source must be one of {', '.join(SOURCES)}")
        text = (text or "").strip()
        if not text or len(text) > 200:
            raise ValidationError("preference text must be 1-200 characters")
        p = self._person(user_id, person)
        self.ledger.add_pref(user_id, p["id"], text, kind, source, self.clock())
        return {"person": p["name"], "text": text, "kind": kind, "source": source}

    def import_people(self, user_id: str, text: str) -> dict:
        from .gifting import parse_people_csv
        rows = parse_people_csv(text)
        added = 0
        for r in rows:
            existed = self.ledger.person(user_id, r["name"]) is not None
            self.add_person(user_id, r["name"], r["relationship"], budget=r.get("budget"),
                            auto_buy_max=r.get("auto_buy_max"), likes=r["likes"], dislikes=r["dislikes"])
            for kind, y, m, d in r["occasions"]:
                self.add_occasion(user_id, r["name"], kind, f"{y:04d}-{m:02d}-{d:02d}" if y else f"{m:02d}-{d:02d}",
                                  source="contacts")
            added += not existed
        return {"people": len(rows), "new": added}

    def import_calendar(self, user_id: str, text: str) -> dict:
        """Birthdays and anniversaries from a calendar export. New names are added as `other`;
        set their relationship (and so their budget) with `earnest gifts add-person`."""
        from .gifting import parse_ics
        events = parse_ics(text)
        new_people, added = [], 0
        for e in events:
            if self.ledger.person(user_id, e["name"]) is None:
                self.add_person(user_id, e["name"])
                new_people.append(e["name"])
            when = f"{e['year']:04d}-{e['month']:02d}-{e['day']:02d}" if e["year"] else f"{e['month']:02d}-{e['day']:02d}"
            added += self.add_occasion(user_id, e["name"], e["kind"], when, source="calendar")["added"]
        return {"events": len(events), "occasions_added": added, "new_people": new_people,
                "note": "set relationships so budgets fit: earnest gifts add-person NAME --relationship friend"
                        if new_people else None}

    def _occasion_obj(self, row: dict):
        from .gifting import Occasion
        return Occasion(row["id"], row["person_id"], row["kind"], row["month"], row["day"], row["year"], row["label"])

    def _person_obj(self, row: dict):
        from .gifting import Person
        return Person(row["id"], row["name"], row["relationship"], row["sizes"], row["notes"], row["budget"],
                      row["auto_buy_max"], row["address"])

    def upcoming_gifts(self, user_id: str, days: int = 60) -> list[dict]:
        from .gifting import upcoming
        today = self.clock().date()
        people = {p["id"]: p for p in self.ledger.people(user_id)}
        plans = {(p["occasion_id"], p["on_date"]): p for p in self.ledger.plans(user_id)}
        out = []
        for occ, on in upcoming([self._occasion_obj(o) for o in self.ledger.occasions(user_id)], today, days):
            p = people.get(occ.person_id)
            if p is None:
                continue
            plan = plans.get((occ.id, on.isoformat()))
            out.append({"date": on.isoformat(), "in_days": (on - today).days, "what": occ.describe(p["name"], on),
                        "person": p["name"], "plan": plan["id"] if plan else None,
                        "status": plan["status"] if plan else "not planned yet"})
        return out

    def plan_gifts(self, user_id: str) -> list[dict]:
        """Open a gift plan for every occasion inside the lead window (run daily). Plans whose
        date passed without a purchase are marked missed."""
        from .gifting import budget_for, upcoming
        policy = self.gift_policy(user_id)
        now = self.clock()
        today = now.date()
        for plan in self.ledger.plans(user_id, ("open", "awaiting_approval", "declined")):
            if date.fromisoformat(plan["on_date"]) < today:
                self.ledger.update_plan(plan["id"], now, status="missed")
        people = {p["id"]: p for p in self.ledger.people(user_id)}
        created = []
        for occ, on in upcoming([self._occasion_obj(o) for o in self.ledger.occasions(user_id)], today,
                                policy["lead_days"]):
            p = people.get(occ.person_id)
            if p is None:
                continue
            remaining = None
            if policy["annual_budget"] is not None:
                committed = sum(x["budget"] for x in self.ledger.plans(user_id, ("open", "awaiting_approval"))
                                if x["on_date"][:4] == str(on.year))
                remaining = policy["annual_budget"] - self.ledger.gift_spend(user_id, on.year) - committed
            budget, why = budget_for(self._person_obj(p), occ, on, policy["relationship_budgets"], remaining)
            plan_id = f"g_{uuid.uuid4().hex[:10]}"
            if self.ledger.put_plan({"id": plan_id, "user_id": user_id, "person_id": p["id"], "occasion_id": occ.id,
                                     "on_date": on.isoformat(), "budget": budget,
                                     "status": "open" if budget > 0 else "declined",
                                     "agent_id": policy["agent_id"], "at": now.isoformat()}):
                if budget <= 0:
                    self.ledger.update_plan(plan_id, now, note="no gift budget left this year")
                created.append({"plan": plan_id, "what": occ.describe(p["name"], on), "date": on.isoformat(),
                                "budget": budget, "budget_why": why})
        return created

    def _plan(self, plan_id: str) -> dict:
        plan = self.ledger.plan(plan_id)
        if plan is None:
            raise ValidationError(f"unknown gift plan: {plan_id}")
        return plan

    def _plan_context(self, plan: dict):
        person = self.ledger.person(plan["user_id"], plan["person_id"])
        occ = next(o for o in self.ledger.occasions(plan["user_id"], plan["person_id"]) if o["id"] == plan["occasion_id"])
        return person, self._occasion_obj(occ), date.fromisoformat(plan["on_date"])

    def _gift_cards(self, user_id: str, agent_id: str | None) -> dict[str, float]:
        """Gift-card balance the agent could use per store, keyed by the store as written on the card."""
        if not agent_id or self.ledger.get_account(user_id, agent_id) is None:
            return {}
        out: dict[str, float] = {}
        for f in self._funding(user_id, agent_id):
            if f.kind == "gift_card":
                out[f.merchant] = max(out.get(f.merchant, 0.0), f.balance)
        return out

    def _spendable(self, user_id: str, agent_id: str | None) -> dict | None:
        """What the shopping agent's account can pay with, if it has one (3i)."""
        if not agent_id or self.ledger.get_account(user_id, agent_id) is None:
            return None
        sources = self._funding(user_id, agent_id)
        return {"allowance": round(sum(f.balance for f in sources if f.kind == ALLOWANCE), 2),
                "prepaid": round(max([f.balance for f in sources if f.kind == "prepaid"], default=0.0), 2),
                "note": "a purchase must fit one source: a gift card at its store, the allowance, or a prepaid card"}

    def gift_brief(self, plan_id: str) -> dict:
        """What a shopping agent needs to find the right gift. No address until it's approved."""
        from .gifting import Preference
        plan = self._plan(plan_id)
        person, occ, on = self._plan_context(plan)
        prefs = [Preference(x["text"], x["kind"], x["source"]) for x in self.ledger.prefs(person["id"])]
        today = self.clock().date()
        return {
            "plan": plan_id, "status": plan["status"], "for": person["name"], "relationship": person["relationship"],
            "occasion": occ.describe(person["name"], on), "date": on.isoformat(), "in_days": (on - today).days,
            "budget": plan["budget"], "currency": "USD",
            "wishes": [f"{p.text} ({p.source})" for p in prefs if p.kind == "wish"],
            "likes": [f"{p.text} ({p.source})" for p in prefs if p.kind == "like"],
            "avoid": [p.text for p in prefs if p.kind == "dislike"],
            "sizes": person["sizes"], "notes": person["notes"],
            "given_before": [g["title"] for g in self.ledger.gift_history(person["id"])][:10],
            "gift_cards": [{"store": k, "balance": round(v, 2)}
                           for k, v in self._gift_cards(plan["user_id"], plan["agent_id"]).items()],
            "account": self._spendable(plan["user_id"], plan["agent_id"]),
            "note": plan["note"],
            "how": "Research 3-8 options that fit, then call propose_gift_options. Include eta_days so earnest "
                   "can check it arrives in time; mark e-gifts and experiences as digital.",
        }

    def propose_gifts(self, plan_id: str, options: list[dict] | None = None, agent_id: str | None = None) -> dict:
        """Rank options for a plan (an agent's research, or the popular-items catalog when none are
        given), then put the best one forward: for your approval, or bought straight away if
        you've authorized that for this person and price."""
        from .gifting import Option, Preference, rank
        plan = self._plan(plan_id)
        if plan["status"] not in ("open", "declined", "awaiting_approval"):
            raise ValidationError(f"plan {plan_id} is {plan['status']}")
        if agent_id and plan["agent_id"] and agent_id != plan["agent_id"]:
            raise ValidationError(f"plan {plan_id} belongs to {plan['agent_id']}")
        agent_id = plan["agent_id"] or agent_id
        if not agent_id:
            raise ValidationError("no shopping agent: set one with `earnest gifts policy --agent ...`")
        now = self.clock()
        if plan["agent_id"] is None:
            self.ledger.update_plan(plan_id, now, agent_id=agent_id)
            plan["agent_id"] = agent_id
        opts = [Option.from_dict(o) for o in (options if options is not None else self.ledger.catalog())]
        if not opts:
            raise ValidationError("no options: pass some, or load popular items with `earnest gifts catalog`")
        if len(opts) > 20:
            raise ValidationError("at most 20 options at a time")
        person, occ, on = self._plan_context(plan)
        prefs = [Preference(x["text"], x["kind"], x["source"]) for x in self.ledger.prefs(person["id"])]
        overrides = self.ledger.merchant_overrides(plan["user_id"])
        cards = self._gift_cards(plan["user_id"], agent_id)
        card_by_option = {}
        for o in opts:
            keys = self.merchants.canonical(o.merchant)
            card_by_option[o.merchant] = max([v for k, v in cards.items() if keys & self.merchants.canonical(k)],
                                             default=0.0)
        ranked = rank(opts, person=self._person_obj(person), prefs=prefs, budget=plan["budget"], on=on,
                      today=now.date(), gift_cards=card_by_option,
                      merchant_status={o.merchant: self.merchants.check(o.merchant, overrides).status for o in opts},
                      given_before=[g["title"] for g in self.ledger.gift_history(person["id"])])
        shortlist = ranked[:5]
        self.ledger.update_plan(plan_id, now, options=shortlist)
        if not any(r["usable"] for r in shortlist):
            problems = sorted({p.split(":")[0] for r in shortlist for p in r["problems"]})
            self.ledger.update_plan(plan_id, now, status="open",
                                    note=f"no option fit ({', '.join(problems) or 'weak match'}); try others")
            out = {"plan": plan_id, "status": "open", "shortlist": shortlist,
                   "note": "none of these fit; propose others"}
            stretch = [r for r in shortlist if r["problems"] and all(p.startswith("over_budget") for p in r["problems"])]
            if stretch:
                o = stretch[0]["option"]
                out["stretch"] = (f"the best fit, {o['title']} (${o['price']:.2f}), is only over budget: "
                                  f"earnest gifts budget {plan_id} {o['price']:g}  then  "
                                  f"earnest gifts approve {plan_id} --option {stretch[0]['index']}")
            return out
        return self._put_forward(plan_id, None, by_user=False)

    def _gift_mandate(self, plan: dict, option: dict, approval: str) -> str:
        person, occ, on = self._plan_context(plan)
        mandate_id = f"gift_{plan['id']}"
        self.add_mandate({
            "id": mandate_id, "user_id": plan["user_id"], "task_type": "gift_shopping", "mode": "gift",
            "intent": f"{occ.describe(person['name'], on)} gift: {option['title']}",
            "budget": {"currency": "USD", "max": plan["budget"]}, "deadline": on.isoformat(), "approval": approval,
            "secrecy": True, "ship_to": "recipient" if person["address"] else "self",
            "recipient": {"name": person["name"], "plan": plan["id"]}})
        return mandate_id

    def _put_forward(self, plan_id: str, index: int | None, by_user: bool) -> dict:
        """Turn a shortlisted option into a spend request under the plan's gift mandate. Unless
        you authorized buying for this person at this price, the mandate needs your approval, so
        the request reaches you through the usual approval links and `earnest pending`."""
        plan = self._plan(plan_id)
        policy = self.gift_policy(plan["user_id"])
        person, occ, on = self._plan_context(plan)
        usable = [r for r in plan["options"] if r["usable"]]
        if index is not None:
            pick = next((r for r in plan["options"] if r["index"] == index), None)
            if pick is None:
                raise ValidationError(f"no option {index} on plan {plan_id}; see `earnest gifts show {plan_id}`")
            if pick["problems"]:
                raise ValidationError(f"option {index} doesn't fit: {', '.join(pick['problems'])}")
            candidates = [pick]
        else:
            candidates = usable
        now = self.clock()
        if plan["request_id"]:   # switching options: withdraw the earlier request (costs the agent nothing)
            old = self.ledger.get_request(plan["request_id"])
            if old and old["status"] == "escalated":
                self.resolve(plan["request_id"], approve=False)
        tried = []
        for r in candidates:
            o = r["option"]
            authorized = (not by_user and policy["auto_buy"] and person["auto_buy_max"] is not None
                          and o["price"] <= person["auto_buy_max"])
            mandate_id = self._gift_mandate(plan, o, "auto" if (authorized or by_user) else "always")
            why = "; ".join(r["why"][:2])
            d = self.request_spend({
                "agent_id": plan["agent_id"], "mandate_id": mandate_id, "amount": o["price"], "merchant": o["merchant"],
                "category": o.get("category", "gifts"), "item": o["title"], "product_id": o.get("product_id"),
                "reason": f"Gift: {occ.describe(person['name'], on)} on {on:%b} {on.day}. {why}".strip()})
            if d.status == "escalated" and by_user:
                try:
                    d = self.resolve(d.request_id, approve=True)   # this is your approval
                except ValidationError as e:   # e.g. the agent's account can't cover it yet
                    self.ledger.update_plan(plan_id, now, chosen=r["index"], request_id=d.request_id,
                                            status="awaiting_approval", note=str(e))
                    raise
            if d.status == "denied":
                tried.append({"option": o["title"], "reasons": d.reasons})
                continue
            self.ledger.update_plan(plan_id, now, chosen=r["index"], request_id=d.request_id,
                                    status="approved" if d.status == "approved" else "awaiting_approval", note=None)
            if d.status == "approved":
                self._record_gift(plan_id, d)
            return {"plan": plan_id, "status": self._plan(plan_id)["status"], "choice": o, "why": r["why"],
                    "request_id": d.request_id, "reasons": d.reasons,
                    "alternatives": [{"index": x["index"], "title": x["option"]["title"], "price": x["option"]["price"],
                                      "merchant": x["option"]["merchant"], "score": x["score"]}
                                     for x in usable if x["index"] != r["index"]][:3],
                    "next": ("the agent can buy it now" if d.status == "approved" else
                             f"approve: earnest approve {d.request_id}   or pick another: "
                             f"earnest gifts approve {plan_id} --option N")}
        self.ledger.update_plan(plan_id, now, status="open", note="earnest turned down every option: " +
                                "; ".join(f"{t['option']} ({', '.join(t['reasons'])})" for t in tried))
        return {"plan": plan_id, "status": "open", "tried": tried, "note": "propose other options"}

    def _record_gift(self, plan_id: str, decision: Decision) -> None:
        plan = self._plan(plan_id)
        o = next(r["option"] for r in plan["options"] if r["index"] == plan["chosen"])
        self.ledger.add_gift_history(plan["user_id"], plan["person_id"], o["title"], int(plan["on_date"][:4]),
                                     o["price"], plan_id, "earnest")

    def _sync_gift_plan(self, request_id: str, decision: Decision) -> None:
        """Keep a gift plan in step when its request is approved or denied through the usual paths."""
        row = self.ledger.get_request(request_id)
        if row is None or not row["mandate_id"].startswith("gift_g_"):
            return
        plan = self.ledger.plan(row["mandate_id"][len("gift_"):])
        if plan is None or plan["request_id"] != request_id or plan["status"] != "awaiting_approval":
            return
        if decision.status == "approved":
            self.ledger.update_plan(plan["id"], self.clock(), status="approved")
            self._record_gift(plan["id"], decision)
        elif decision.status == "denied":
            self.ledger.update_plan(plan["id"], self.clock(), status="declined",
                                    note="you declined; pick another option or skip")

    def set_gift_budget(self, plan_id: str, amount: float) -> dict:
        """Change one gift's budget (yours to decide; agents can't). Re-checks the shortlist."""
        from .gifting import Option, Preference, rank
        plan = self._plan(plan_id)
        if plan["status"] not in ("open", "declined", "awaiting_approval"):
            raise ValidationError(f"plan {plan_id} is {plan['status']}")
        if amount <= 0:
            raise ValidationError("budget must be > 0")
        now = self.clock()
        self.ledger.update_plan(plan_id, now, budget=round(amount, 2))
        if plan["options"]:   # re-score what's already shortlisted against the new budget
            person, occ, on = self._plan_context(plan)
            prefs = [Preference(x["text"], x["kind"], x["source"]) for x in self.ledger.prefs(person["id"])]
            opts = [Option.from_dict(r["option"]) for r in sorted(plan["options"], key=lambda r: r["index"])]
            overrides = self.ledger.merchant_overrides(plan["user_id"])
            cards = self._gift_cards(plan["user_id"], plan["agent_id"])
            ranked = rank(opts, person=self._person_obj(person), prefs=prefs, budget=amount, on=on,
                          today=now.date(),
                          gift_cards={o.merchant: max([v for k, v in cards.items() if self.merchants.canonical(o.merchant)
                                                       & self.merchants.canonical(k)], default=0.0) for o in opts},
                          merchant_status={o.merchant: self.merchants.check(o.merchant, overrides).status for o in opts},
                          given_before=[g["title"] for g in self.ledger.gift_history(person["id"])])
            old_index = {i: r["index"] for i, r in enumerate(sorted(plan["options"], key=lambda r: r["index"]))}
            for r in ranked:
                r["index"] = old_index[r["index"]]
            self.ledger.update_plan(plan_id, now, options=ranked)
        return self.gift_plan_view(plan_id)

    def approve_gift(self, plan_id: str, option: int | None = None) -> dict:
        """Approve the suggested gift, or pick another shortlisted option instead."""
        plan = self._plan(plan_id)
        if plan["status"] not in ("awaiting_approval", "declined", "open"):
            raise ValidationError(f"plan {plan_id} is {plan['status']}")
        if not plan["options"]:
            raise ValidationError(f"plan {plan_id} has no options yet")
        if option is None and plan["status"] == "awaiting_approval" and plan["request_id"]:
            d = self.resolve(plan["request_id"], approve=True)
            return {"plan": plan_id, "status": self._plan(plan_id)["status"], "request_id": d.request_id,
                    "reasons": d.reasons}
        return self._put_forward(plan_id, option if option is not None else plan["chosen"], by_user=True)

    def skip_gift(self, plan_id: str, reason: str = "skipped by you") -> dict:
        plan = self._plan(plan_id)
        if plan["status"] in ("approved", "ordered", "done"):
            raise ValidationError(f"plan {plan_id} is already {plan['status']}")
        if plan["request_id"]:
            old = self.ledger.get_request(plan["request_id"])
            if old and old["status"] == "escalated":
                self.resolve(plan["request_id"], approve=False)
        self.ledger.update_plan(plan_id, self.clock(), status="skipped", note=reason)
        return {"plan": plan_id, "status": "skipped"}

    def gift_tasks(self, agent_id: str) -> list[dict]:
        """For the shopping agent: plans to research, and approved gifts to buy and send."""
        out = []
        for plan in self.ledger.plans_for_agent(agent_id, ("open", "approved")):
            if plan["status"] == "open":
                out.append({"todo": "find options", **self.gift_brief(plan["id"])})
            else:
                out.append({"todo": "buy and send", **self.gift_purchase(plan["id"])})
        return out

    def gift_purchase(self, plan_id: str) -> dict:
        """Everything needed to buy and send an approved gift, including the address."""
        from .gifting import ship_by
        plan = self._plan(plan_id)
        if plan["status"] != "approved":
            raise ValidationError(f"plan {plan_id} isn't approved")
        person, occ, on = self._plan_context(plan)
        o = next(r["option"] for r in plan["options"] if r["index"] == plan["chosen"])
        decision = json.loads(self.ledger.get_request(plan["request_id"])["decision"])
        n = occ.count(on)
        message = {"birthday": f"Happy {_ordinal_word(n)}birthday, {person['name']}!",
                   "anniversary": f"Happy {_ordinal_word(n)}anniversary!"}.get(occ.kind, f"Thinking of you, {person['name']}!")
        sb = ship_by(on, o.get("eta_days"), o.get("digital", False))
        return {"plan": plan_id, "for": person["name"], "occasion": occ.describe(person["name"], on),
                "date": on.isoformat(), "item": o["title"], "merchant": o["merchant"], "price": o["price"],
                "url": o.get("url"), "request_id": plan["request_id"],
                "payment": decision.get("payment"), "credential": decision.get("credential"),
                "ship_to": person["address"] or "you (no address on file for them)",
                "order_by": sb.isoformat() if sb else None,
                "buy_before": (decision.get("credential") or {}).get("expires_at"),
                "gift_message": message,
                "keep_secret": True, "after": f"call gift_ordered('{plan_id}') once the order is placed"}

    def gift_ordered(self, plan_id: str, agent_id: str | None = None, order_ref: str | None = None) -> dict:
        plan = self._plan(plan_id)
        if agent_id and plan["agent_id"] != agent_id:
            raise ValidationError(f"plan {plan_id} belongs to {plan['agent_id']}")
        if plan["status"] != "approved":
            raise ValidationError(f"plan {plan_id} is {plan['status']}, not approved")
        self.ledger.update_plan(plan_id, self.clock(), status="ordered",
                                note=f"order {order_ref}" if order_ref else "ordered")
        return {"plan": plan_id, "status": "ordered"}

    def gift_feedback(self, plan_id: str, how: str) -> dict:
        """How the gift went: loved / liked / returned / disliked. It teaches the next gift (and
        counts as your verdict on the purchase for the agent's trust)."""
        if how not in ("loved", "liked", "returned", "disliked"):
            raise ValidationError("feedback must be loved, liked, returned or disliked")
        plan = self._plan(plan_id)
        if plan["status"] not in ("approved", "ordered", "done"):
            raise ValidationError(f"plan {plan_id} was never bought")
        person, _, _ = self._plan_context(plan)
        o = next(r["option"] for r in plan["options"] if r["index"] == plan["chosen"])
        self.ledger.add_pref(plan["user_id"], person["id"], o["title"], "like" if how in ("loved", "liked") else "dislike",
                             "feedback", self.clock())
        events = []
        if not self.ledger.has_outcome(plan["request_id"]):
            events = self.report_outcome({"request_id": plan["request_id"],
                                          "verdict": "accepted" if how in ("loved", "liked") else "rejected"})
        self.ledger.update_plan(plan_id, self.clock(), status="done", feedback=how)
        return {"plan": plan_id, "status": "done", "feedback": how, "trust_events": events}

    def gift_plan_view(self, plan_id: str) -> dict:
        plan = self._plan(plan_id)
        person, occ, on = self._plan_context(plan)
        return {"plan": plan_id, "status": plan["status"], "for": person["name"],
                "occasion": occ.describe(person["name"], on), "date": on.isoformat(), "budget": plan["budget"],
                "chosen": plan["chosen"], "request_id": plan["request_id"], "note": plan["note"],
                "feedback": plan["feedback"],
                "shortlist": [{"index": r["index"], "title": r["option"]["title"], "merchant": r["option"]["merchant"],
                               "price": r["option"]["price"], "score": r["score"], "why": r["why"],
                               "problems": r["problems"]} for r in plan["options"]]}

    def load_gift_catalog(self, items: list[dict]) -> int:
        """Popular items to fall back on when no agent has researched options."""
        from .gifting import Option
        for d in items:
            o = Option.from_dict({**d, "source": d.get("source", "popular")})
            self.ledger.put_catalog(o.title, o.merchant, o.price, o.to_dict())
        return len(items)


    # --- purchase evaluation (spec section 3l) ---
    def eval_policy(self, user_id: str) -> dict:
        from .evaluation import DEFAULT_POLICY
        return {**DEFAULT_POLICY, **(self.ledger.eval_policy(user_id) or {})}

    def set_eval_policy(self, user_id: str, goal: str | None = None, weights: dict | None = None,
                        threshold: float | None = None, hourly_value: float | None = None, gate: bool | None = None,
                        net_check_over: float | None = None) -> dict:
        """Your rubric: a goal (balanced, save_money, save_time, wellbeing) or your own weights, the
        threshold a purchase must reach, what an hour of your time is worth, whether requests below
        the bar come to you (gate), and the price from which the net-value check applies."""
        from .evaluation import GOALS, normalize_weights
        policy = self.eval_policy(user_id)
        if goal is not None:
            if goal not in GOALS:
                raise ValidationError(f"goal must be one of {', '.join(GOALS)}")
            policy["goal"], policy["weights"] = goal, dict(GOALS[goal])
        if weights is not None:
            policy["goal"], policy["weights"] = "custom", normalize_weights(weights)
        if threshold is not None:
            if not 1 <= threshold <= 10:
                raise ValidationError("threshold must be between 1 and 10")
            policy["threshold"] = threshold
        if hourly_value is not None:
            if hourly_value <= 0:
                raise ValidationError("hourly value must be > 0")
            policy["hourly_value"] = hourly_value
        if net_check_over is not None:
            if net_check_over < 0:
                raise ValidationError("net check amount must be >= 0")
            policy["net_check_over"] = net_check_over
        if gate is not None:
            policy["gate"] = bool(gate)
        self.ledger.put_eval_policy(user_id, policy)
        return policy

    def _market_price(self, conf: dict | None, quantity: int, mandate: Mandate | None) -> float | None:
        m = (conf or {}).get("market")
        if m and m.get("median_price"):
            return round(m["median_price"] * max(1, quantity), 2)
        return mandate.benchmark_price if mandate and mandate.benchmark_price else None

    def _evaluate_request(self, mandate: Mandate, req: SpendRequest, conf: dict | None) -> dict | None:
        """Score the agent's value estimate. Gifts are judged by the gift rubric (3k), and standing
        restocks were pre-approved, so neither is gated; with the gate on, any other request without
        an estimate comes to you."""
        from .evaluation import EvalInputs, evaluate, gate_reasons
        policy = self.eval_policy(mandate.user_id)
        exempt = mandate.mode == "gift" or mandate.standing is not None
        if req.evaluation is None:
            if policy["gate"] and not exempt:
                return {"gate": ["eval:missing"], "result": None}
            return None
        result = evaluate(req.amount, EvalInputs.from_dict(req.evaluation), policy,
                          self._market_price(conf, req.quantity, mandate))
        result["weights"] = policy["weights"]
        gate = gate_reasons(result, policy) if policy["gate"] and not exempt else []
        return {"gate": gate, "result": result}

    @staticmethod
    def _eval_summary(evaluation: dict | None) -> dict | None:
        if not evaluation or evaluation.get("result") is None:
            return {"missing": True} if evaluation else None
        r = evaluation["result"]
        return {"total": r["total"], "threshold": r["threshold"], "passes": r["passes"], "coverage": r["coverage"],
                "net_value": r["net_value"], "scores": {m: v["score"] for m, v in r["metrics"].items()}}

    def _store_eval(self, request_id: str, evaluation: dict | None, now: datetime) -> None:
        if evaluation and evaluation.get("result") is not None:
            self.ledger.put_eval(request_id, "estimate", evaluation["result"], now)

    def score_purchase(self, user_id: str, price: float, inputs: dict, market_price: float | None = None) -> dict:
        """Score a purchase against your rubric without asking to buy anything."""
        from .evaluation import EvalInputs, evaluate, gate_reasons
        if price <= 0:
            raise ValidationError("price must be > 0")
        policy = self.eval_policy(user_id)
        result = evaluate(price, EvalInputs.from_dict(inputs), policy, market_price)
        result["would_come_to_you"] = gate_reasons(result, policy) if policy["gate"] else []
        return result

    def purchase_eval(self, request_id: str) -> dict:
        row = self.ledger.get_request(request_id)
        if row is None:
            raise ValidationError(f"unknown request: {request_id}")
        evals = self.ledger.get_evals(request_id)
        out = {"request_id": request_id, "agent_id": row["agent_id"], "merchant": row["merchant"],
               "amount": row["amount"], "item": json.loads(row["data"]).get("item"), "status": row["status"],
               "estimate": evals.get("estimate"), "review": evals.get("review")}
        if evals.get("estimate") and evals.get("review"):
            out["comparison"] = self._compare(evals["estimate"], evals["review"])
        return out

    @staticmethod
    def _compare(est: dict, rev: dict) -> dict:
        from .evaluation import METRICS
        per = {}
        for m in METRICS:
            a, b = est["metrics"][m]["score"], rev["metrics"][m]["score"]
            if a is not None and b is not None:
                per[m] = round(a - b, 1)
        gap = round(est["total"] - rev["total"], 2) if est["total"] is not None and rev["total"] is not None else None
        nv = (round(est["net_value"] - rev["net_value"], 2)
              if est["net_value"] is not None and rev["net_value"] is not None else None)
        return {"total_gap": gap, "by_metric": per, "net_value_gap": nv,
                "verdict": None if gap is None else "overrated" if gap >= 1 else "underrated" if gap <= -1 else
                "about right"}

    def review_purchase(self, request_id: str, inputs: dict) -> dict:
        """After using it: what the purchase was really worth, on the same rubric. Compared with
        the agent's estimate; a big overestimate costs it trust, a close one earns a little."""
        from .evaluation import EvalInputs, evaluate
        from .scoring import EVAL_CALIBRATED, EVAL_OVERCLAIM
        row = self.ledger.get_request(request_id)
        if row is None:
            raise ValidationError(f"unknown request: {request_id}")
        if row["status"] != "approved":
            raise ValidationError(f"request {request_id} was {row['status']}; only purchases can be reviewed")
        inp = EvalInputs.from_dict(inputs)
        if inp.is_empty():
            raise ValidationError("say something about how it went: uses, hours saved, scores...")
        now = self.clock()
        mandate = self.ledger.get_mandate(row["mandate_id"])
        conf = json.loads(row["decision"]).get("confidence")
        result = evaluate(row["amount"], inp, self.eval_policy(row["user_id"]),
                          self._market_price(conf, json.loads(row["data"]).get("quantity", 1), mandate))
        first = "review" not in self.ledger.get_evals(request_id)
        self.ledger.put_eval(request_id, "review", result, now)
        out = self.purchase_eval(request_id)
        gap = (out.get("comparison") or {}).get("total_gap")
        events = []
        if first and gap is not None:
            key = (row["user_id"], row["agent_id"], mandate.task_type)
            if gap >= 2.5:
                self.ledger.add_points(*key, "eval_overclaim", EVAL_OVERCLAIM, request_id, now)
                events.append({"kind": "eval_overclaim", "points": EVAL_OVERCLAIM})
            elif abs(gap) <= 1:
                self.ledger.add_points(*key, "eval_calibrated", EVAL_CALIBRATED, request_id, now)
                events.append({"kind": "eval_calibrated", "points": EVAL_CALIBRATED})
        out["trust_events"] = events
        return out

    def agent_eval(self, user_id: str, agent_id: str, days: int = 365) -> dict:
        """How good the agent's purchasing decisions are: its purchases on your rubric, how many met
        your bar, the net value, and how its estimates compare with your reviews."""
        from statistics import mean
        from .evaluation import METRICS, grade
        policy = self.eval_policy(user_id)
        since = (self.clock() - timedelta(days=days)).isoformat()
        bought = [p for p in self.ledger.purchases(user_id) if p["agent_id"] == agent_id and p["at"] >= since]
        rows = []
        for p in bought:
            ev = self.ledger.get_evals(p["request_id"])
            rows.append({"request_id": p["request_id"], "item": p.get("item"), "merchant": p["merchant"],
                         "amount": p["amount"], "verdict": p["verdict"], "estimate": ev.get("estimate"),
                         "review": ev.get("review")})
        est = [r for r in rows if r["estimate"] and r["estimate"]["total"] is not None]
        rev = [r for r in rows if r["review"] and r["review"]["total"] is not None]
        both = [r for r in est if r["review"] and r["review"]["total"] is not None]
        bias = round(mean(r["estimate"]["total"] - r["review"]["total"] for r in both), 2) if both else None
        by_metric = {}
        for m in METRICS:
            gaps = [r["estimate"]["metrics"][m]["score"] - r["review"]["metrics"][m]["score"] for r in both
                    if r["estimate"]["metrics"][m]["score"] is not None and r["review"]["metrics"][m]["score"] is not None]
            if gaps:
                by_metric[m] = round(mean(gaps), 2)
        judged = [r["review"]["total"] if r["review"] and r["review"]["total"] is not None else r["estimate"]["total"]
                  for r in est]
        quality = round(mean(judged), 2) if judged else None
        met = sum(t >= policy["threshold"] for t in judged)
        nv_est = [r["estimate"]["net_value"] for r in est if r["estimate"]["net_value"] is not None]
        nv_rev = [r["review"]["net_value"] for r in rev if r["review"]["net_value"] is not None]
        out = {
            "agent_id": agent_id, "days": days, "purchases": len(rows), "spent": round(sum(r["amount"] for r in rows), 2),
            "evaluated": len(est), "reviewed": len(rev),
            "average_score": {"estimated": round(mean(r["estimate"]["total"] for r in est), 2) if est else None,
                              "reviewed": round(mean(r["review"]["total"] for r in rev), 2) if rev else None},
            "met_threshold": {"count": met, "of": len(judged), "threshold": policy["threshold"]},
            "net_value": {"estimated": round(sum(nv_est), 2) if nv_est else None,
                          "reviewed": round(sum(nv_rev), 2) if nv_rev else None},
            "estimate_bias": bias, "bias_by_metric": by_metric,
            "kept": sum(r["verdict"] == "accepted" for r in rows),
            "sent_back": sum(r["verdict"] in ("rejected", "disputed") for r in rows),
            "unevaluated": [r["request_id"] for r in rows if not r["estimate"]],
            "grade": grade(quality, bias),
        }
        out["summary"] = self._eval_story(out, policy)
        out["purchases_detail"] = [{"request_id": r["request_id"], "item": r["item"], "amount": r["amount"],
                                    "estimated": r["estimate"]["total"] if r["estimate"] else None,
                                    "reviewed": r["review"]["total"] if r["review"] else None,
                                    "verdict": r["verdict"]} for r in rows]
        return out

    @staticmethod
    def _eval_story(o: dict, policy: dict) -> str:
        if not o["purchases"]:
            return f"{o['agent_id']} hasn't bought anything in {o['days']} days"
        if not o["evaluated"]:
            return f"{o['agent_id']} made {o['purchases']} purchase(s) without value estimates"
        names = {"price": "price", "time_saved": "time saved", "utility": "utility", "added_value": "added value"}
        s = (f"Grade {o['grade']}: {o['met_threshold']['count']} of {o['met_threshold']['of']} purchases met your "
             f"{policy['threshold']:g} bar")
        nv = o["net_value"]["reviewed"] if o["net_value"]["reviewed"] is not None else o["net_value"]["estimated"]
        if nv is not None:
            s += f", net value {'+' if nv >= 0 else '-'}${abs(nv):,.0f} a year"
        if o["estimate_bias"] is not None:
            if o["estimate_bias"] >= 1:
                worst = max(o["bias_by_metric"].items(), key=lambda kv: kv[1], default=None)
                s += f"; it overrates purchases by {o['estimate_bias']:.1f} points"
                if worst and worst[1] >= 1:
                    s += f", most on {names[worst[0]]} (+{worst[1]:.1f})"
            elif o["estimate_bias"] <= -1:
                s += f"; it underrates purchases by {-o['estimate_bias']:.1f} points"
            else:
                s += "; its estimates match your reviews"
        return s


def _ordinal_word(n: int | None) -> str:
    from .gifting import _ordinal
    return f"{_ordinal(n)} " if n else ""
