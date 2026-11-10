"""Approvals on your phone or in chat.

When a request is escalated, earnest sends a message with the amount, merchant, item, why it
needs you, and the four confidence factors, plus Approve / Deny links. Channels:

    slack:<incoming-webhook-url>     Slack incoming webhook (Block Kit buttons)
    lark:<custom-bot-webhook-url>    Lark / Feishu custom bot (interactive card)
    ntfy:<topic-url>                 ntfy.sh push to the phone app (action buttons)
    webhook:<url>                    plain JSON, for anything else

Configure with EARNEST_NOTIFY (comma-separated) and EARNEST_APPROVAL_URL (where
`earnest serve` is reachable). Links carry a signed, expiring token for one request and one
action. Opening a link shows a confirm page; only the button press (a POST) acts, so chat
link previews and prefetchers can't approve anything.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Protocol

TOKEN_TTL_S = 24 * 3600
Post = Callable[[str, bytes, dict], None]   # (url, body, headers)


# ------------------------------------------------------------------ tokens

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def make_token(secret: bytes, request_id: str, action: str, now: float | None = None,
               ttl: int = TOKEN_TTL_S, approver: str | None = None) -> str:
    data = {"r": request_id, "a": action, "e": int((now or time.time()) + ttl)}
    if approver:
        data["u"] = approver
    payload = json.dumps(data, separators=(",", ":")).encode()
    sig = hmac.new(secret, payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def read_token(secret: bytes, token: str, now: float | None = None, with_approver: bool = False):
    """(request_id, action), or (request_id, action, approver) with with_approver; ValueError if invalid."""
    try:
        p, s = token.split(".", 1)
        payload, sig = _unb64(p), _unb64(s)
    except ValueError:
        raise ValueError("malformed token") from None
    if not hmac.compare_digest(hmac.new(secret, payload, hashlib.sha256).digest(), sig):
        raise ValueError("bad signature")
    data = json.loads(payload)
    if (now or time.time()) > data["e"]:
        raise ValueError("link expired")
    if data["a"] not in ("approve", "deny"):
        raise ValueError("unknown action")
    return (data["r"], data["a"], data.get("u")) if with_approver else (data["r"], data["a"])


# ------------------------------------------------------------------ messages

@dataclass
class ApprovalMessage:
    request_id: str
    agent_id: str
    merchant: str
    amount: float
    item: str | None
    reason_given: str
    reasons: list[str]
    confidence: dict | None
    approve_url: str
    deny_url: str
    currency: str = "USD"
    approver: str | None = None      # household member this copy of the message is for

    @property
    def title(self) -> str:
        what = f"{self.item} at {self.merchant}" if self.item else self.merchant
        return f"Approve ${self.amount:,.2f} for {what}?"

    def lines(self) -> list[str]:
        out = [f"Agent {self.agent_id}: \"{self.reason_given}\"",
               "Needs you because: " + ", ".join(self.reasons)]
        c = self.confidence
        if c:
            out.append(f"Confidence {c['score']:.2f} ({c['level']})")
            for name, f in c.get("factors", {}).items():
                out.append(f"  {name}: {f['score']:.2f} – {f['why']}")
        return out

    def to_dict(self) -> dict:
        return {"title": self.title, "request_id": self.request_id, "agent_id": self.agent_id,
                "merchant": self.merchant, "amount": self.amount, "currency": self.currency,
                "item": self.item, "reasons": self.reasons, "confidence": self.confidence,
                "approve_url": self.approve_url, "deny_url": self.deny_url}


def slack_payload(m: ApprovalMessage) -> dict:
    return {"text": m.title, "blocks": [
        {"type": "header", "text": {"type": "plain_text", "text": m.title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(m.lines())[:2900]}},
        {"type": "actions", "elements": [
            {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"},
             "url": m.approve_url},
            {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Deny"},
             "url": m.deny_url}]},
    ]}


def lark_payload(m: ApprovalMessage) -> dict:
    return {"msg_type": "interactive", "card": {
        "header": {"title": {"tag": "plain_text", "content": m.title}, "template": "orange"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(m.lines())}},
            {"tag": "action", "actions": [
                {"tag": "button", "type": "primary", "text": {"tag": "plain_text", "content": "Approve"},
                 "url": m.approve_url},
                {"tag": "button", "type": "danger", "text": {"tag": "plain_text", "content": "Deny"},
                 "url": m.deny_url}]},
        ]}}


def ntfy_request(m: ApprovalMessage) -> tuple[bytes, dict]:
    headers = {"Title": m.title.encode("ascii", "replace").decode(), "Priority": "high", "Tags": "moneybag",
               "Actions": f"view, Approve, {m.approve_url}; view, Deny, {m.deny_url}"}
    return "\n".join(m.lines()).encode(), headers


# ------------------------------------------------------------------ notifiers

class Notifier(Protocol):
    def notify(self, message: ApprovalMessage) -> None: ...


def http_post(url: str, body: bytes, headers: dict) -> None:
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    urllib.request.urlopen(req, timeout=10).close()


@dataclass
class ChannelNotifier:
    kind: str     # slack | lark | ntfy | webhook
    url: str
    post: Post = http_post

    def notify(self, m: ApprovalMessage) -> None:
        if self.kind == "ntfy":
            body, headers = ntfy_request(m)
            self.post(self.url, body, headers)
            return
        payload = {"slack": slack_payload, "lark": lark_payload}.get(self.kind, lambda x: x.to_dict())(m)
        self.post(self.url, json.dumps(payload).encode(), {"Content-Type": "application/json"})


@dataclass
class MemoryNotifier:
    """Keeps messages in memory (tests, demos)."""
    sent: list[ApprovalMessage] = field(default_factory=list)

    def notify(self, m: ApprovalMessage) -> None:
        self.sent.append(m)


@dataclass
class MultiNotifier:
    notifiers: list
    errors: list[str] = field(default_factory=list)

    def notify(self, m: ApprovalMessage) -> None:
        for n in self.notifiers:
            try:
                n.notify(m)
            except Exception as e:  # a broken channel must never block a decision
                self.errors.append(f"{getattr(n, 'kind', type(n).__name__)}: {e}")


def notifier_from_env(env: dict | None = None) -> MultiNotifier | None:
    spec = (env if env is not None else os.environ).get("EARNEST_NOTIFY", "").strip()
    if not spec:
        return None
    out = []
    for part in spec.split(","):
        kind, _, url = part.strip().partition(":")
        if kind not in ("slack", "lark", "ntfy", "webhook") or not url:
            raise ValueError(f"EARNEST_NOTIFY entry should look like slack:https://...: {part!r}")
        out.append(ChannelNotifier(kind, url))
    return MultiNotifier(out)
