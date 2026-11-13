"""Gift concierge (spec/SPEC.md section 3k): remember the people you care about and their dates,
and have a gift ready, within budget, before each one.

- People: who they are to you, what they like and dislike, sizes, a per-gift budget, and
  whether the agent may buy for them without asking (and up to how much).
- Occasions: birthdays, anniversaries and other yearly dates, from you, a contacts CSV or a
  calendar (.ics) export.
- Preferences carry their source, because a wishlist item they asked for counts for more than
  a guess: `wishlist` > `feedback` (how a past gift went) > `you` > `conversation` > `social` >
  `popular`. earnest doesn't scrape
  anyone's accounts: preferences come from you, from what an agent learned in your own
  conversations, or from wishlists and public posts you point it to.
- Options (from an agent's research or a catalog of popular items) are scored on how well they
  fit the person, the budget, the date, what they've been given before, and whether money is
  already set aside for that store (a linked gift card).

Pure functions; the gateway supplies people, dates and funding.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from .history import item_tokens
from .models import ValidationError

KINDS = ("birthday", "anniversary", "holiday", "other")
SOURCES = {"wishlist": 1.0, "feedback": 0.9, "you": 0.8, "conversation": 0.6, "social": 0.5, "popular": 0.3}
RELATIONSHIP_BUDGETS = {  # general guidance when you haven't set a budget for someone
    "partner": 150.0, "spouse": 150.0, "parent": 100.0, "child": 75.0, "grandparent": 75.0,
    "sibling": 60.0, "friend": 40.0, "colleague": 25.0, "other": 30.0,
}
DEFAULT_LEAD_DAYS = 14        # start planning this long before the date
SHIP_BUFFER_DAYS = 1          # an option must arrive at least this long before the date
MILESTONE_FACTOR = 1.5        # 30th, 40th... birthdays and 5th, 10th... anniversaries
MIN_SCORE = 0.35              # options below this aren't suggested


@dataclass
class Person:
    id: str
    name: str
    relationship: str = "other"
    sizes: dict = field(default_factory=dict)
    notes: str | None = None
    budget: float | None = None          # per gift; overrides the relationship guidance
    auto_buy_max: float | None = None    # may buy without asking up to this; None = always ask
    address: str | None = None           # only shown to the agent once a gift is approved


@dataclass
class Occasion:
    id: str
    person_id: str
    kind: str
    month: int
    day: int
    year: int | None = None              # birth year / wedding year, for milestones
    label: str | None = None

    def next_on(self, today: date) -> date:
        """The next time it falls on or after `today` (Feb 29 falls on Feb 28 in other years)."""
        for y in (today.year, today.year + 1):
            d = _safe_date(y, self.month, self.day)
            if d >= today:
                return d
        raise AssertionError("unreachable")

    def count(self, on: date) -> int | None:
        """Which birthday / anniversary this is, when the year is known."""
        return on.year - self.year if self.year else None

    def describe(self, person_name: str, on: date) -> str:
        n = self.count(on)
        if self.kind == "birthday":
            return f"{person_name}'s {_ordinal(n) + ' ' if n else ''}birthday"
        if self.kind == "anniversary":
            return f"{_ordinal(n) + ' ' if n else ''}anniversary with {person_name}"
        return self.label or f"{person_name}: {self.kind}"


@dataclass
class Preference:
    text: str
    kind: str          # like | dislike | wish
    source: str        # see SOURCES

    @property
    def weight(self) -> float:
        return SOURCES.get(self.source, 0.3)


@dataclass
class Option:
    title: str
    merchant: str
    price: float
    category: str = "gifts"
    url: str | None = None
    product_id: str | None = None
    eta_days: int | None = None          # delivery time; None = unknown
    rating: float | None = None          # 0-5
    popularity: float | None = None      # 0-1, e.g. a bestseller rank turned into a share
    digital: bool = False                # e-gift, experience, subscription: no shipping
    source: str = "agent"

    FIELDS = {"title", "merchant", "price", "category", "url", "product_id", "eta_days", "rating", "popularity",
              "digital", "source"}

    @classmethod
    def from_dict(cls, d: dict) -> "Option":
        if not isinstance(d, dict):
            raise ValidationError("each option must be an object")
        extra = set(d) - cls.FIELDS
        if extra:
            raise ValidationError(f"unknown option field(s): {sorted(extra)}")
        try:
            price = float(d["price"])
        except (KeyError, TypeError, ValueError):
            raise ValidationError("option price must be a number") from None
        if not d.get("title") or not d.get("merchant") or price <= 0:
            raise ValidationError("each option needs a title, a merchant and a price > 0")
        eta = d.get("eta_days")
        return cls(str(d["title"]), str(d["merchant"]), round(price, 2), str(d.get("category") or "gifts"),
                   d.get("url"), d.get("product_id"), int(eta) if eta is not None else None,
                   float(d["rating"]) if d.get("rating") is not None else None,
                   float(d["popularity"]) if d.get("popularity") is not None else None,
                   bool(d.get("digital", False)), str(d.get("source") or "agent"))

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in sorted(self.FIELDS) if getattr(self, k) not in (None, False)}


def _safe_date(y: int, m: int, d: int) -> date:
    try:
        return date(y, m, d)
    except ValueError:
        if m == 2 and d == 29:
            return date(y, 2, 28)
        raise


def _ordinal(n: int | None) -> str:
    if not n:
        return ""
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def is_milestone(occ: Occasion, on: date) -> bool:
    n = occ.count(on)
    if not n:
        return False
    if occ.kind == "birthday":
        return n in (1, 13, 16, 18, 21) or n % 10 == 0
    if occ.kind == "anniversary":
        return n == 1 or n % 5 == 0
    return False


def budget_for(person: Person, occ: Occasion, on: date, defaults: dict[str, float] | None = None,
               remaining_this_year: float | None = None) -> tuple[float, list[str]]:
    """The budget for one gift and why: your per-person budget, or the guidance for the
    relationship; more for a milestone; never more than what's left of the yearly gift budget."""
    defaults = {**RELATIONSHIP_BUDGETS, **(defaults or {})}
    why = []
    if person.budget is not None:
        b = person.budget
        why.append(f"your budget for {person.name}")
    else:
        b = defaults.get(person.relationship, defaults["other"])
        why.append(f"guidance for a {person.relationship}")
    if is_milestone(occ, on):
        b *= MILESTONE_FACTOR
        why.append(f"milestone ({_ordinal(occ.count(on))})")
    if remaining_this_year is not None and b > remaining_this_year:
        b = max(0.0, remaining_this_year)
        why.append("limited by what's left of this year's gift budget")
    return round(b, 2), why


