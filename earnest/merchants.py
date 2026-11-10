"""Merchant trust: a known-good list, a known-bad list, and lookalike detection.

The default lists live in earnest/data/merchant_trust.json. Users add their own entries
(trusted or untrusted) on top, and those always win: explicit instructions beat defaults.

Merchants arrive in many shapes: "amazon.com", "https://www.amazon.com/dp/...", "Amazon",
"AMZN Mktp" (from a card statement). `merchant_keys` turns each into comparable keys so the
same store matches across spend requests, user lists and imported statements.

Pure functions plus one data file. No I/O beyond loading that file.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

DATA_FILE = Path(__file__).parent / "data" / "merchant_trust.json"

# Two-level public suffixes we see on shopping sites. Not exhaustive; good enough to find
# the label that names the store ("amaz0n" in "shop.amaz0n.co.uk").
_SECOND_LEVEL = {"co.uk", "com.au", "co.in", "co.jp", "com.br", "com.mx", "co.nz", "com.sg", "co.za"}
_DOMAIN = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")

TRUSTED = ("verified", "user_trusted")


# ------------------------------------------------------------------ normalization

def host_of(merchant: str) -> str | None:
    """'https://www.Amazon.com/dp/1' -> 'amazon.com'. None if it isn't a domain."""
    s = merchant.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    if s.startswith("www."):
        s = s[4:]
    return s if " " not in s and _DOMAIN.match(s) else None


def split_host(host: str) -> tuple[str, str, str]:
    """'shop.amaz0n.co.uk' -> ('shop', 'amaz0n', 'co.uk')."""
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _SECOND_LEVEL:
        return ".".join(parts[:-3]), parts[-3], ".".join(parts[-2:])
    if len(parts) >= 2:
        return ".".join(parts[:-2]), parts[-2], parts[-1]
    return "", host, ""


def name_key(merchant: str) -> str:
    """'Lowe's' -> 'lowes'; 'B&H Photo' -> 'bhphoto'."""
    return re.sub(r"[^a-z0-9]", "", merchant.lower())


def merchant_keys(merchant: str) -> set[str]:
    """Keys that identify a merchant regardless of how it was written."""
    host = host_of(merchant)
    if host:
        _, label, _ = split_host(host)
        return {host, label}
    k = name_key(merchant)
    return {k} if k else set()


def _tokens(host: str) -> list[str]:
    return [t for t in re.split(r"[.\-_]+", host) if t]


