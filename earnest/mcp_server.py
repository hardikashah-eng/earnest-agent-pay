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
def request_spend(mandate_id: str, amount: float, merchant: str, category: str, reason: str,
                  recurring: bool = False, international: bool = False, ask_first: bool = False) -> dict:
    """Ask to spend money for a mandate. Returns approved (with a scoped, expiring card),
    escalated (waiting for the user) or denied. Set ask_first=true when you're unsure:
    asking before acting is rewarded when it was needed. Spending outside the mandate is
    penalized heavily."""
    try:
        return _gateway.request_spend({
            "agent_id": AGENT_ID, "mandate_id": mandate_id, "amount": amount, "merchant": merchant,
            "category": category, "reason": reason, "recurring": recurring,
            "international": international, "ask_first": ask_first,
        }).to_dict()
    except ValidationError as e:
        return {"status": "invalid", "error": str(e)}


if __name__ == "__main__":
    mcp.run()