def score(option: Option, person: Person, prefs: list[Preference], budget: float, on: date, today: date,
          *, merchant_status: str = "unknown", gift_card_balance: float = 0.0,
          given_before: list[str] | None = None) -> tuple[float, list[str], list[str]]:
    """How well an option fits. Returns (score 0-1, reasons for, problems). Any problem in
    `blocking` form ('over_budget', 'disliked', 'arrives_late', 'already_given', 'untrusted_merchant')
    rules the option out."""
    toks = item_tokens(f"{option.title} {option.category}")
    reasons: list[str] = []
    problems: list[str] = []

    # 1. the person (0.45): their wishes and likes, weighted by where we learned them
    fit = 0.0
    for p in prefs:
        ptoks = item_tokens(p.text)
        if not ptoks:
            continue
        overlap = len(toks & ptoks) / len(ptoks)
        if p.kind == "dislike" and overlap >= 0.5:
            problems.append(f"disliked:{p.text}")
        elif p.kind in ("like", "wish") and overlap > 0:
            strength = overlap * p.weight * (1.0 if p.kind == "wish" else 0.8)
            if strength > fit:
                fit = strength
                label = "on their wishlist" if p.kind == "wish" and p.source == "wishlist" else \
                        f"{'wants' if p.kind == 'wish' else 'likes'} {p.text} ({p.source})"
                reasons[:] = [r for r in reasons if not r.startswith(("likes ", "wants ", "on their"))] + [label]
    fit = min(1.0, fit)

    # 2. the budget (0.2): within budget, and not so cheap it looks like an afterthought
    if option.price > budget + 0.01:
        problems.append(f"over_budget:{option.price:.2f}>{budget:.2f}")
        bfit = 0.0
    else:
        share = option.price / budget if budget else 1.0
        bfit = 1.0 if share >= 0.6 else 0.5 + share / 1.2
        if share >= 0.6:
            reasons.append("uses the budget well")

    # 3. timing (0.1): it has to arrive before the day
    days_left = (on - today).days
    if option.digital:
        tfit = 1.0
    elif option.eta_days is None:
        tfit = 0.5
        reasons.append("delivery time unknown")
    elif option.eta_days > days_left - SHIP_BUFFER_DAYS:
        problems.append(f"arrives_late:{option.eta_days}d>{max(0, days_left - SHIP_BUFFER_DAYS)}d")
        tfit = 0.0
    else:
        tfit = 1.0

    # 4. quality (0.15): ratings and popularity, so a guess is at least a well-liked guess
    q = []
    if option.rating is not None:
        q.append(max(0.0, min(1.0, (option.rating - 3.0) / 2.0)))
    if option.popularity is not None:
        q.append(max(0.0, min(1.0, option.popularity)))
    qfit = sum(q) / len(q) if q else 0.4

    # 5. money already set aside (0.1): a gift card for this store
    ffit = 1.0 if gift_card_balance >= option.price else 0.0
    if ffit:
        reasons.append("paid from a gift card you linked")

    for given in given_before or []:
        gtoks = item_tokens(given)
        if gtoks and len(toks & gtoks) / len(gtoks) >= 0.8:
            problems.append(f"already_given:{given}")
            break
    if merchant_status == "untrusted":
        problems.append("untrusted_merchant")
    elif merchant_status == "suspicious":
        qfit *= 0.5
        reasons.append("store looks suspicious: you'll be asked")

    total = 0.45 * fit + 0.2 * bfit + 0.1 * tfit + 0.15 * qfit + 0.1 * ffit
    if not fit and not prefs:
        reasons.append("no preferences known yet: picked on popularity")
    return round(total, 3), reasons, problems


