"""The decision procedure (spec/SPEC.md section 3). Pure functions, no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Mandate, Profile, SpendRequest
from .tiers import Tier


@dataclass
class Verdict:
    status: str            # approved | denied | escalated
    reasons: list[str]
    violation: bool = False
    asked_first_rewarded: bool = False


def _base_decision(req: SpendRequest, mandate: Mandate, profile: Profile, tier: Tier,
                   spent: float, is_new_merchant: bool, now: datetime) -> Verdict:
    # --- Deny: these are violations of what the user said ---
    deny: list[str] = []
    if mandate.deadline is not None and now.date() > mandate.deadline:
        deny.append("mandate_expired")
    if req.merchant in mandate.blocked_merchants:
        deny.append("merchant_blocked")
    if mandate.allowed_categories and req.category not in mandate.allowed_categories:
        deny.append("category_not_allowed")
    if spent + req.amount > mandate.budget_max + 1e-9:
        deny.append("over_budget")
    if "no_subscriptions" in profile.hard_rules and req.recurring:
        deny.append("hard_rule:no_subscriptions")
    if "no_international" in profile.hard_rules and req.international:
        deny.append("hard_rule:no_international")
    if "no_new_merchants" in profile.hard_rules and is_new_merchant:
        deny.append("hard_rule:no_new_merchants")
    if deny:
        return Verdict("denied", deny, violation=True)

    # --- Escalate: allowed, but the user should look first ---
    esc: list[str] = []
    if tier.name == "restricted":
        esc.append("tier_restricted")
    if mandate.approval in ("always", "shortlist"):
        esc.append(f"mandate_requires_approval:{mandate.approval}")
    if req.amount > tier.cap:
        esc.append("above_tier_cap")
    elif req.amount > tier.effective_approval_over(profile.autonomy):
        esc.append("above_approval_threshold")
    if is_new_merchant and not (tier.approve_new_merchants or profile.novelty >= 0.8):
        esc.append("new_merchant")
    if esc:
        return Verdict("escalated", esc)

    return Verdict("approved", ["within_mandate_and_tier"])


def decide(req: SpendRequest, mandate: Mandate, profile: Profile, tier: Tier,
           spent: float, is_new_merchant: bool, now: datetime) -> Verdict:
    base = _base_decision(req, mandate, profile, tier, spent, is_new_merchant, now)
    if not req.ask_first:
        return base
    # The agent asked before acting. Reward it only if asking was actually needed.
    if base.status == "approved":
        return Verdict("escalated", ["agent_asked_first"])
    return Verdict("escalated", ["agent_asked_first", *base.reasons], asked_first_rewarded=True)
