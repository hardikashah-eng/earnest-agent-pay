"""Item-level purchases from emailed receipts.

Costco, Walmart and Target don't offer an easy order export, but they (and Amazon) all
email itemized receipts. This turns those emails into orders:

- Sources: an mbox file (Gmail via Google Takeout), a Maildir, a folder of .eml files, or
  IMAP (Gmail / Outlook / iCloud with an app password). Everything runs locally.
- Only mail from a **verified merchant's domain** is read, and by default only when the
  message's Authentication-Results show **DKIM pass** for that domain, so a forged
  "receipt" can't plant fake purchase history.
- Order confirmations, receipts, shipping and delivery emails are read; refund, return and
  cancellation emails are skipped. Items from several emails about one order are counted once.
- Item lines are found in the plain-text part or the HTML (prices like $12.99, Qty lines);
  totals, tax, shipping and discounts are ignored. Product links give ASINs where present.

This is pattern-based, not a parser per retailer template: templates change often, so it
aims for the common shapes and skips what it can't read rather than guessing.
"""
from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import mailbox
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, Iterable, Iterator

from ..merchants import MerchantRegistry, host_of
from ..products import asin_from_url
from ..products import normalize as normalize_product
from .orders import Order

ORDERISH = re.compile(r"\b(order|receipt|purchase|confirm|confirmation|shipped|delivered|your .* items?)\b", re.I)
NOT_A_PURCHASE = re.compile(r"\b(refund|return|returned|cancel+ed|cancellation|price adjustment)\b", re.I)
SKIP_LINE = re.compile(r"\b(sub-?total|total|tax|shipping|delivery fee|discount|savings|saved|coupon|promo|"
                       r"payment|paid|card|points|rewards|balance|tip|deposit|fee|estimated|before tax|"
                       r"grand|amount|you save)\b", re.I)
PRICE = re.compile(r"(?<![\w.])\$\s?(\d{1,5}(?:,\d{3})*\.\d{2})\b")
QTY = re.compile(r"\b(?:qty|quantity)\s*[:x]?\s*(\d{1,3})\b|\b(\d{1,3})\s*[x×]\s|\s[x×]\s*(\d{1,3})\b", re.I)
ORDER_REF = re.compile(r"\border\s*(?:#|number|no\.?|id)?\s*:?\s*#?\s*([A-Z0-9][A-Z0-9-]{5,})", re.I)
ITEM_NO = re.compile(r"^(?:item|sku|article)\s*(?:#|no\.?|number)\s*:?\s*([A-Z0-9-]{3,})$", re.I)
DKIM_PASS = re.compile(r"dkim=pass[^;]*?header\.(?:d|i)=@?([\w.-]+)", re.I)
BLOCK_TAGS = {"p", "div", "br", "tr", "li", "td", "th", "table", "h1", "h2", "h3", "h4", "section"}


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []   # (anchor text, href)
        self._href: str | None = None
        self._anchor: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("style", "script", "head"):
            self._skip += 1
        if tag in BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "a":
            self._href, self._anchor = dict(attrs).get("href"), []

    def handle_endtag(self, tag):
        if tag in ("style", "script", "head"):
            self._skip = max(0, self._skip - 1)
        if tag == "a" and self._href:
            self.links.append((" ".join("".join(self._anchor).split()), self._href))
            self._href = None
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip:
            return
        self.parts.append(data)
        if self._href is not None:
            self._anchor.append(data)


def html_to_text(html: str) -> tuple[str, list[tuple[str, str]]]:
    p = _HTMLText()
    p.feed(html)
    return "".join(p.parts), p.links


@dataclass
class ReceiptResult:
    merchant: str | None = None
    orders: list[Order] = field(default_factory=list)
    skipped: str | None = None


def _body(msg) -> tuple[str, list[tuple[str, str]]]:
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    links: list[tuple[str, str]] = []
    if html is not None:
        text, links = html_to_text(html.get_content())
        if plain is None:
            return text, links
    return (plain.get_content() if plain is not None else ""), links


def _dkim_ok(msg, sender_host: str, merchant_domains: tuple[str, ...]) -> bool:
    for header in msg.get_all("Authentication-Results", []) or []:
        for d in DKIM_PASS.findall(str(header)):
            d = d.lower().strip(".")
            if any(d == md or d.endswith("." + md) for md in merchant_domains) and \
                    (sender_host == d or sender_host.endswith("." + d) or d.endswith("." + sender_host)):
                return True
    return False


def extract_items(text: str) -> list[tuple[str, float, int, str | None]]:
    """(item, unit price, quantity, retailer item number) from receipt text."""
    lines = [" ".join(line.split()) for line in text.splitlines()]
    lines = [line for line in lines if line]
    out: list[tuple[str, float, int, str | None]] = []
    title: str | None = None
    qty, sku = 1, None
    for line in lines:
        if SKIP_LINE.search(line):
            title, qty, sku = None, 1, None
            continue
        m = ITEM_NO.match(line)
        if m:
            sku = m.group(1)
            continue
        prices = PRICE.findall(line)
        q = QTY.search(line)
        if q and not prices:
            qty = int(next(g for g in q.groups() if g))
            continue
        if not prices:
            words = re.sub(r"[^A-Za-z]", "", line)
            if len(words) >= 3 and not ORDER_REF.search(line) and len(line) <= 160:
                title, qty, sku = line, 1, None
            continue
        before = PRICE.split(line)[0]
        before = QTY.sub(" ", before).strip(" -:|•·\t")
        if q:
            qty = int(next(g for g in q.groups() if g))
        name = before if len(re.sub(r"[^A-Za-z]", "", before)) >= 3 else title
        if not name:
            continue
        price = float(prices[-1].replace(",", ""))
        # a single price next to a quantity > 1 is usually the line total
        unit = round(price / qty, 2) if qty > 1 and len(prices) == 1 else float(prices[0].replace(",", ""))
        out.append((name.strip(), unit, qty, sku))
        title, qty, sku = None, 1, None
    return out


