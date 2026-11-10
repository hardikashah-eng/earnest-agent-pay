"""Portable agent trust records (spec/SPEC.md section 3h).

A user can give an agent a signed summary of how it behaved for them: a JWT signed with
EdDSA (Ed25519, RFC 8037) by the user's earnest install. Another earnest user can verify it
with the public key in the token and give the agent a head start instead of starting at zero.

Privacy: the record names the agent and the task type, and carries coarse bands only (tier,
purchase count band, verified-item band, kept-rate to one decimal, violations in the last 90
days). No merchants, items, amounts, dates of purchases or anything identifying the issuing
user beyond a pseudonymous key.

Limits on the head start: only from issuers the importing user chose to trust; at most enough to
reach `established` (score 20, still asks for new merchants and anything over $150); nothing if
the record shows recent violations; once per agent, task type and issuer.
"""
from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta

from . import ed25519

TTL_DAYS = 30
SEED = {"trusted": 20.0, "established": 10.0}


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def band(n: int) -> str:
    for lo, hi in ((0, 0), (1, 9), (10, 49), (50, 199)):
        if lo <= n <= hi:
            return f"{lo}-{hi}" if lo != hi else "0"
    return "200+"


def encode(claims: dict, secret: bytes) -> str:
    kid = _b64(ed25519.public_key(secret))
    header = {"alg": "EdDSA", "typ": "JWT", "kid": kid}
    signing_input = f"{_b64(json.dumps(header, separators=(',', ':')).encode())}." \
                    f"{_b64(json.dumps(claims, separators=(',', ':'), sort_keys=True).encode())}"
    return signing_input + "." + _b64(ed25519.sign(secret, signing_input.encode()))


def decode(token: str, now: datetime) -> tuple[dict, str]:
    """(claims, issuer key id). ValueError if malformed, forged or expired."""
    try:
        h, c, s = token.strip().split(".")
        header, claims, sig = json.loads(_unb64(h)), json.loads(_unb64(c)), _unb64(s)
    except ValueError:
        raise ValueError("not an earnest trust record") from None
    if header.get("alg") != "EdDSA" or "kid" not in header:
        raise ValueError("unsupported signature algorithm")
    if not ed25519.verify(_unb64(header["kid"]), f"{h}.{c}".encode(), sig):
        raise ValueError("signature doesn't verify")
    if claims.get("iss") != f"earnest:{header['kid']}":
        raise ValueError("issuer doesn't match the signing key")
    if now.timestamp() > claims.get("exp", 0):
        raise ValueError("trust record expired")
    return claims, header["kid"]


def claims_for(agent_id: str, task_type: str, tier: str, purchases: int, verified: int, kept_rate: float | None,
               violations_90d: int, issuer_kid: str, now: datetime, ttl_days: int = TTL_DAYS) -> dict:
    return {
        "iss": f"earnest:{issuer_kid}", "sub": agent_id, "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=ttl_days)).timestamp()), "v": 1,
        "trust": {"task_type": task_type, "tier": tier, "purchases": band(purchases),
                  "verified_items": band(verified),
                  "kept_rate": None if kept_rate is None else round(kept_rate, 1),
                  "violations_90d": violations_90d},
    }


def seed_points(claims: dict) -> float:
    t = claims["trust"]
    if t.get("violations_90d", 0) > 0:
        return 0.0
    if t.get("kept_rate") is not None and t["kept_rate"] < 0.8:
        return 0.0
    return SEED.get(t.get("tier"), 0.0)
