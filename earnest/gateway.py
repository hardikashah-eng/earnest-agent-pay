"""The gateway: ties together ledger, rules, scoring and a payment backend."""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Callable

from . import ap2
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
        # approval-link targets other than spend requests, by id prefix: prefix -> (resolve, describe)
        self.token_handlers: dict[str, tuple] = {}
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

    def _past_purchases(self, user_id: str) -> list[PastPurchase]:
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
               product_id: str | None = None):
        """Everything known about a prospective purchase.
        Returns (MerchantCheck, History, is_known_merchant, confidence dict)."""
        check = self.merchants.check(merchant, self.ledger.merchant_overrides(user_id))
        keys = self.merchants.canonical(merchant)
        self._apply_reputation(check, merchant)
        past = self._past_purchases(user_id)
        hist = summarize(keys, past, item, self.clock().date(), self._interests(user_id),
                         self._memberships_at(user_id, keys), product_id)
        known = bool(hist.memberships) or any(keys & p.merchant_keys for p in past)
        extra = []
        if category and self.merchants.high_risk_category(category):
            extra.append(f"high_risk_category:{category}")
        rw = return_window(check.merchant_id, category, hist.memberships)
        extra.append(f"returnable:{'open_ended' if rw.open_ended else rw.days}" if rw.known
                     else "return_policy_unknown")
        intent_match = bool(item and mandate and item_tokens(item) & item_tokens(mandate.intent))
        today = self.clock().date()
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
            spent=self.ledger.spent_on_mandate(mandate.id) if mandate else 0.0,
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
        verdict = decide(
            req, mandate, profile, t,
            spent=spent,
            is_new_merchant=not bought_here,
            now=now,
            merchant=check, history=hist,
            high_risk_category=self.merchants.high_risk_category(req.category),
            confidence=conf, extra_deny=extra_deny, extra_escalate=extra_esc,
        )
        decision = Decision(request_id, verdict.status, verdict.reasons, t.name, confidence=conf)
        if verdict.status == "approved":
            decision.credential = self._issue(req, mandate, profile, t, now, request_id)

        self.ledger.add_request(request_id, mandate.user_id, mandate.task_type, req,
                                decision.status, decision.to_dict(), now)

        if decision.status == "escalated" and self.notifier is not None:
            self._notify(request_id, req, decision)

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
            return decision

        profile = self.profile(mandate.user_id)
        decision = Decision(request_id, "approved", [*prior, "user_approved"], t.name,
                            self._issue(req, mandate, profile, t, now, request_id),
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
    def _issue(self, req: SpendRequest, mandate: Mandate, profile: Profile, t: Tier, now: datetime,
               request_id: str):
        cred = self.backend.issue(
            cap=req.amount, currency=mandate.currency, merchant=req.merchant,
            lifetime_hours=t.lifetime_hours, single_use=t.single_use or profile.privacy >= 0.8, now=now,
            item=req.item, product_id=req.product_id)
        self.ledger.link_credential(cred.id, request_id)
        return cred

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

    def resolve_token(self, token: str):
        request_id, action, approver = read_token(self.ledger.secret("approvals"), token,
                                                  self.clock().timestamp(), with_approver=True)
        for prefix, (resolve, _) in self.token_handlers.items():
            if request_id.startswith(prefix):
                return resolve(request_id, action == "approve", approver)
        return self.resolve(request_id, approve=action == "approve", approver=approver)

    def describe_pending(self, request_id: str) -> dict | None:
        """What an approval link is about, or None if it's no longer waiting."""
        for prefix, (_, describe) in self.token_handlers.items():
            if request_id.startswith(prefix):
                return describe(request_id)
        row = self.ledger.get_request(request_id)
        if row is None:
            return None
        return next((p for p in self.pending(row["user_id"]) if p["request_id"] == request_id), None)

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
