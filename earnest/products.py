"""Product identity: ASINs and GTINs (UPC / EAN) instead of guessing from titles.

Text matching treats "Anker USB-C cable 6ft" and "Anker USB-C cable 3ft" as similar. A product
ID settles it: two purchases with the same ID are the same item, two with different IDs are
not the same item (they may still be similar). IDs come from retailer order exports (Amazon's
ASIN column, UPC / GTIN / item-number columns), product links in email receipts, and the agent's
spend request.

Normalized form: "asin:B000TEST01", "gtin:00012345678905" (GTIN-14, check digit verified),
"sku:<retailer>:<id>" for retailer item numbers (e.g. Costco item #).
"""
from __future__ import annotations

import re

_ASIN = re.compile(r"^(B0[A-Z0-9]{8}|\d{9}[\dX])$")
_ASIN_IN_URL = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/ASIN)/([A-Z0-9]{10})(?:[/?&#]|$)", re.I)


def gtin_check_digit_ok(digits: str) -> bool:
    if not digits.isdigit() or len(digits) not in (8, 12, 13, 14):
        return False
    body, check = digits[:-1], int(digits[-1])
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize(raw: str | None, kind: str | None = None, retailer: str | None = None) -> str | None:
    """Normalize a product ID. `kind` may be asin, gtin, upc, ean or sku; None = detect."""
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    low = s.lower()
    for prefix in ("asin:", "gtin:", "sku:"):
        if low.startswith(prefix):
            return prefix + (s[len(prefix):].upper() if prefix != "sku:" else s[len(prefix):].lower())
    kind = (kind or "").lower()
    if kind in ("gtin", "upc", "ean") or (not kind and s.isdigit() and len(s) in (8, 12, 13, 14)):
        digits = re.sub(r"\D", "", s)
        if gtin_check_digit_ok(digits):
            return "gtin:" + digits.zfill(14)
        return None
    if kind == "asin" or (not kind and _ASIN.match(s.upper())):
        return "asin:" + s.upper() if _ASIN.match(s.upper()) else None
    if kind == "sku" or retailer:
        return f"sku:{(retailer or 'unknown').lower()}:{s.lower()}"
    return None


def asin_from_url(url: str) -> str | None:
    m = _ASIN_IN_URL.search(url or "")
    return "asin:" + m.group(1).upper() if m else None


def same_product(a: str | None, b: str | None) -> bool | None:
    """True / False when both IDs are known, None when either is missing."""
    if not a or not b:
        return None
    return a == b
