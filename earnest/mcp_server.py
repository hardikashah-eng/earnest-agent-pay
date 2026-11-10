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

from .gateway import Gateway
from .ledger import Ledger
from .models import ValidationError

USER_ID = os.environ.get("EARNEST_USER_ID", "user_demo")
AGENT_ID = os.environ.get("EARNEST_AGENT_ID", "agent_default")
DB_PATH = os.environ.get("EARNEST_DB", "earnest.db")

mcp = FastMCP("earnest")
_gateway = Gateway(Ledger(DB_PATH))


@mcp.tool()
def list_mandates() -> list[dict]:
    """List the tasks you're allowed to spend on, with their budgets and rules."""
    return [m.to_dict() for m in _gateway.ledger.list_mandates(USER_ID)]


@mcp.tool()
def get_limits(task_type: str) -> dict:
    """Your current trust tier and spending limits for a task type (e.g. 'software')."""
    return _gateway.limits(USER_ID, AGENT_ID, task_type)


@mcp.tool()
def check_merchant(merchant: str, item: str = "", amount: float = 0.0, category: str = "") -> dict:
    """Check a merchant before you buy. Returns its trust status (verified, user_trusted,
    unknown, suspicious or untrusted), whether the user has bought here before (and the same
    or a similar item), and an overall confidence level. Untrusted merchants and lookalikes
    of well-known stores are denied; buying from them costs trust. Prefer verified merchants
    and ones the user already buys from."""
    return _gateway.check_merchant(USER_ID, merchant, item or None, amount, category or None)


@mcp.tool()
def request_spend(mandate_id: str, amount: float, merchant: str, category: str, reason: str,
                  item: str = "", recurring: bool = False, international: bool = False,
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
        return _gateway.request_spend(payload).to_dict()
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


if __name__ == "__main__":
    mcp.run()
