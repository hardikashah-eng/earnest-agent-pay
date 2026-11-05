"""Parse card statement CSV exports and clean up merchant names.

Bank exports differ. We accept the common shapes:
- one signed `Amount` column (charges may be positive or negative; we detect which)
- separate `Debit` / `Credit` columns
and the usual date / description header names.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass
from datetime import date, datetime

from ..models import ValidationError

DATE_COLS = ("transaction date", "trans. date", "date", "posted date", "post date", "posting date")
DESC_COLS = ("description", "merchant", "name", "payee", "details")
AMOUNT_COLS = ("amount", "transaction amount")
DEBIT_COLS = ("debit", "charge", "withdrawal")
CREDIT_COLS = ("credit", "payment", "deposit")
DATE_FORMATS = ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%d %b %Y", "%b %d, %Y")

# Rows that move money between your own accounts, not spending.
NOT_SPENDING = re.compile(r"PAYMENT\s*(-|–)?\s*THANK|AUTOPAY PAYMENT|ONLINE PAYMENT|TRANSFER (TO|FROM)", re.I)


@dataclass(frozen=True)
class Transaction:
    id: str
    date: date
    description: str
    merchant: str
    category: str
    amount: float  # positive = money spent


# ------------------------------------------------------------------ merchants

PREFIXES = re.compile(r"^(SQ \*|SQ\*|TST\* ?|PAYPAL \*|PP\*|SP \*|GOOGLE \*|APPLE\.COM/BILL)\s*", re.I)

# (pattern on raw description, clean name, category)
KNOWN: list[tuple[str, str, str]] = [
    (r"WHOLEFDS|WHOLE FOODS", "Whole Foods", "groceries"),
    (r"TRADER JOE", "Trader Joe's", "groceries"),
    (r"\bALDI\b", "Aldi", "groceries"),
    (r"COSTCO", "Costco", "groceries"),
    (r"KROGER", "Kroger", "groceries"),
    (r"SAFEWAY", "Safeway", "groceries"),
    (r"INSTACART", "Instacart", "delivery"),
    (r"DOORDASH|DD \*", "DoorDash", "delivery"),
    (r"UBER\s*\*?\s*EATS", "Uber Eats", "delivery"),
    (r"GRUBHUB", "Grubhub", "delivery"),
    (r"UBER(?!\s*\*?\s*EATS)", "Uber", "transport"),
    (r"LYFT", "Lyft", "transport"),
    (r"NETFLIX", "Netflix", "streaming"),
    (r"HULU", "Hulu", "streaming"),
    (r"DISNEY\s*PLUS|DISNEYPLUS", "Disney+", "streaming"),
    (r"HBO\s*MAX|\bMAX\.COM", "Max", "streaming"),
    (r"SPOTIFY", "Spotify", "streaming"),
    (r"YOUTUBE\s*PREMIUM|YOUTUBEPREMIUM", "YouTube Premium", "streaming"),
    (r"AMZN MKTP|AMAZON\.COM|AMZN\.COM|AMAZON MARKETPLACE", "Amazon", "shopping"),
    (r"PRIME VIDEO|AMAZON PRIME|AMZN PRIME", "Amazon Prime", "streaming"),
    (r"TARGET", "Target", "shopping"),
    (r"WALMART|WM SUPERCENTER", "Walmart", "shopping"),
    (r"STARBUCKS", "Starbucks", "coffee"),
    (r"PLANET FITNESS|EQUINOX|24 HOUR FITNESS|\bGYM\b", None, "fitness"),
    (r"SHELL|CHEVRON|EXXON|\bBP\b|ARCO", None, "fuel"),
    (r"\bFEE\b|INTEREST CHARGE|OVERDRAFT|FOREIGN TRANS", None, "fees"),
]
_KNOWN = [(re.compile(p, re.I), name, cat) for p, name, cat in KNOWN]


def clean_merchant(description: str) -> tuple[str, str]:
    """Raw statement text -> (merchant name, category)."""
    raw = description.strip()
    for rx, name, cat in _KNOWN:
        if rx.search(raw):
            return (name or _tidy(raw)), cat
    return _tidy(raw), "other"


def _tidy(raw: str) -> str:
    s = PREFIXES.sub("", raw)
    s = re.sub(r"#\s*\d+", " ", s)               # store numbers
    s = re.sub(r"\b\d{3,}\b", " ", s)            # long digit runs (ids, phone numbers)
    s = re.sub(r"\s+[A-Z]{2}\s*$", "", s)        # trailing state code
    s = re.sub(r"[*]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s).strip(" -.,")
    return s.title() if s.isupper() else s


# ------------------------------------------------------------------ parsing

def _pick(header: dict[str, str], options: tuple[str, ...]) -> str | None:
    for opt in options:
        if opt in header:
            return header[opt]
    return None


def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    raise ValidationError(f"unrecognized date: {s!r}")


def _parse_amount(s: str | None) -> float | None:
    if s is None:
        return None
    s = s.strip().replace("$", "").replace(",", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    v = float(s)
    return -v if neg else v


def transaction_id(user_id: str, d: date, description: str, amount: float, occurrence: int) -> str:
    """Stable id so re-importing the same or an overlapping export doesn't double count.
    `occurrence` separates genuine repeats (two identical coffees on one day)."""
    key = f"{user_id}|{d.isoformat()}|{description.strip().upper()}|{amount:.2f}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def parse_csv(text: str, user_id: str) -> list[Transaction]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if not reader.fieldnames:
        raise ValidationError("empty CSV")
    header = {h.strip().lower(): h for h in reader.fieldnames if h}
    date_col, desc_col = _pick(header, DATE_COLS), _pick(header, DESC_COLS)
    amt_col = _pick(header, AMOUNT_COLS)
    debit_col, credit_col = _pick(header, DEBIT_COLS), _pick(header, CREDIT_COLS)
    if not date_col or not desc_col or not (amt_col or debit_col):
        raise ValidationError(f"couldn't find date/description/amount columns in {reader.fieldnames}")

    rows = []
    for row in reader:
        if not (row.get(date_col) or "").strip():
            continue
        desc = (row.get(desc_col) or "").strip()
        if amt_col:
            amount = _parse_amount(row.get(amt_col))
        else:
            debit, credit = _parse_amount(row.get(debit_col)), _parse_amount(row.get(credit_col))
            amount = abs(debit) if debit else (-abs(credit) if credit else None)
        if amount is None:
            continue
        rows.append((_parse_date(row[date_col]), desc, amount))

    # With a single signed column, some banks show charges as negative. Detect by majority,
    # ignoring payments to the card (which have the opposite sign to charges).
    if amt_col:
        spend_rows = [a for _, desc, a in rows if not NOT_SPENDING.search(desc)]
        if spend_rows and sum(a < 0 for a in spend_rows) > len(spend_rows) / 2:
            rows = [(d, desc, -a) for d, desc, a in rows]

    out: list[Transaction] = []
    seen: dict[tuple, int] = {}
    for d, desc, amount in rows:
        if NOT_SPENDING.search(desc) or amount <= 0:
            continue  # card payments, refunds and credits aren't spending
        k = (d, desc.upper(), round(amount, 2))
        seen[k] = seen.get(k, 0) + 1
        merchant, category = clean_merchant(desc)
        out.append(Transaction(transaction_id(user_id, d, desc, amount, seen[k]), d, desc,
                               merchant, category, round(amount, 2)))
    return out
