"""Dispute evidence pack (spec/SPEC.md section 3f).

Everything about one purchase in one place, for a chargeback or a merchant complaint: what
the user authorized (mandate), what the agent asked for and why, what the gateway decided and
on what evidence (reasons, confidence), who approved it and when, the scoped credential, the
checkout/order verification, the user's verdict, matching orders, and a timeline. Comes out as
JSON (with a SHA-256 over the canonical body) and a short Markdown summary that suggests a
dispute reason.
"""
from __future__ import annotations

import hashlib
import json


def suggest_reason(pack: dict) -> str:
    v = (pack.get("verification") or {}).get("status")
    verdict = (pack.get("outcome") or {}).get("verdict")
    if v == "mismatch":
        return "Merchandise/service not as described: the charge was for a different item than authorized"
    if v in (None, "pending", "unverified") and verdict in ("rejected", "disputed"):
        return "Merchandise/service not received"
    if verdict in ("rejected", "disputed"):
        return "Merchandise/service not as described or defective"
    return "No dispute reason detected (purchase verified and not rejected)"


def build(*, request: dict, mandate: dict | None, decision: dict, events: list[dict], approvals: list[dict],
          verification: dict | None, outcome: dict | None, orders: list[dict], intent_mandate: dict | None) -> dict:
    timeline = [{"at": request["created_at"], "what": f"agent {request['agent_id']} requested "
                                                    f"${request['amount']:.2f} at {request['merchant']}"}]
    timeline += [{"at": a["at"], "what": f"approved by {a['approver']}"} for a in approvals]
    timeline += [{"at": e["at"], "what": f"{e['kind']} ({e['points']:+g} trust)"} for e in events]
    if verification:
        timeline.append({"at": verification["at"], "what": f"item check: {verification['status']} "
                                                           f"({verification['detail']})"})
    if outcome:
        timeline.append({"at": outcome["at"], "what": f"user verdict: {outcome['verdict']}"})
    body = {
        "request_id": request["id"], "status": request["status"],
        "authorized_by_user": mandate, "agent_request": request["data"],
        "gateway_decision": {k: decision.get(k) for k in ("status", "reasons", "tier", "credential", "confidence")},
        "approvals": approvals, "verification": verification, "outcome": outcome,
        "matching_orders": orders, "ap2_intent_mandate": intent_mandate,
        "timeline": sorted(timeline, key=lambda t: t["at"]),
    }
    body["suggested_dispute_reason"] = suggest_reason(body)
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return {**body, "sha256": digest}


def to_markdown(pack: dict) -> str:
    req = pack["agent_request"]
    lines = [f"# Purchase evidence: {pack['request_id']}", "",
             f"**Suggested dispute reason:** {pack['suggested_dispute_reason']}", "",
             f"- Merchant: {req['merchant']}",
             f"- Amount: ${req['amount']:.2f}" + (f" ({req['quantity']} units)" if req.get("quantity") else ""),
             f"- Item claimed: {req.get('item') or 'not stated'}",
             f"- Agent's reason: {req['reason']}"]
    m = pack.get("authorized_by_user")
    if m:
        lines.append(f"- User's mandate: {m['intent']} (budget up to {m['budget'].get('currency', 'USD')} "
                     f"{m['budget']['max']})")
    cred = (pack["gateway_decision"] or {}).get("credential") or {}
    if cred:
        lines.append(f"- Card: single-use={cred.get('single_use')}, cap ${cred.get('cap')}, locked to "
                     f"{cred.get('merchant_lock')}, expires {cred.get('expires_at')}")
    v = pack.get("verification")
    if v:
        lines.append(f"- Item check: {v['status']}: {v['detail']}")
    o = pack.get("outcome")
    if o:
        lines.append(f"- User verdict: {o['verdict']}" + (f" ({o['note']})" if o.get("note") else ""))
    lines += ["", "## Timeline", ""] + [f"- {t['at']}: {t['what']}" for t in pack["timeline"]]
    lines += ["", f"Evidence hash (SHA-256 of the JSON pack): `{pack['sha256']}`", ""]
    return "\n".join(lines)
