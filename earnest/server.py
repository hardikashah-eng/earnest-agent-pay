"""`earnest serve`: the small web endpoint behind approval links and the Stripe webhook.

    GET  /a/<token>        confirm page for an Approve / Deny link (does nothing by itself)
    POST /a/<token>        acts on it
    POST /stripe/webhook   Stripe Issuing real-time authorizations (when a Stripe backend is set)
    GET  /healthz

Standard library only. Put it behind HTTPS (a tunnel or reverse proxy) before exposing it.
"""
from __future__ import annotations

import html
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .models import ValidationError

PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>earnest</title><style>body{{font:16px system-ui;max-width:32rem;margin:2rem auto;padding:0 1rem}}
button{{font:inherit;padding:.6rem 1.4rem;border-radius:.5rem;border:0;color:#fff;background:{color}}}
pre{{white-space:pre-wrap;background:#f4f4f4;padding:.8rem;border-radius:.4rem}}</style>
<h2>{title}</h2>{body}"""


def make_handler(gateway, lock: threading.Lock, stripe_secret: str | None = None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep request logs out of stdout
            pass

        def _send(self, status: int, body: str, ctype: str = "text/html; charset=utf-8", headers=None):
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _page(self, status, title, body, color="#555"):
            self._send(status, PAGE.format(title=html.escape(title), body=body, color=color))

        def do_GET(self):
            if self.path == "/healthz":
                return self._send(200, "ok", "text/plain")
            if not self.path.startswith("/a/"):
                return self._page(404, "Not found", "")
            token = self.path[3:]
            with lock:
                try:
                    request_id, action = gateway.read_approval_token(token)
                    p = gateway.describe_pending(request_id)
                except ValueError as e:
                    return self._page(400, "This link doesn't work", html.escape(str(e)))
            if p is None:
                return self._page(410, "Already handled", "This request isn't waiting for you any more.")
            what = p.get("title") or (f"${p['amount']:,.2f} at {p['merchant']}" +
                                      (f" for {p['item']}" if p.get("item") else ""))
            detail = html.escape(json.dumps({"reasons": p["reasons"], "agent_said": p["reason_given"],
                                             "confidence": (p.get("confidence") or {}).get("score")}, indent=1))
            verb = "Approve" if action == "approve" else "Deny"
            noun = "sale" if p.get("merchant") == "sell" else "purchase"
            body = (f"<p>{html.escape(what)}</p><pre>{detail}</pre>"
                    f"<form method=post><button>{verb}</button></form>")
            self._page(200, f"{verb} this {noun}?", body, "#1a7f37" if action == "approve" else "#b42318")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            payload = self.rfile.read(length)
            if self.path == "/stripe/webhook":
                backend = gateway.backend
                if stripe_secret is None or not hasattr(backend, "handle_webhook"):
                    return self._send(404, "{}", "application/json")
                status, headers, body = backend.handle_webhook(
                    payload, self.headers.get("Stripe-Signature", ""), stripe_secret)
                return self._send(status, json.dumps(body), "application/json", headers)
            if not self.path.startswith("/a/"):
                return self._page(404, "Not found", "")
            with lock:
                try:
                    d = gateway.resolve_token(self.path[3:])
                except (ValueError, ValidationError) as e:
                    return self._page(400, "Couldn't do that", html.escape(str(e)))
            status = getattr(d, "status", "")
            if status == "approved":
                self._page(200, "Approved", f"A ${d.credential.cap:,.2f} card was issued to the agent.", "#1a7f37")
            elif status == "active":
                self._page(200, "Selling", f"The agent can now sell it for ${d.floor:,.2f} or more.", "#1a7f37")
            elif status == "escalated":
                self._page(200, "Approved", "Thanks. One more adult needs to approve this purchase.", "#1a7f37")
            else:
                self._page(200, "Done", "The agent was told no.", "#b42318")

    return Handler


def serve(gateway, host: str = "127.0.0.1", port: int = 8787) -> ThreadingHTTPServer:
    """Build the server (call .serve_forever() on it)."""
    handler = make_handler(gateway, threading.Lock(), os.environ.get("STRIPE_WEBHOOK_SECRET"))
    return ThreadingHTTPServer((host, port), handler)
