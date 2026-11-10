"""Command line for the user side (and for simulating an agent's request).

    python -m earnest profile examples/profile-frugal.json
    python -m earnest mandate examples/mandate-software.json
    python -m earnest request examples/request-ok.json
    python -m earnest pending --user user_demo
    python -m earnest approve req_123
    python -m earnest outcome req_123 accepted
    python -m earnest limits --user user_demo --agent agent_shopper --task software
    python -m earnest merchant check amazon.com --user user_demo --item "usb-c cable"
    python -m earnest merchant distrust sketchy-deals.shop --user user_demo
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


def _insights_cmd(args, gw: Gateway) -> int:
    from .insights import TransactionStore, import_file, sync_folder, top_insights, watch_folder

    store = TransactionStore(gw.ledger.db)
    if args.cmd == "import":
        _print(import_file(store, args.file, args.user).to_dict())
    elif args.cmd == "sync":
        if args.watch:
            print(f"watching {args.inbox} every {args.every:g}s (Ctrl+C to stop)")
            try:
                watch_folder(store, args.inbox, args.user, args.every, on_result=lambda r: _print(r.to_dict()))
            except KeyboardInterrupt:
                pass
        else:
            results = sync_folder(store, args.inbox, args.user)
            _print([r.to_dict() for r in results] or f"no new files in {args.inbox}")
            if any(r.error for r in results):
                return 1
    elif args.cmd == "imports":
        _print(store.imports(args.user))
    elif args.cmd == "insights":
        txs = store.all(args.user)
        if not txs:
            print("no transactions yet: run `earnest import <statement.csv> --user ...` or `earnest sync`",
                  file=sys.stderr)
            return 2
        report = top_insights(txs, args.month, store.dismissed(args.user), n=args.top)
        if args.json:
            _print(report.to_dict())
        else:
            print(report.to_text())
    elif args.cmd == "dismiss-insight":
        store.dismiss(args.user, args.key)
        print(f"dismissed {args.key}")
    return 0


def _merchant_cmd(args, gw: Gateway) -> int:
    if args.action == "check":
        _print(gw.check_merchant(args.user, args.merchant, args.item, args.amount or 0.0, args.category))
    elif args.action == "trust":
        gw.trust_merchant(args.user, args.merchant, args.reason or "user")
        print(f"trusted {args.merchant}")
    elif args.action == "distrust":
        gw.distrust_merchant(args.user, args.merchant, args.reason or "user")
        print(f"untrusted {args.merchant}")
    elif args.action == "forget":
        n = gw.ledger.forget_merchant(args.user, args.merchant)
        print(f"removed {args.merchant} from your lists" if n else f"{args.merchant} wasn't on your lists")
    elif args.action == "list":
        _print({"yours": gw.ledger.merchant_lists(args.user),
                "verified_defaults": [m.name for m in gw.merchants.verified],
                "defaults_version": gw.merchants.version})
    elif args.action == "import-blocklist":
        added = 0
        for line in Path(args.merchant).read_text(encoding="utf-8").splitlines():
            entry = line.split("#", 1)[0].strip()
            if entry:
                gw.distrust_merchant(args.user, entry, args.reason or f"blocklist:{Path(args.merchant).name}")
                added += 1
        print(f"added {added} untrusted merchants from {args.merchant}")
    return 0


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
    p = sub.add_parser("merchant", help="check a merchant, or manage your trusted / untrusted lists")
    p.add_argument("action", choices=["check", "trust", "distrust", "forget", "list", "import-blocklist"])
    p.add_argument("merchant", nargs="?", help="domain or name (a file path for import-blocklist)")
    p.add_argument("--user", required=True)
    p.add_argument("--item", help="for check: the item, to match against past purchases")
    p.add_argument("--amount", type=float)
    p.add_argument("--category")
    p.add_argument("--reason")
    p = sub.add_parser("demo", help="run the two-agent simulation and write a chart")
    p.add_argument("--out", default="demo/limits.svg")
    p.add_argument("--days", type=int, default=40)

    # --- spending insights ---
    p = sub.add_parser("import", help="manually import a card statement CSV")
    p.add_argument("file")
    p.add_argument("--user", required=True)
    p = sub.add_parser("sync", help="automatically import new CSVs dropped into an inbox folder")
    p.add_argument("--user", required=True)
    p.add_argument("--inbox", default="statements/inbox")
    p.add_argument("--watch", action="store_true", help="keep running and sync on an interval")
    p.add_argument("--every", type=float, default=3600, help="seconds between syncs with --watch")
    p = sub.add_parser("imports", help="list past imports")
    p.add_argument("--user", required=True)
    p = sub.add_parser("insights", help="top spending insights for a month")
    p.add_argument("--user", required=True)
    p.add_argument("--month", help="YYYY-MM (default: latest month with data)")
    p.add_argument("--top", type=int, default=5)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("dismiss-insight", help="hide an insight you already know about")
    p.add_argument("key")
    p.add_argument("--user", required=True)

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
        elif args.cmd == "merchant":
            if args.action != "list" and not args.merchant:
                ap.error(f"merchant {args.action} needs a merchant")
            return _merchant_cmd(args, gw)
        elif args.cmd == "events":
            _print([dict(r) for r in gw.ledger.events(args.user, args.agent)])
        elif args.cmd in ("import", "sync", "imports", "insights", "dismiss-insight"):
            return _insights_cmd(args, gw)
    except ValidationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    finally:
        gw.ledger.close()
    return 0