def _edit_distance(a: str, b: str, limit: int) -> int:
    """Damerau-Levenshtein (optimal string alignment), stopping early past `limit`."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


# ------------------------------------------------------------------ registry

@dataclass(frozen=True)
class KnownMerchant:
    id: str
    name: str
    domains: tuple[str, ...]
    aliases: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()


@dataclass
class MerchantCheck:
    """What the gateway knows about a merchant before any purchase history."""
    merchant: str
    status: str                      # verified | user_trusted | unknown | suspicious | untrusted
    merchant_id: str | None = None   # canonical id for verified merchants
    name: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.status in TRUSTED

    def to_dict(self) -> dict:
        out = {"merchant": self.merchant, "status": self.status, "reasons": self.reasons}
        if self.merchant_id:
            out["merchant_id"] = self.merchant_id
        if self.name:
            out["name"] = self.name
        return out


class MerchantRegistry:
    def __init__(self, data: dict):
        self.version = data.get("version", "")
        self.verified = [KnownMerchant(m["id"], m["name"], tuple(d.lower() for d in m["domains"]),
                                       tuple(m.get("aliases", ())), tuple(m.get("categories", ())))
                         for m in data.get("verified", [])]
        u = data.get("untrusted", {})
        self.entities = {}
        for e in u.get("entities", []):
            for k in merchant_keys(e["merchant"]):
                self.entities[k] = e.get("reason", "listed")
        look = u.get("lookalikes", {})
        self.homoglyphs = sorted(look.get("homoglyphs", {}).items(), key=lambda kv: -len(kv[0]))
        self.max_edit = int(look.get("max_edit_distance", 1))
        self.edit_min_len = int(look.get("edit_distance_min_length", 6))
        self.brand_min_len = int(look.get("brand_in_domain_min_length", 4))
        self.suspicious_tlds = set(u.get("suspicious_tlds", []))
        self.suspicious_keywords = set(u.get("suspicious_keywords", []))
        self.high_risk_categories = set(u.get("high_risk_categories", {}).get("values", []))

        self._by_domain: dict[str, KnownMerchant] = {}
        self._by_label: dict[str, KnownMerchant] = {}
        self._by_name: dict[str, KnownMerchant] = {}
        for m in self.verified:
            for d in m.domains:
                self._by_domain[d] = m
                self._by_label.setdefault(split_host(d)[1], m)
            for n in (m.id, m.name, *m.aliases):
                self._by_name.setdefault(name_key(n), m)

    @classmethod
    def default(cls) -> "MerchantRegistry":
        return _default_registry()

    @classmethod
    def from_file(cls, path: str | Path) -> "MerchantRegistry":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    # --- lookups ---
    def find_verified(self, merchant: str) -> KnownMerchant | None:
        host = host_of(merchant)
        if host:
            # exact domain or a subdomain of one (smile.amazon.com)
            for d in (host, *[host.split(".", i)[-1] for i in range(1, host.count("."))]):
                if d in self._by_domain:
                    return self._by_domain[d]
            return None
        return self._by_name.get(name_key(merchant))

    def canonical(self, merchant: str) -> set[str]:
        """Keys for matching history: the verified id when known, plus the raw keys."""
        keys = merchant_keys(merchant)
        m = self.find_verified(merchant)
        if m:
            keys.add("id:" + m.id)
        return keys

    def lookalike_of(self, merchant: str) -> KnownMerchant | None:
        """A domain that imitates a verified merchant without being one of its domains."""
        host = host_of(merchant)
        if not host or self.find_verified(merchant):
            return None
        _, label, _ = split_host(host)
        if label in self._by_label:
            return None  # same brand name on an unlisted domain (ikea.fr): see unlisted_domain_of
        # 1. typo of a brand: amazom.com, targett.com
        for known_label, m in self._by_label.items():
            if (len(known_label) >= self.edit_min_len
                    and _edit_distance(label, known_label, self.max_edit) <= self.max_edit):
                return m
        # 2. brand (or a homoglyph spelling of it) used as a whole token inside someone else's
        #    domain: amaz0n.com, arnazon.com, amazon-giftcards.shop, amaz0n-deals.com, amazon.evil.com
        for token in {label, *_tokens(host)[:-1]}:
            folded = self._fold(token)
            m = self._by_label.get(folded)
            if m and len(folded) >= self.brand_min_len:
                return m
        return None

    def _fold(self, s: str) -> str:
        for src, dst in self.homoglyphs:
            s = s.replace(src, dst)
        return s

    def unlisted_domain_of(self, merchant: str) -> KnownMerchant | None:
        """A verified brand's name on a domain we don't list (amazon.fr, apple.co). Could be a
        regional store, could be a fake: the user should look first."""
        host = host_of(merchant)
        if not host or self.find_verified(merchant):
            return None
        return self._by_label.get(split_host(host)[1])

    def check(self, merchant: str, overrides: dict[str, tuple[str, str]] | None = None) -> MerchantCheck:
        """Classify a merchant. `overrides` maps merchant keys to (status, reason) from the
        user's own lists; status is 'trusted' or 'untrusted'."""
        keys = merchant_keys(merchant)
        for k in keys:
            if overrides and k in overrides:
                status, reason = overrides[k]
                if status == "untrusted":
                    return MerchantCheck(merchant, "untrusted", reasons=[f"merchant_untrusted:user_list:{reason}"])
                v = self.find_verified(merchant)
                return MerchantCheck(merchant, "user_trusted", v.id if v else None, v.name if v else None,
                                     ["merchant_user_trusted"])

        v = self.find_verified(merchant)
        if v:
            return MerchantCheck(merchant, "verified", v.id, v.name, ["merchant_verified"])

        for k in keys:
            if k in self.entities:
                return MerchantCheck(merchant, "untrusted", reasons=[f"merchant_untrusted:{self.entities[k]}"])

        look = self.lookalike_of(merchant)
        if look:
            return MerchantCheck(merchant, "untrusted", reasons=[f"merchant_lookalike:{look.id}"])

        reasons = []
        unlisted = self.unlisted_domain_of(merchant)
        if unlisted:
            reasons.append(f"merchant_unlisted_domain:{unlisted.id}")
        host = host_of(merchant)
        if host:
            _, _, tld = split_host(host)
            if tld.rsplit(".", 1)[-1] in self.suspicious_tlds:
                reasons.append(f"merchant_suspicious_tld:{tld}")
            hits = sorted(self.suspicious_keywords & set(_tokens(host)))
            if hits:
                reasons.append(f"merchant_suspicious_name:{hits[0]}")
        if reasons:
            return MerchantCheck(merchant, "suspicious", reasons=reasons)
        return MerchantCheck(merchant, "unknown", reasons=["merchant_unknown"])

    def high_risk_category(self, category: str) -> bool:
        return re.sub(r"[\s-]+", "_", category.strip().lower()) in self.high_risk_categories


@lru_cache(maxsize=1)
def _default_registry() -> MerchantRegistry:
    return MerchantRegistry.from_file(DATA_FILE)
