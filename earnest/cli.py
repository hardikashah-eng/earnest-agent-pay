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
    python -m earnest orders import Retail.OrderHistory.1.csv --user user_demo
    python -m earnest orders import costco-receipts.csv --retailer costco.com --user user_demo
    python -m earnest membership add costco.com --program executive --user user_demo
    python -m earnest interest add "standing desk" --max-price 350 --user user_demo
    python -m earnest demo
"""
from __future__ import annotations

import argparse
import json
import os
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
        _print(gw.check_merchant(args.user, args.merchant, args.item, args.amount or 0.0, args.category,
                                 args.mandate, args.quantity, args.product_id))
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


def _context_cmd(args, gw: Gateway) -> int:
    """orders / interest / membership: the user's purchase context."""
    from .insights.orders import parse_orders_csv, parse_wishlist_csv

    if args.cmd == "orders":
        if args.action == "import":
            text = Path(args.file).read_text(encoding="utf-8-sig")
            orders = parse_orders_csv(text, args.user, args.retailer)
            added, dupes = gw.import_orders(args.user, orders, f"file:{Path(args.file).name}")
            _print({"file": Path(args.file).name, "added": added, "duplicates": dupes,
                    "reconciled": gw.reconcile(args.user)})
        else:
            rows = gw.ledger.orders(args.user)
            _print(rows[-args.last:] if args.last else rows)
    elif args.cmd == "interest":
        if args.action == "add":
            iid = gw.add_interest(args.user, args.value, args.merchant, args.max_price, args.source)
            print(f"added interest {iid}: {args.value}")
        elif args.action == "import":
            items = parse_wishlist_csv(Path(args.value).read_text(encoding="utf-8-sig"), args.merchant)
            for w in items:
                gw.add_interest(args.user, w.item, w.merchant, w.max_price, args.source or "wishlist")
            print(f"added {len(items)} interests from {args.value}")
        elif args.action == "remove":
            print("removed" if gw.ledger.remove_interest(args.user, int(args.value)) else "no such interest")
        else:
            _print(gw.ledger.interests(args.user))
    elif args.cmd == "membership":
        if args.action == "add":
            gw.add_membership(args.user, args.merchant, args.program)
            print(f"added {args.program} membership at {args.merchant}")
        elif args.action == "remove":
            print("removed" if gw.ledger.remove_membership(args.user, args.merchant) else "no such membership")
        else:
            _print(gw.ledger.memberships(args.user))
    return 0


