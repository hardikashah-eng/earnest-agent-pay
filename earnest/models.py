"""Data objects from the earnest spec (see spec/*.schema.json).

Validation is hand-written so the core has no dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


class ValidationError(ValueError):
    """Raised when input doesn't match the spec."""


def _number(d: dict, key: str, *, default=None, lo=None, hi=None, required=False):
    if key not in d or d[key] is None:
        if required:
            raise ValidationError(f"missing field: {key}")
        return default
    v = d[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValidationError(f"{key} must be a number")
    if lo is not None and v < lo:
        raise ValidationError(f"{key} must be >= {lo}")
    if hi is not None and v > hi:
        raise ValidationError(f"{key} must be <= {hi}")
    return float(v)


def _string(d: dict, key: str, *, default=None, required=False, choices=None):
    if key not in d or d[key] is None:
        if required:
            raise ValidationError(f"missing field: {key}")
        return default
    v = d[key]
    if not isinstance(v, str) or (required and not v):
        raise ValidationError(f"{key} must be a non-empty string")
    if choices is not None and v not in choices:
        raise ValidationError(f"{key} must be one of {sorted(choices)}")
    return v


def _bool(d: dict, key: str, default=False):
    v = d.get(key, default)
    if not isinstance(v, bool):
        raise ValidationError(f"{key} must be true or false")
    return v


def _strings(d: dict, key: str, *, choices=None):
    v = d.get(key) or []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ValidationError(f"{key} must be a list of strings")
    if choices is not None:
        bad = [x for x in v if x not in choices]
        if bad:
            raise ValidationError(f"{key} has unknown values: {bad}")
    return list(v)


def _no_extra(d: dict, allowed: set[str], what: str):
    if not isinstance(d, dict):
        raise ValidationError(f"{what} must be an object")
    extra = set(d) - allowed
    if extra:
        raise ValidationError(f"{what} has unknown fields: {sorted(extra)}")


HARD_RULES = {"no_subscriptions", "no_new_merchants", "no_international"}


@dataclass
class Profile:
    user_id: str
    price_sensitivity: float = 0.5
    autonomy: float = 0.5
    novelty: float = 0.5
    privacy: float = 0.5
    explanation_style: str = "plain"
    hard_rules: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        _no_extra(d, set(cls.__dataclass_fields__), "profile")
        return cls(
            user_id=_string(d, "user_id", required=True),
            price_sensitivity=_number(d, "price_sensitivity", default=0.5, lo=0, hi=1),
            autonomy=_number(d, "autonomy", default=0.5, lo=0, hi=1),
            novelty=_number(d, "novelty", default=0.5, lo=0, hi=1),
            privacy=_number(d, "privacy", default=0.5, lo=0, hi=1),
            explanation_style=_string(d, "explanation_style", default="plain",
                                      choices={"plain", "brief_technical", "detailed"}),
            hard_rules=_strings(d, "hard_rules", choices=HARD_RULES),
        )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Mandate:
    id: str
    user_id: str
    intent: str
    task_type: str
    budget_max: float
    budget_min: float | None = None
    currency: str = "USD"
    mode: str = "personal"
    allowed_categories: list[str] = field(default_factory=list)
    blocked_merchants: list[str] = field(default_factory=list)
    deadline: date | None = None
    approval: str = "auto"
    secrecy: bool = False
    ship_to: str = "self"
    recipient: dict[str, Any] | None = None
    benchmark_price: float | None = None

    FIELDS = {"id", "user_id", "intent", "task_type", "mode", "budget", "allowed_categories",
              "blocked_merchants", "deadline", "approval", "secrecy", "ship_to", "recipient",
              "benchmark_price"}

    @classmethod
    def from_dict(cls, d: dict) -> "Mandate":
        _no_extra(d, cls.FIELDS, "mandate")
        budget = d.get("budget")
        if budget is None:
            raise ValidationError("missing field: budget")
        _no_extra(budget, {"currency", "min", "max"}, "budget")
        bmax = _number(budget, "max", lo=0, required=True)
        bmin = _number(budget, "min", lo=0)
        if bmin is not None and bmin > bmax:
            raise ValidationError("budget.min must be <= budget.max")
        deadline = d.get("deadline")
        if deadline is not None:
            try:
                deadline = date.fromisoformat(deadline)
            except (TypeError, ValueError):
                raise ValidationError("deadline must be a date like 2026-11-14") from None
        recipient = d.get("recipient")
        if recipient is not None and not isinstance(recipient, dict):
            raise ValidationError("recipient must be an object")
        return cls(
            id=_string(d, "id", required=True),
            user_id=_string(d, "user_id", required=True),
            intent=_string(d, "intent", required=True),
            task_type=_string(d, "task_type", required=True),
            budget_max=bmax,
            budget_min=bmin,
            currency=_string(budget, "currency", default="USD"),
            mode=_string(d, "mode", default="personal", choices={"personal", "work", "gift"}),
            allowed_categories=_strings(d, "allowed_categories"),
            blocked_merchants=_strings(d, "blocked_merchants"),
            deadline=deadline,
            approval=_string(d, "approval", default="auto", choices={"auto", "always", "shortlist"}),
            secrecy=_bool(d, "secrecy"),
            ship_to=_string(d, "ship_to", default="self", choices={"self", "recipient"}),
            recipient=recipient,
            benchmark_price=_number(d, "benchmark_price", lo=0),
        )

    def to_dict(self) -> dict:
        budget: dict[str, Any] = {"currency": self.currency, "max": self.budget_max}
        if self.budget_min is not None:
            budget["min"] = self.budget_min
        out: dict[str, Any] = {
            "id": self.id, "user_id": self.user_id, "intent": self.intent,
            "task_type": self.task_type, "mode": self.mode, "budget": budget,
            "allowed_categories": self.allowed_categories,
            "blocked_merchants": self.blocked_merchants,
            "approval": self.approval, "secrecy": self.secrecy, "ship_to": self.ship_to,
        }
        if self.deadline is not None:
            out["deadline"] = self.deadline.isoformat()
        if self.recipient is not None:
            out["recipient"] = self.recipient
        if self.benchmark_price is not None:
            out["benchmark_price"] = self.benchmark_price
        return out


@dataclass
class SpendRequest:
    agent_id: str
    mandate_id: str
    amount: float
    merchant: str
    category: str
    reason: str
    recurring: bool = False
    international: bool = False
    ask_first: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "SpendRequest":
        _no_extra(d, set(cls.__dataclass_fields__), "spend request")
        amount = _number(d, "amount", required=True)
        if amount <= 0:
            raise ValidationError("amount must be > 0")
        return cls(
            agent_id=_string(d, "agent_id", required=True),
            mandate_id=_string(d, "mandate_id", required=True),
            amount=amount,
            merchant=_string(d, "merchant", required=True),
            category=_string(d, "category", required=True),
            reason=_string(d, "reason", required=True),
            recurring=_bool(d, "recurring"),
            international=_bool(d, "international"),
            ask_first=_bool(d, "ask_first"),
        )

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Outcome:
    request_id: str
    verdict: str
    on_time: bool = True
    note: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "Outcome":
        _no_extra(d, set(cls.__dataclass_fields__), "outcome")
        return cls(
            request_id=_string(d, "request_id", required=True),
            verdict=_string(d, "verdict", required=True, choices={"accepted", "rejected", "disputed"}),
            on_time=_bool(d, "on_time", default=True),
            note=_string(d, "note"),
        )

    def to_dict(self) -> dict:
        out = {"request_id": self.request_id, "verdict": self.verdict, "on_time": self.on_time}
        if self.note is not None:
            out["note"] = self.note
        return out


@dataclass
class Credential:
    id: str
    cap: float
    currency: str
    merchant_lock: str
    expires_at: datetime
    single_use: bool
    backend: str

    def to_dict(self) -> dict:
        return {
            "id": self.id, "cap": self.cap, "currency": self.currency,
            "merchant_lock": self.merchant_lock, "expires_at": self.expires_at.isoformat(),
            "single_use": self.single_use, "backend": self.backend,
        }


@dataclass
class Decision:
    request_id: str
    status: str  # approved | denied | escalated
    reasons: list[str]
    tier: str
    credential: Credential | None = None

    def to_dict(self) -> dict:
        out: dict[str, Any] = {"request_id": self.request_id, "status": self.status,
                               "reasons": self.reasons, "tier": self.tier}
        if self.credential is not None:
            out["credential"] = self.credential.to_dict()
        return out