def parse_receipt(raw: bytes, user_id: str, registry: MerchantRegistry | None = None,
                  require_dkim: bool = True) -> ReceiptResult:
    reg = registry or MerchantRegistry.default()
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    sender = parseaddr(str(msg.get("From", "")))[1].lower()
    host = host_of(sender.split("@")[-1]) if "@" in sender else None
    known = reg.find_verified(host) if host else None
    if not known:
        return ReceiptResult(skipped="not_a_verified_merchant")
    subject = str(msg.get("Subject", ""))
    if NOT_A_PURCHASE.search(subject):
        return ReceiptResult(known.domains[0], skipped="refund_or_return")
    if not ORDERISH.search(subject):
        return ReceiptResult(known.domains[0], skipped="not_a_receipt")
    if require_dkim and not _dkim_ok(msg, host, known.domains):
        return ReceiptResult(known.domains[0], skipped="unauthenticated")
    try:
        sent = parsedate_to_datetime(str(msg.get("Date"))).date()
    except (TypeError, ValueError):
        sent = date.today()
    text, links = _body(msg)
    ref_match = ORDER_REF.search(subject) or ORDER_REF.search(text)
    ref = ref_match.group(1).upper() if ref_match else ""
    asins = {anchor.lower(): asin_from_url(href) for anchor, href in links if asin_from_url(href)}
    merchant = known.domains[0]
    orders = []
    seen: dict[tuple, int] = {}
    for item, unit, qty, sku in extract_items(text):
        k = (item.lower(), unit)
        seen[k] = seen.get(k, 0) + 1
        key = f"{user_id}|{merchant}|{ref or sent.isoformat()}|{item.lower()}|{unit:.2f}|{seen[k]}"
        pid = asins.get(item.lower()) or (normalize_product(sku, "sku", merchant) if sku else None)
        orders.append(Order("rcpt_" + hashlib.sha256(key.encode()).hexdigest()[:16], sent, merchant, item,
                            unit, qty, pid))
    if not orders:
        return ReceiptResult(merchant, skipped="no_items_found")
    return ReceiptResult(merchant, orders)


# ------------------------------------------------------------------ mailboxes

def read_path(path: str | Path) -> Iterator[bytes]:
    """Messages from an mbox file, a Maildir, a single .eml, or a folder of .eml files."""
    p = Path(path)
    if p.is_dir() and (p / "cur").is_dir():
        for m in mailbox.Maildir(str(p), create=False):
            yield m.as_bytes()
    elif p.is_dir():
        for f in sorted(p.glob("*.eml")):
            yield f.read_bytes()
    elif p.suffix.lower() == ".eml":
        yield p.read_bytes()
    else:
        for m in mailbox.mbox(str(p), create=False):
            yield m.as_bytes()


def read_imap(host: str, username: str, password: str, since: date, domains: Iterable[str],
              folder: str = "INBOX", connect: Callable[[str], imaplib.IMAP4] = imaplib.IMAP4_SSL) -> Iterator[bytes]:
    """Messages from retailer domains since a date (read-only)."""
    conn = connect(host)
    try:
        conn.login(username, password)
        conn.select(folder, readonly=True)
        seen: set[bytes] = set()
        day = since.strftime("%d-%b-%Y")
        for d in domains:
            typ, data = conn.search(None, "SINCE", day, "FROM", f'"{d}"')
            if typ != "OK":
                continue
            for num in data[0].split():
                if num in seen:
                    continue
                seen.add(num)
                typ, msg = conn.fetch(num, "(BODY.PEEK[])")
                if typ == "OK" and msg and isinstance(msg[0], tuple):
                    yield msg[0][1]
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def import_receipts(ledger, messages: Iterable[bytes], user_id: str, require_dkim: bool = True,
                    source: str = "email") -> dict:
    added = dupes = 0
    skipped: dict[str, int] = {}
    for raw in messages:
        r = parse_receipt(raw, user_id, require_dkim=require_dkim)
        if r.skipped:
            skipped[r.skipped] = skipped.get(r.skipped, 0) + 1
            continue
        a, d = ledger.add_orders(user_id, r.orders, source)
        added, dupes = added + a, dupes + d
    return {"added": added, "duplicates": dupes, "skipped": skipped}


def retailer_domains(registry: MerchantRegistry | None = None) -> list[str]:
    reg = registry or MerchantRegistry.default()
    return [m.domains[0] for m in reg.verified]


def since_days(days: int, today: date | None = None) -> date:
    from datetime import timedelta
    return (today or datetime.now().date()) - timedelta(days=days)