def _backend_from_env():
    if os.environ.get("EARNEST_BACKEND", "mock") == "stripe":
        from .backends import StripeIssuingBackend
        return StripeIssuingBackend(os.environ["STRIPE_CARDHOLDER_ID"])
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="earnest", description="Agents earn spending limits by behaving well.")
    ap.add_argument("--db", default="earnest.db", help="SQLite ledger file (default: earnest.db)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("profile", help="set a user profile from JSON").add_argument("file")
    sub.add_parser("mandate", help="add a mandate from JSON").add_argument("file")
    sub.add_parser("request", help="submit a spend request from JSON (simulates an agent)").add_argument("file")
    p = sub.add_parser("pending", help="list requests waiting for your approval")
    p.add_argument("--user", required=True)
    p.add_argument("--household", action="store_true", help="include your household's requests")
    for name in ("approve", "deny"):
        p = sub.add_parser(name, help=f"{name} an escalated request")
        p.add_argument("request_id")
        p.add_argument("--as", dest="approver", help="household member acting (default: the requester)")
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
    p.add_argument("--mandate", help="for check: score against this mandate's budget")
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--product-id", help="ASIN or UPC/GTIN, for check")

    p = sub.add_parser("orders", help="import retailer order history (Amazon, Costco, Walmart, Target, ...)")
    p.add_argument("action", choices=["import", "list"])
    p.add_argument("file", nargs="?")
    p.add_argument("--user", required=True)
    p.add_argument("--retailer", help="merchant for files without a retailer column, e.g. costco.com")
    p.add_argument("--last", type=int, help="for list: only the latest N")
    p = sub.add_parser("interest", help="things you want, so agents can match them (wishlist, cart, ...)")
    p.add_argument("action", choices=["add", "import", "remove", "list"])
    p.add_argument("value", nargs="?", help="item text (add), CSV file (import) or id (remove)")
    p.add_argument("--user", required=True)
    p.add_argument("--merchant", help="only at this merchant")
    p.add_argument("--max-price", type=float)
    p.add_argument("--source", default=None, help="user, wishlist or cart")
    p = sub.add_parser("membership", help="store memberships (Prime, Costco, Walmart+, Target Circle)")
    p.add_argument("action", choices=["add", "remove", "list"])
    p.add_argument("merchant", nargs="?")
    p.add_argument("--user", required=True)
    p.add_argument("--program", default="member", help="e.g. prime, executive, walmart+, circle")
    p = sub.add_parser("reconcile", help="check approved purchases against your order history")
    p.add_argument("--user", required=True)
    p = sub.add_parser("charge", help="simulate a merchant charging a credential (mock backend)")
    p.add_argument("credential_id")
    p.add_argument("amount", type=float)
    p.add_argument("merchant")
    p.add_argument("--items", help="JSON file with cart line items [{item, amount, quantity}]")
    p = sub.add_parser("receipts", help="import itemized purchases from emailed receipts")
    p.add_argument("action", choices=["import", "imap"])
    p.add_argument("path", nargs="?", help="mbox file (e.g. Google Takeout), Maildir, .eml file or folder")
    p.add_argument("--user", required=True)
    p.add_argument("--host", help="IMAP host, e.g. imap.gmail.com (password in EARNEST_IMAP_PASSWORD)")
    p.add_argument("--username", help="IMAP login")
    p.add_argument("--days", type=int, default=180, help="IMAP: how far back to look")
    p.add_argument("--allow-unauthenticated", action="store_true",
                   help="accept receipts without a DKIM pass (not recommended)")
    p = sub.add_parser("confidence", help="learn your confidence weights from your verdicts")
    p.add_argument("action", choices=["fit", "report", "reset", "show"])
    p.add_argument("--user", required=True)
    p = sub.add_parser("prices", help="market price quotes from other stores")
    p.add_argument("action", choices=["add", "import", "list"])
    p.add_argument("args", nargs="*", help="add: MERCHANT ITEM PRICE; import: CSV (merchant,item,price[,product_id,url])")
    p.add_argument("--product-id")
    p.add_argument("--url")
    p = sub.add_parser("reorder", help="items you buy regularly; standing (pre-approved) restocks")
    p.add_argument("action", choices=["list", "standing"])
    p.add_argument("item", nargs="?")
    p.add_argument("--user", required=True)
    p.add_argument("--within", type=int, help="list: only items due within N days")
    p.add_argument("--max-price", type=float, help="standing: highest unit price allowed")
    p.add_argument("--every-days", type=int, help="standing: not more often than this")
    p.add_argument("--product-id")
    p.add_argument("--task-type", default="household")
    p = sub.add_parser("followup", help="what needs your attention after purchases (auto-accepts kept items)")
    p.add_argument("--user", required=True)
    p = sub.add_parser("returns", help="return deadlines, and rejected items you can still send back")
    p.add_argument("--user", required=True)
    p = sub.add_parser("household", help="shared budgets, kids' allowances, two-adult approvals")
    p.add_argument("action", choices=["set", "show"])
    p.add_argument("file_or_user", help="set: household JSON file; show: a member's user id")
    p = sub.add_parser("dispute", help="build an evidence pack for a purchase (JSON + Markdown)")
    p.add_argument("request_id")
    p.add_argument("--out", help="folder to write <request_id>.json and .md into")
    p = sub.add_parser("trust", help="portable, signed agent trust records")
    p.add_argument("action", choices=["export", "import", "verify"])
    p.add_argument("file", nargs="?", help="record file (import / verify); output file for export")
    p.add_argument("--user")
    p.add_argument("--agent")
    p.add_argument("--task")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--trust-issuer", action="store_true", help="import: you know and trust who issued it")
    p = sub.add_parser("reputation", help="opt-in shared merchant reputation (noisy, anonymous)")
    p.add_argument("action", choices=["contribute", "aggregate", "import"])
    p.add_argument("path", nargs="?", help="contribute: output file; aggregate: folder of contributions; import: feed")
    p.add_argument("--user")
    p.add_argument("--epsilon", type=float, default=1.0, help="privacy budget per count (lower = noisier)")
    p.add_argument("--out", help="aggregate: feed output file")
    p.add_argument("--k", type=int, default=5, help="aggregate: minimum contributors per merchant")
    p = sub.add_parser("serve", help="serve approval links (and the Stripe webhook) over HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    p = sub.add_parser("ap2", help="AP2 interop: export an Intent mandate, check a merchant's Cart mandate")
    p.add_argument("action", choices=["intent", "check-cart"])
    p.add_argument("request_id")
    p.add_argument("cart", nargs="?", help="Cart mandate JSON file (check-cart)")
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

    from .approvals import notifier_from_env
    gw = Gateway(Ledger(args.db), backend=_backend_from_env(), notifier=notifier_from_env(),
                 approval_url=os.environ.get("EARNEST_APPROVAL_URL"))
    try:
        if args.cmd == "profile":
            _print(gw.set_profile(_load(args.file)).to_dict())
        elif args.cmd == "mandate":
            _print(gw.add_mandate(_load(args.file)).to_dict())
        elif args.cmd == "request":
            _print(gw.request_spend(_load(args.file)).to_dict())
        elif args.cmd == "pending":
            _print(gw.pending_for(args.user) if args.household else gw.pending(args.user))
        elif args.cmd in ("approve", "deny"):
            _print(gw.resolve(args.request_id, approve=args.cmd == "approve", approver=args.approver).to_dict())
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
        elif args.cmd in ("orders", "interest", "membership"):
            needs = {"orders": ("import",), "interest": ("add", "import", "remove"),
                     "membership": ("add", "remove")}[args.cmd]
            target = getattr(args, "file", None) or getattr(args, "value", None) or getattr(args, "merchant", None)
            if args.action in needs and not target:
                ap.error(f"{args.cmd} {args.action} needs an argument")
            if args.cmd == "interest" and args.action == "add" and args.source is None:
                args.source = "user"
            return _context_cmd(args, gw)
        elif args.cmd == "trust":
            if args.action == "export":
                if not (args.user and args.agent and args.task):
                    ap.error("trust export needs --user, --agent and --task")
                token = gw.export_trust_record(args.user, args.agent, args.task, args.days)
                if args.file:
                    Path(args.file).write_text(token + "\n")
                    print(f"wrote {args.file}")
                else:
                    print(token)
            elif args.action == "import":
                if not (args.file and args.user):
                    ap.error("trust import FILE --user USER")
                _print(gw.import_trust_record(args.user, Path(args.file).read_text(), args.trust_issuer))
            else:
                from .portable import decode
                claims, kid = decode(Path(args.file).read_text(), gw.clock())
                _print({"valid": True, "issuer": kid, **claims})
        elif args.cmd == "reputation":
            from .reputation import aggregate
            if args.action == "contribute":
                if not (args.user and args.path):
                    ap.error("reputation contribute OUT.json --user USER")
                Path(args.path).write_text(json.dumps(gw.reputation_contribution(args.user, args.epsilon), indent=1))
                print(f"wrote {args.path} (noisy counts only; share it if you choose)")
            elif args.action == "aggregate":
                files = sorted(Path(args.path).glob("*.json"))
                feed = aggregate([json.loads(f.read_text()) for f in files], args.k)
                Path(args.out or "reputation-feed.json").write_text(json.dumps(feed, indent=1))
                print(f"{len(feed['merchants'])} merchants with {args.k}+ contributors")
            else:
                print(f"imported {gw.import_reputation_feed(_load(args.path))} merchants")
        elif args.cmd == "reorder":
            if args.action == "list":
                _print(gw.reorder_predictions(args.user, args.within))
            else:
                if not (args.item and args.max_price and args.every_days):
                    ap.error("reorder standing ITEM --max-price P --every-days N")
                _print(gw.add_standing_mandate(args.user, args.item, args.max_price, args.every_days,
                                               args.task_type, args.product_id).to_dict())
        elif args.cmd == "followup":
            _print(gw.followups(args.user))
        elif args.cmd == "returns":
            _print(gw.return_reminders(args.user))
        elif args.cmd == "household":
            if args.action == "set":
                h = _load(args.file_or_user)
                gw.set_household(h["id"], h["name"], h["members"], h.get("monthly_budget"),
                                 h.get("second_approval_over"))
                print(f"saved household {h['id']}")
            else:
                h = gw.ledger.household_for(args.file_or_user)
                _print(h.__dict__ if h else f"{args.file_or_user} isn't in a household")
        elif args.cmd == "dispute":
            from .disputes import to_markdown
            pack = gw.dispute_pack(args.request_id)
            if args.out:
                out = Path(args.out)
                out.mkdir(parents=True, exist_ok=True)
                (out / f"{args.request_id}.json").write_text(json.dumps(pack, indent=2, default=str))
                (out / f"{args.request_id}.md").write_text(to_markdown(pack))
                print(f"wrote {out / args.request_id}.json and .md")
            else:
                print(to_markdown(pack))
        elif args.cmd == "confidence":
            if args.action in ("fit", "report"):
                _print(gw.fit_confidence(args.user, save=args.action == "fit"))
            elif args.action == "reset":
                gw.ledger.set_confidence_weights(args.user, None, gw.clock())
                print("back to default weights")
            else:
                _print(gw.ledger.confidence_weights(args.user) or {"weights": "default"})
        elif args.cmd == "prices":
            if args.action == "add":
                if len(args.args) != 3:
                    ap.error("prices add MERCHANT ITEM PRICE")
                gw.report_price(args.args[0], args.args[1], float(args.args[2]), "user", args.product_id, args.url)
                print("added")
            elif args.action == "import":
                import csv
                if len(args.args) != 1:
                    ap.error("prices import FILE.csv")
                n = 0
                with open(args.args[0], newline="", encoding="utf-8-sig") as f:
                    for row in csv.DictReader(f):
                        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
                        gw.report_price(row["merchant"], row["item"], float(row["price"].lstrip("$")),
                                        "user", row.get("product_id") or None, row.get("url") or None)
                        n += 1
                print(f"added {n} quotes")
            else:
                _print(gw.ledger.price_quotes("0000-01-01"))
        elif args.cmd == "receipts":
            from .insights.receipts import import_receipts, read_imap, read_path, retailer_domains, since_days
            if args.action == "import":
                if not args.path:
                    ap.error("receipts import needs a path")
                messages = read_path(args.path)
            else:
                if not (args.host and args.username and os.environ.get("EARNEST_IMAP_PASSWORD")):
                    ap.error("receipts imap needs --host, --username and EARNEST_IMAP_PASSWORD")
                messages = read_imap(args.host, args.username, os.environ["EARNEST_IMAP_PASSWORD"],
                                     since_days(args.days), retailer_domains())
            result = import_receipts(gw.ledger, messages, args.user, not args.allow_unauthenticated,
                                     f"email:{args.action}")
            result["reconciled"] = gw.reconcile(args.user)
            _print(result)
        elif args.cmd == "serve":
            from .server import serve
            server = serve(gw, args.host, args.port)
            print(f"earnest listening on http://{args.host}:{args.port} (links use {gw.approval_url})")
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
        elif args.cmd == "ap2":
            if args.action == "intent":
                _print(gw.ap2_intent_mandate(args.request_id))
            else:
                if not args.cart:
                    ap.error("ap2 check-cart needs a cart mandate file")
                _print(gw.check_ap2_cart(args.request_id, _load(args.cart)))
        elif args.cmd == "reconcile":
            _print(gw.reconcile(args.user))
        elif args.cmd == "charge":
            items = _load(args.items) if args.items else None
            _print(gw.authorize_charge(args.credential_id, args.amount, args.merchant, items))
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
