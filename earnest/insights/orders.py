"""Retailer order history and wishlists: item-level purchase data.

Card statements say *where* and *how much*; order history says *what*. Import it from:

- Amazon: Account > "Request your data" > Your Orders. Use Retail.OrderHistory.*.csv
  (columns like "Order Date", "Product Name", "Unit Price", "Quantity", "Website",
  "Order Status"). Older order reports ("Title", "Item Total") work too.
- Costco, Walmart, Target, and anything else: export or copy receipts into a CSV with a
  date, item and price column (see the generic column names below), and pass
  --retailer costco.com / walmart.com / target.com. Their privacy-portal data requests
  return order data that maps onto the same columns.
- A connector: implement `OrderSource` and call `sync_orders`.

Cancelled, returned and refunded lines are skipped. Re-importing never double counts.
"""
from __future__ import annotations

import csv
import hashlib
import io
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

from ..models import ValidationError
from .statement import _parse_amount, _parse_date

DATE_COLS = ("order date", "purchase date", "date", "transaction date", "order placed")
ITEM_COLS = ("product name", "title", "item name", "item", "item description", "description", "product", "name")
UNIT_COLS = ("unit price", "item price", "price", "purchase price per unit", "price per unit")
TOTAL_COLS = ("item total", "total owed", "item subtotal", "total", "amount")
QTY_COLS = ("quantity", "qty", "units")
MERCHANT_COLS = ("website", "retailer", "store", "merchant")
STATUS_COLS = ("order status", "status")
SKIP_STATUS = ("cancel", "return", "refund")


@dataclass(frozen=True)
class Order:
    id: str
    date: date
    merchant: str
    item: str
    unit_price: float
    quantity: int = 1


@dataclass(frozen=True)
class WishlistItem:
    item: str
    merchant: str | None = None
    max_price: float | None = None


def _header(fieldnames) -> dict[str, str]:
    if not fieldnames:
        raise ValidationError("empty CSV")
    return {h.strip().lower(): h for h in fieldnames if h}


def _pick(header: dict[str, str], options: tuple[str, ...]) -> str | None:
    for opt in options:
        if opt in header:
            return header[opt]
    return None


def _date(s: str) -> date:
    s = s.strip()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return _parse_date(s)


def order_id(user_id: str, merchant: str, d: date, item: str, unit: float, ref: str, occurrence: int) -> str:
    key = f"{user_id}|{merchant.lower()}|{d.isoformat()}|{item.strip().lower()}|{unit:.2f}|{ref}|{occurrence}"
    return hashlib.sha256(key.encode()).hexdigest()[:20]


def parse_orders_csv(text: str, user_id: str, retailer: str | None = None) -> list[Order]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = _header(reader.fieldnames)
    date_col, item_col = _pick(header, DATE_COLS), _pick(header, ITEM_COLS)
    unit_col, total_col = _pick(header, UNIT_COLS), _pick(header, TOTAL_COLS)
    qty_col, merch_col = _pick(header, QTY_COLS), _pick(header, MERCHANT_COLS)
    status_col, ref_col = _pick(header, STATUS_COLS), _pick(header, ("order id", "order number", "order #"))
    if not date_col or not item_col or not (unit_col or total_col):
        raise ValidationError(f"couldn't find date/item/price columns in {reader.fieldnames}")
    if not merch_col and not retailer:
        raise ValidationError("no retailer column: pass the retailer (e.g. --retailer costco.com)")

    out: list[Order] = []
    seen: dict[tuple, int] = {}
    for row in reader:
        raw_date, item = (row.get(date_col) or "").strip(), (row.get(item_col) or "").strip()
        if not raw_date or not item:
            continue
        if status_col and any(s in (row.get(status_col) or "").lower() for s in SKIP_STATUS):
            continue
        try:
            qty = max(1, int(float((row.get(qty_col) or "1").strip() or 1))) if qty_col else 1
        except ValueError:
            qty = 1
        unit = _parse_amount(row.get(unit_col)) if unit_col else None
        if unit is None and total_col:
            total = _parse_amount(row.get(total_col))
            unit = total / qty if total is not None else None
        if unit is None or unit <= 0:
            continue
        merchant = (row.get(merch_col) or "").strip() if merch_col else ""
        merchant = merchant or retailer
        d = _date(raw_date)
        ref = (row.get(ref_col) or "").strip() if ref_col else ""
        k = (d, item.lower(), round(unit, 2), ref)
        seen[k] = seen.get(k, 0) + 1
        out.append(Order(order_id(user_id, merchant, d, item, unit, ref, seen[k]), d, merchant, item,
                         round(unit, 2), qty))
    return out


def parse_wishlist_csv(text: str, retailer: str | None = None) -> list[WishlistItem]:
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    header = _header(reader.fieldnames)
    item_col = _pick(header, ITEM_COLS)
    price_col = _pick(header, UNIT_COLS + TOTAL_COLS + ("max price", "budget"))
    merch_col = _pick(header, MERCHANT_COLS)
    if not item_col:
        raise ValidationError(f"couldn't find an item column in {reader.fieldnames}")
    out = []
    for row in reader:
        item = (row.get(item_col) or "").strip()
        if not item:
            continue
        price = _parse_amount(row.get(price_col)) if price_col else None
        merchant = ((row.get(merch_col) or "").strip() if merch_col else "") or retailer
        out.append(WishlistItem(item, merchant or None, price))
    return out


class OrderSource(Protocol):
    """A connector that can pull order history (e.g. a retailer API or an email-receipt parser)."""
    name: str

    def fetch(self, user_id: str, since: date | None) -> list[Order]: ...


def sync_orders(ledger, source: OrderSource, user_id: str, since: date | None = None) -> tuple[int, int]:
    return ledger.add_orders(user_id, source.fetch(user_id, since), source.name)