def rank(options: list[Option], **kw) -> list[dict]:
    """Score every option; usable ones first, best first."""
    out = []
    for i, o in enumerate(options):
        balance = kw.get("gift_cards", {}).get(o.merchant, 0.0)
        status = kw.get("merchant_status", {}).get(o.merchant, "unknown")
        s, reasons, problems = score(o, kw["person"], kw["prefs"], kw["budget"], kw["on"], kw["today"],
                                     merchant_status=status, gift_card_balance=balance,
                                     given_before=kw.get("given_before"))
        out.append({"option": o.to_dict(), "score": s, "why": reasons, "problems": problems,
                    "usable": not problems and s >= MIN_SCORE, "index": i})
    out.sort(key=lambda r: (not r["usable"], -r["score"], r["option"]["price"]))
    return out


# --- importing people and dates ---

_DATE = re.compile(r"^(?:(\d{4})-)?(\d{1,2})-(\d{1,2})$")


def parse_md(value: str) -> tuple[int | None, int, int]:
    """'1990-03-14' or '03-14' -> (year or None, month, day)."""
    m = _DATE.match(value.strip())
    if not m:
        raise ValidationError(f"date must be YYYY-MM-DD or MM-DD, not {value!r}")
    y, mo, d = (int(m.group(1)) if m.group(1) else None), int(m.group(2)), int(m.group(3))
    try:
        _safe_date(y or 2024, mo, d)   # 2024: a leap year, so 02-29 is accepted
    except ValueError:
        raise ValidationError(f"{value!r} isn't a real date") from None
    return y, mo, d


def parse_people_csv(text: str) -> list[dict]:
    """Columns: name, relationship, birthday, anniversary, likes, dislikes, budget, auto_buy_max.
    likes / dislikes are separated by ';'. Dates are YYYY-MM-DD or MM-DD."""
    rows = []
    for i, r in enumerate(csv.DictReader(io.StringIO(text)), start=2):
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        if not r.get("name"):
            raise ValidationError(f"row {i}: name is required")
        out: dict = {"name": r["name"], "relationship": (r.get("relationship") or "other").lower(),
                     "occasions": [], "likes": [], "dislikes": []}
        for kind in ("birthday", "anniversary"):
            if r.get(kind):
                out["occasions"].append((kind, *parse_md(r[kind])))
        for k in ("likes", "dislikes"):
            out[k] = [x.strip() for x in r.get(k, "").split(";") if x.strip()]
        for k in ("budget", "auto_buy_max"):
            if r.get(k):
                try:
                    out[k] = float(r[k])
                except ValueError:
                    raise ValidationError(f"row {i}: {k} must be a number") from None
        rows.append(out)
    return rows


_ICS_KIND = [("anniversary", re.compile(r"\banniversary\b", re.I)),
             ("birthday", re.compile(r"\b(birthday|bday|b-day)\b", re.I))]


def _ics_name(summary: str, kind: str) -> str | None:
    s = re.sub(r"\b(birthday|bday|b-day|anniversary|happy|wedding)\b", " ", summary, flags=re.I)
    s = re.sub(r"['’]s\b", "", s)
    s = re.sub(r"[^\w&+ -]", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -:")
    return s or None


def parse_ics(text: str) -> list[dict]:
    """Yearly birthdays and anniversaries from a calendar export (.ics), e.g. Google Calendar's
    birthdays calendar or events like "Sam's birthday". Other events are ignored."""
    text = re.sub(r"\r?\n[ \t]", "", text)   # unfold long lines
    out = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", text, flags=re.S):
        fields = {}
        for line in block.strip().splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.split(";")[0].upper()] = v.strip()
        summary = fields.get("SUMMARY", "")
        kind = next((k for k, rx in _ICS_KIND if rx.search(summary)), None)
        start = re.match(r"(\d{4})(\d{2})(\d{2})", fields.get("DTSTART", ""))
        if not kind or not start:
            continue
        name = _ics_name(summary, kind)
        if not name:
            continue
        y, m, d = (int(x) for x in start.groups())
        try:
            _safe_date(y, m, d)
        except ValueError:
            continue   # a malformed date: skip the event, not the whole calendar
        yearly = "FREQ=YEARLY" in fields.get("RRULE", "").upper()
        # a recurring event's first year is usually the real year; a one-off is just this year's date
        out.append({"name": name, "kind": kind, "year": y if yearly and y > 1900 else None, "month": m, "day": d})
    return out


def upcoming(occasions: list[Occasion], today: date, days: int) -> list[tuple[Occasion, date]]:
    out = [(o, o.next_on(today)) for o in occasions]
    return sorted([(o, d) for o, d in out if (d - today).days <= days], key=lambda x: x[1])


def ship_by(on: date, eta_days: int | None, digital: bool) -> date | None:
    if digital or eta_days is None:
        return None
    return on - timedelta(days=eta_days + SHIP_BUFFER_DAYS)
