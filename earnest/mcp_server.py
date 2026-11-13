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
def get_account() -> dict:
    """Your spending account, if the user gave you one: total balance, and each source (your
    allowance, store gift cards and prepaid cards, last 4 digits only). A gift card only pays at
    its store. You can't top it up; ask the user. Also says if you're in read-only shadow mode."""
    out: dict = {}
    if _gateway.ledger.get_account(USER_ID, AGENT_ID) is not None:
        acct = _gateway.account(USER_ID, AGENT_ID)
        out = {k: acct[k] for k in ("status", "balance", "sources")}
    else:
        out = {"status": "no_account", "note": "purchases are paid with single-use cards when approved"}
    shadow = _gateway.ledger.shadow_enrollment(USER_ID, AGENT_ID)
    if shadow and not shadow["ended_at"]:
        out["mode"] = "shadow"
        out["shadow_note"] = ("read-only: your requests get a real decision but no card and no money. "
                              "Propose what the user would buy; matches with their real purchases build trust.")
    return out


@mcp.tool()
def evaluate_purchase(price: float, evaluation: dict) -> dict:
    """Score a possible purchase on the user's rubric before asking to buy it: price (cost per
    use against their hourly value), time saved, utility and added value, each 1-10, weighted by
    their goals, plus the one-year net value (hours saved x hourly value + added value - price).
    Says whether it meets their threshold. Nothing is bought or recorded."""
    try:
        return _gateway.score_purchase(USER_ID, price, evaluation)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def get_gift_tasks() -> list[dict]:
    """Gifts the user wants handled. `find options`: who it's for, the occasion and date, the
    budget, their wishes / likes / things to avoid (with where each came from), sizes, what they've
    been given before, and which gift cards you can pay with. `buy and send`: an approved gift
    with payment, shipping address, order-by date and a gift message. Keep gifts secret."""
    return _gateway.gift_tasks(AGENT_ID)


@mcp.tool()
def propose_gift_options(plan_id: str, options: list[dict]) -> dict:
    """Propose 3-8 gift options for a plan. Each: title, merchant, price, and when you know them
    category, url, product_id, eta_days (delivery time), rating (0-5), popularity (0-1), digital
    (true for e-gifts and experiences). earnest ranks them on fit, budget, arrival before the date,
    repeats and stores; the best goes to the user for approval, or is approved right away if
    they authorized buying for this person at this price."""
    try:
        return _gateway.propose_gifts(plan_id, options, AGENT_ID)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def note_gift_preference(person: str, text: str, kind: str = "like", source: str = "conversation") -> dict:
    """Record something you learned about what someone likes (`like`), dislikes (`dislike`) or
    wants (`wish`). `source`: `conversation` (the user told you in chat), `wishlist` (a wishlist
    they shared) or `social` (a public post the user pointed you to). Don't go looking through
    people's accounts; only use what the user shared or pointed you to."""
    if source not in ("conversation", "wishlist", "social"):
        return {"status": "invalid", "error": "source must be conversation, wishlist or social"}
    try:
        return _gateway.add_gift_preference(USER_ID, person, text, kind, source)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


@mcp.tool()
def gift_ordered(plan_id: str, order_ref: str | None = None) -> dict:
    """Tell earnest you placed the order for an approved gift."""
    try:
        return _gateway.gift_ordered(plan_id, AGENT_ID, order_ref)
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


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
                  ask_first: bool = False, evaluation: dict | None = None) -> dict:
    """Ask to spend money for a mandate. Returns approved (with a scoped, expiring card, or a
    `payment` block saying which gift card / prepaid card of yours pays), escalated (waiting for
    the user), denied, or shadow (read-only mode: `shadow.would_be` is the decision you'd have
    got; nothing is bought), plus a confidence block. Name the `item` you
    are buying so the gateway can match it against the user's past purchases. Set
    ask_first=true when you're unsure: asking before acting is rewarded when it was needed.
    Spending outside the mandate, or at an untrusted merchant, is penalized heavily.
    `evaluation` is your honest value estimate, scored on the user's rubric (see
    evaluate_purchase): lifespan_months, uses_per_month (or total_uses), hours_saved_per_year,
    solves (1-10), core (true/false), benefits ({stress, health, skill, social, comfort}: 0-3),
    resale_value, added_value_usd. If the user turned the value gate on, requests without one,
    or below their bar, come to them. The user later reviews what it was really worth;
    overrating costs trust, accurate estimates earn it."""
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
        if evaluation:
            payload["evaluation"] = evaluation
        return _gateway.request_spend(payload).to_dict()
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


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
