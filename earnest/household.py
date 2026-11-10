"""Households (spec/SPEC.md section 3g): shared budgets, kids' allowances, two-adult approvals.

A household groups users. Each member is an `adult` or a `child`.
- Children's requests always go to an adult (a guardian) and are capped by a monthly allowance.
- A household monthly budget caps everyone's combined approved spending.
- Purchases over `second_approval_over` need two different adults to approve.

Pure functions; the gateway supplies the month's spending.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Member:
    user_id: str
    role: str                      # adult | child
    monthly_allowance: float | None = None


@dataclass
class Household:
    id: str
    name: str
    members: list[Member]
    monthly_budget: float | None = None
    second_approval_over: float | None = None

    def member(self, user_id: str) -> Member | None:
        return next((m for m in self.members if m.user_id == user_id), None)

    @property
    def adults(self) -> list[str]:
        return [m.user_id for m in self.members if m.role == "adult"]

    def approvals_needed(self, user_id: str, amount: float) -> int:
        if self.second_approval_over is not None and amount > self.second_approval_over and len(self.adults) >= 2:
            return 2
        return 1

    def can_approve(self, approver: str, requester: str) -> bool:
        m = self.member(approver)
        if m is None or m.role != "adult":
            return False
        return True


def checks(h: Household, user_id: str, amount: float, member_spent_month: float,
           household_spent_month: float) -> tuple[list[str], list[str]]:
    """(deny reasons, escalate reasons) for one request."""
    deny, esc = [], []
    m = h.member(user_id)
    if m is None:
        return deny, esc
    if m.role == "child":
        esc.append("household:child_needs_guardian")
        if m.monthly_allowance is not None and member_spent_month + amount > m.monthly_allowance + 1e-9:
            deny.append("household:over_allowance")
    if h.monthly_budget is not None and household_spent_month + amount > h.monthly_budget + 1e-9:
        deny.append("household:over_monthly_budget")
    if h.approvals_needed(user_id, amount) == 2:
        esc.append("household:needs_two_adults")
    return deny, esc
