"""MCP server exposing the agent-side tools.

Install the extra and run:

    pip install -e ".[mcp]"
    EARNEST_USER_ID=user_demo EARNEST_AGENT_ID=agent_shopper EARNEST_DB=earnest.db python -m earnest.mcp_server

The agent's identity comes from the environment, not from tool arguments, so an agent
can't claim to be a different (more trusted) agent. Outcomes are reported by the user
through the CLI, never by the agent.
"""
from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from .approvals import notifier_from_env
from .gateway import Gateway
from .ledger import Ledger
from .models import ValidationError

USER_ID = os.environ.get("EARNEST_USER_ID", "user_demo")
AGENT_ID = os.environ.get("EARNEST_AGENT_ID", "agent_default")
DB_PATH = os.environ.get("EARNEST_DB", "earnest.db")

mcp = FastMCP("earnest")
_gateway = Gateway(Ledger(DB_PATH), notifier=notifier_from_env(),
                   approval_url=os.environ.get("EARNEST_APPROVAL_URL"))


def _own(request_id: str) -> bool:
    row = _gateway.ledger.get_request(request_id)
    return row is not None and row["agent_id"] == AGENT_ID and row["user_id"] == USER_ID


@mcp.tool()
def list_mandates() -> list[dict]:
    """List the tasks you're allowed to spend on, with their budgets and rules."""
    return [m.to_dict() for m in _gateway.ledger.list_mandates(USER_ID)]


@mcp.tool()
def get_limits(task_type: str) -> dict:
    """Your current trust tier and spending limits for a task type (e.g. 'software')."""
    return _gateway.limits(USER_ID, AGENT_ID, task_type)


@mcp.tool()
def check_merchant(merchant: str, item: str = "", amount: float = 0.0, category: str = "",
                   mandate_id: str = "", quantity: int = 1, product_id: str = "") -> dict:
    """Score a purchase before you request it. Returns a 0-1 confidence with four factors:
    merchant authenticity (verified store, user's membership or history), price (vs what the
    user paid for this or similar items in the last year), familiarity (bought before, or on
    the user's wishlist / interests) and budget headroom on the mandate. Untrusted merchants
    and lookalikes of well-known stores are denied and cost trust; low confidence goes to
    the user. Prefer the merchant and price that score highest."""
    return _gateway.check_merchant(USER_ID, merchant, item or None, amount, category or None,
                                   mandate_id or None, quantity, product_id or None)


@mcp.tool()
def request_spend(mandate_id: str, amount: float, merchant: str, category: str, reason: str,
                  item: str = "", quantity: int = 1, product_id: str = "", recurring: bool = False, international: bool = False,
                  ask_first: bool = False) -> dict:
    """Ask to spend money for a mandate. Returns approved (with a scoped, expiring card),
    escalated (waiting for the user) or denied, plus a confidence block. Name the `item` you
    are buying so the gateway can match it against the user's past purchases. Set
    ask_first=true when you're unsure: asking before acting is rewarded when it was needed.
    Spending outside the mandate, or at an untrusted merchant, is penalized heavily."""
    try:
        payload = {
            "agent_id": AGENT_ID, "mandate_id": mandate_id, "amount": amount, "merchant": merchant,
            "category": category, "reason": reason, "recurring": recurring,
            "international": international, "ask_first": ask_first,
        }
        if item:
            payload["item"] = item
        if quantity != 1:
            payload["quantity"] = quantity
        if product_id:
            payload["product_id"] = product_id  # ASIN or UPC/GTIN from the product page
        return _gateway.request_spend(payload).to_dict()
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def get_ap2_intent_mandate(request_id: str) -> dict:
    """For one of your approved requests: an AP2 Intent mandate to present to an AP2 merchant,
    attested by earnest (merchant, max amount, item, expiry)."""
    if not _own(request_id):
        return {"status": "invalid", "error": "not one of your requests"}
    try:
        return _gateway.ap2_intent_mandate(request_id)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def check_ap2_cart(request_id: str, cart_mandate: dict) -> dict:
    """Before paying an AP2 merchant: check its Cart mandate against your approved request
    (merchant, total within the approved amount, the item you named is in the cart).
    A cart without your claimed item is a violation."""
    if not _own(request_id):
        return {"status": "invalid", "error": "not one of your requests"}
    try:
        return _gateway.check_ap2_cart(request_id, cart_mandate)
    except (ValidationError, KeyError) as e:
        return {"status": "invalid", "error": str(e)}


if __name__ == "__main__":
    mcp.run()
