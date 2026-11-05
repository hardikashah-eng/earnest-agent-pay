"""Command line for the user side (and for simulating an agent's request).

    python -m earnest profile examples/profile-frugal.json
    python -m earnest mandate examples/mandate-software.json
    python -m earnest request examples/request-ok.json
    python -m earnest pending --user user_demo
    python -m earnest approve req_123
    python -m earnest outcome req_123 accepted
    python -m earnest limits --user user_demo --agent agent_shopper --task software
    python -m earnest demo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .gateway import Gateway
from .ledger import Ledger
from .models import ValidationError


def _load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="earnest", description="Agents earn spending limits by behaving well.")
    ap.add_argument("--db", default="earnest.db", help="SQLite ledger file (default: earnest.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profile", help="set a user profile from JSON").add_argument("file")
    sub.add_parser("mandate", help="add a mandate from JSON").add_argument("file")
    sub.add_parser("request", help="submit a spend request from JSON (simulates an agent)").add_argument("file")
    p = sub.add_parser("pending", help="list requests waiting for your approval")
    p.add_argument("--user", required=True)
    sub.add_parser("approve", help="approve an escalated request").add_argument("request_id")
    sub.add_parser("deny", help="deny an escalated request").add_argument("request_id")
    p = sub.add_parser("outcome", help="report your verdict on a purchase")
    p.add_argument("request_id")
    p.add_argument("verdict", choices=["accepted", "rejected", "disputed"])
    p.add_argument("--late", action="store_true", help="it arrived late")
    p.add_argument("--note")
    p = sub.add_parser("limits", help="show an agent's current trust tier and limits")
    p.add_argument("--user", required=True)
    p.add_argument("--agent", required=True)
    p.add_argument("--task", required=True, help="task type, e.g. software")
    p = sub.add_parser("events", help="show score history")
    p.add_argument("--user", required=True)
    p.add_argument("--agent")
    p = sub.add_parser("demo", help="run the two-agent simulation and write a chart")
    p.add_argument("--out", default="demo/limits.svg")
    p.add_argument("--days", type=int, default=40)

    args = ap.parse_args(argv)

    if args.cmd == "demo":
        from .simulate import run
        run(out=args.out, days=args.days)
        return 0

    gw = Gateway(Ledger(args.db))
    try:
        if args.cmd == "profile":
            _print(gw.set_profile(_load(args.file)).to_dict())
        elif args.cmd == "mandate":
            _print(gw.add_mandate(_load(args.file)).to_dict())
        elif args.cmd == "request":
            _print(gw.request_spend(_load(args.file)).to_dict())
        elif args.cmd == "pending":
            _print(gw.pending(args.user))
        elif args.cmd in ("approve", "deny"):
            _print(gw.resolve(args.request_id, approve=args.cmd == "approve").to_dict())
        elif args.cmd == "outcome":
            outcome = {"request_id": args.request_id, "verdict": args.verdict, "on_time": not args.late}
            if args.note:
                outcome["note"] = args.note
            _print(gw.report_outcome(outcome))
        elif args.cmd == "limits":
            _print(gw.limits(args.user, args.agent, args.task))
        elif args.cmd == "events":
            _print([dict(r) for r in gw.ledger.events(args.user, args.agent)])
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        gw.ledger.close()
    return 0
