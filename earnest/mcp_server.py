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


_desk = None


def _sell():
    global _desk
    if _desk is None:
        from .cli import sell_desk_from_env
        _desk = sell_desk_from_env(_gateway)
    return _desk


def _own_item(item_id: str) -> bool:
    try:
        return _sell().item(item_id).user_id == USER_ID
    except ValidationError:
        return False


@mcp.tool()
def appraise_item(title: str, condition: str = "good", category: str = "", product_id: str = "",
                  description: str = "", photo_paths: list[str] | None = None) -> dict:
    """Start selling something the user showed you. Look at the photo yourself and fill in what it
    is (title), its condition (new, like_new, good, fair, poor), category (books, electronics,
    apparel, shoes, toys, home, ...) and any ISBN / UPC you can read. Returns an item id, the
    likely price, a quick-sale price, and where it would pay the most after fees and shipping."""
    try:
        it = _sell().new_item(USER_ID, title, condition, category or None, description, product_id or None,
                              photo_paths or [])
        return _sell().appraise(it.id)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def propose_sale(item_id: str, floor: float, target: float = 0.0, auto: bool = True,
                 platforms: list[str] | None = None) -> dict:
    """Propose the user's price range for an item: floor = the lowest price THE USER said they'd
    take (never invent one), target = asking price. The user gets a one-tap approval; nothing is
    listed until they approve. After approval, call sell_now."""
    if not _own_item(item_id):
        return {"status": "invalid", "error": "unknown item"}
    try:
        m = _sell().set_floor(item_id, floor, target or None, auto, platforms, by=f"agent:{AGENT_ID}")
        return {"status": m.status, "instruction": m.to_dict(),
                "next": "ask the user to tap Approve in the message earnest sent them"}
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def sell_now(item_id: str) -> dict:
    """Once the user approved the price range: take an instant buyback at or above the floor if
    there is one, otherwise list on the best connected platform (eBay auto-accepts offers at the
    floor) and prepare copy-paste listings for others. Selling below the floor is a violation."""
    if not _own_item(item_id):
        return {"status": "invalid", "error": "unknown item"}
    try:
        return _sell().go(item_id, AGENT_ID)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def respond_to_offer(item_id: str, platform: str, amount: float, offer_id: str = "") -> dict:
    """A buyer offered `amount`. Accepts it when it's at or above the user's floor and the user
    allowed auto-selling; declines below the floor; otherwise tells you to ask the user."""
    if not _own_item(item_id):
        return {"status": "invalid", "error": "unknown item"}
    try:
        if offer_id:
            return _sell().accept_offer(item_id, platform, amount, offer_id, AGENT_ID)
        return _sell().handle_offer(item_id, platform, amount, None, AGENT_ID)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def list_items_for_sale() -> list[dict]:
    """The user's items for sale, their price floors and where they're listed."""
    return _sell().items(USER_ID)


@mcp.tool()
def get_reorder_suggestions(within_days: int = 14) -> list[dict]:
    """Items the user buys regularly that are due soon (item, usual store, typical price,
    interval, next due date). Standing mandates (list_mandates) pre-approve some restocks."""
    return _gateway.reorder_predictions(USER_ID, within_days)


@mcp.tool()
def report_price(merchant: str, item: str, price: float, product_id: str = "", url: str = "") -> dict:
    """Report a price you saw for an item at a store while shopping. Reports from agents can
    only flag that a cheaper store exists; they never raise confidence in a price."""
    _gateway.report_price(merchant, item, price, f"agent:{AGENT_ID}", product_id or None, url or None)
    return {"status": "recorded"}


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
