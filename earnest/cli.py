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
    python -m earnest account open --user user_demo --agent agent_shopper --allowance 25
    python -m earnest account add-card --user user_demo --agent agent_shopper --kind gift_card \
        --merchant target.com --balance 50 --last4 4417
    python -m earnest account freeze --user user_demo              # every agent, at once
    python -m earnest import statement.csv --user user_demo --card "Visa 4417"
    python -m earnest shadow start --user user_demo --agent agent_shopper --card "Visa 4417"
    python -m earnest shadow report --user user_demo --agent agent_shopper
    python -m earnest gifts import-calendar birthdays.ics --user user_demo
    python -m earnest gifts add-person Sam --user user_demo --relationship friend --likes "hiking;coffee"
    python -m earnest gifts policy --user user_demo --agent agent_shopper --annual-budget 1500
    python -m earnest gifts plan --user user_demo                # run daily: plans upcoming dates
    python -m earnest gifts approve g_123 --user user_demo
    python -m earnest eval policy --user user_demo --goal save_time --hourly-value 50 --threshold 7.5 --gate on
    python -m earnest eval review req_123 --uses-per-month 8 --hours-saved 20 --solves 7
    python -m earnest eval agent --user user_demo --agent agent_shopper
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
        _print(import_file(store, args.file, args.user, card=args.card).to_dict())
    elif args.cmd == "sync":
        if args.watch:
            print(f"watching {args.inbox} every {args.every:g}s (Ctrl+C to stop)")
            try:
                watch_folder(store, args.inbox, args.user, args.every, on_result=lambda r: _print(r.to_dict()),
                             card=args.card)
            except KeyboardInterrupt:
                pass
        else:
            results = sync_folder(store, args.inbox, args.user, args.card)
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


def _account_cmd(args, ap, gw: Gateway) -> int:
    a = args.action
    needs_agent = a not in ("list", "freeze", "refill", "offers", "refund", "settle")
    if needs_agent and not args.agent:
        ap.error(f"account {a} needs --agent")
    if a not in ("refund",) and not args.user:
        ap.error(f"account {a} needs --user")
    if a == "open":
        _print(gw.open_account(args.user, args.agent, args.allowance or 0.0, args.max_allowance,
                               args.topup_to, args.every_days))
    elif a == "show":
        _print(gw.account(args.user, args.agent))
    elif a == "list":
        _print([gw.account(args.user, x["agent_id"]) for x in gw.ledger.accounts(args.user)])
    elif a == "topup":
        if not args.amount:
            ap.error("account topup needs --amount")
        _print(gw.top_up(args.user, args.agent, args.amount))
    elif a == "add-card":
        if not (args.kind and args.balance and args.last4):
            ap.error("account add-card needs --kind, --balance and --last4")
        _print(gw.add_card(args.user, args.agent, args.kind, args.balance, args.last4, args.merchant, args.label))
    elif a in ("set-balance", "remove-card"):
        if not args.id:
            ap.error(f"account {a} needs a funding id (see `earnest account show`)")
        if a == "set-balance":
            if args.balance is None:
                ap.error("account set-balance needs --balance")
            _print(gw.set_card_balance(args.user, args.agent, args.id, args.balance))
        else:
            gw.remove_card(args.user, args.agent, args.id)
            print(f"removed {args.id}")
    elif a == "freeze":
        _print(gw.freeze(args.user, args.agent, args.reason or "frozen by you"))
    elif a == "unfreeze":
        _print(gw.unfreeze(args.user, args.agent))
    elif a == "refill":
        _print(gw.refill_accounts(args.user) or "no refills due")
    elif a == "offers":
        _print(gw.account_offers(args.user) or "no raises on offer")
    elif a == "raise":
        _print(gw.accept_raise(args.user, args.agent))
    elif a == "refund":
        if not args.id:
            ap.error("account refund needs a request id")
        _print(gw.refund(args.id, args.amount))
    elif a == "settle":
        _print(gw.settle_accounts(args.user) or "nothing to release")
    elif a == "statement":
        st = gw.account_statement(args.user, args.agent, args.days)
        _print(st if args.json else st["summary"])
    return 0


def _shadow_cmd(args, ap, gw: Gateway) -> int:
    a = args.action
    if a == "review":
        if not (args.id and args.answer):
            ap.error("shadow review REQUEST_ID yes|no")
        _print(gw.shadow_review(args.id, args.answer == "yes"))
        return 0
    if not args.user:
        ap.error(f"shadow {a} needs --user")
    if a in ("start", "report", "live") and not args.agent:
        ap.error(f"shadow {a} needs --agent")
    if a in ("start", "profile", "replay") and not args.card:
        ap.error(f"shadow {a} needs --card (see `earnest shadow cards`)")
    if a == "start":
        _print(gw.start_shadow(args.user, args.agent, args.card))
    elif a == "cards":
        _print(gw.ledger.cards(args.user) or "no labelled cards yet: import with --card")
    elif a == "profile":
        _print(gw.card_profile(args.user, args.card))
    elif a == "replay":
        _print(gw.shadow_replay(args.user, args.card, args.task, args.agent, args.days))
    elif a == "pending":
        _print(gw.shadow_pending(args.user) or "nothing to review")
    elif a == "match":
        _print(gw.shadow_match(args.user) or "no new matches")
    elif a == "report":
        _print(gw.shadow_report(args.user, args.agent))
    elif a == "live":
        _print(gw.go_live(args.user, args.agent, args.allowance))
    return 0


def _split(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(";") if x.strip()]


def _gifts_cmd(args, ap, gw: Gateway) -> int:
    a, target, value = args.action, args.target, args.value
    user = args.user
    if not user:
        ap.error(f"gifts {a} needs --user")

    def need(what, v):
        if v is None:
            ap.error(f"gifts {a} needs {what}")
        return v
    if a == "add-person":
        sizes = dict(x.split("=", 1) for x in args.size or [] if "=" in x)
        _print(gw.add_person(user, need("a name", target), args.relationship, budget=args.budget,
                             auto_buy_max=args.auto_buy_max, address=args.address, sizes=sizes, notes=args.notes,
                             likes=_split(args.likes), dislikes=_split(args.dislikes), wishes=_split(args.wishes)))
    elif a == "person":
        _print(gw.person_card(user, need("a name", target)))
    elif a == "people":
        _print([{"name": p["name"], "relationship": p["relationship"], "budget": p["budget"],
                 "auto_buy_max": p["auto_buy_max"]} for p in gw.ledger.people(user)])
    elif a == "add-date":
        _print(gw.add_occasion(user, need("a name", target), args.kind, need("--date", args.date), args.label))
    elif a == "like":
        _print(gw.add_gift_preference(user, need("a name", target), need("the preference text", value),
                                      args.kind_pref, args.source))
    elif a == "forget-like":
        p = gw.ledger.person(user, need("a name", target))
        if p is None or not gw.ledger.remove_pref(p["id"], need("the preference text", value)):
            print("not found")
            return 2
        print("removed")
    elif a == "import-people":
        _print(gw.import_people(user, Path(need("a CSV file", target)).read_text(encoding="utf-8-sig")))
    elif a == "import-calendar":
        _print(gw.import_calendar(user, Path(need("an .ics file", target)).read_text(encoding="utf-8-sig")))
    elif a == "policy":
        rel = dict(x.split("=", 1) for x in args.budget_for or [] if "=" in x)
        auto = None if args.auto_buy is None else args.auto_buy == "on"
        _print(gw.set_gift_policy(user, agent_id=args.agent, lead_days=args.lead_days,
                                  annual_budget=args.annual_budget, auto_buy=auto,
                                  relationship_budgets={k: float(v) for k, v in rel.items()} or None))
    elif a == "upcoming":
        _print(gw.upcoming_gifts(user, args.days) or "nothing coming up")
    elif a == "plan":
        _print(gw.plan_gifts(user) or "no new plans")
    elif a == "plans":
        _print([gw.gift_plan_view(p["id"]) for p in gw.ledger.plans(user)])
    elif a == "show":
        _print(gw.gift_plan_view(need("a plan id", target)))
    elif a == "brief":
        _print(gw.gift_brief(need("a plan id", target)))
    elif a == "propose":
        options = json.loads(Path(args.options).read_text()) if args.options else None
        _print(gw.propose_gifts(need("a plan id", target), options, args.agent))
    elif a == "catalog":
        print(f"loaded {gw.load_gift_catalog(json.loads(Path(need('a JSON file', target)).read_text()))} items")
    elif a == "budget":
        _print(gw.set_gift_budget(need("a plan id", target), float(need("an amount", value))))
    elif a == "approve":
        _print(gw.approve_gift(need("a plan id", target), args.option))
    elif a == "skip":
        _print(gw.skip_gift(need("a plan id", target), value or "skipped by you"))
    elif a == "feedback":
        _print(gw.gift_feedback(need("a plan id", target), need("loved|liked|returned|disliked", value)))
    return 0


def _eval_inputs(args) -> dict:
    d = {}
    for k, attr in (("lifespan_months", "lifespan_months"), ("uses_per_month", "uses_per_month"),
                    ("total_uses", "total_uses"), ("hours_saved_per_year", "hours_saved"), ("solves", "solves"),
                    ("resale_value", "resale"), ("added_value_usd", "added_value")):
        v = getattr(args, attr)
        if v is not None:
            d[k] = v
    if args.core is not None:
        d["core"] = args.core == "yes"
    benefits = dict(x.split("=", 1) for x in args.benefit or [] if "=" in x)
    if benefits:
        d["benefits"] = {k: float(v) for k, v in benefits.items()}
    scores = dict(x.split("=", 1) for x in args.score or [] if "=" in x)
    if scores:
        d["scores"] = {k: float(v) for k, v in scores.items()}
    return d


def _eval_cmd(args, ap, gw: Gateway) -> int:
    a = args.action
    if a == "policy":
        if not args.user:
            ap.error("eval policy needs --user")
        weights = None
        if args.weights:
            try:
                weights = {k: float(v) for k, v in (x.split("=", 1) for x in args.weights.split(","))}
            except ValueError:
                ap.error("--weights looks like price=30,time_saved=25,utility=30,added_value=15")
        _print(gw.set_eval_policy(args.user, args.goal, weights, args.threshold, args.hourly_value,
                                  None if args.gate is None else args.gate == "on", args.net_check_over))
    elif a == "score":
        if not (args.user and args.price):
            ap.error("eval score needs --user and --price")
        _print(gw.score_purchase(args.user, args.price, _eval_inputs(args), args.market_price))
    elif a == "show":
        _print(gw.purchase_eval(args.id or ap.error("eval show needs a request id")))
    elif a == "review":
        _print(gw.review_purchase(args.id or ap.error("eval review needs a request id"), _eval_inputs(args)))
    elif a == "agent":
        if not (args.user and args.agent):
            ap.error("eval agent needs --user and --agent")
        out = gw.agent_eval(args.user, args.agent, args.days)
        _print(out if args.json else out["summary"])
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

    p = sub.add_parser("account", help="agent accounts: an allowance, gift cards or prepaid cards per agent")
    p.add_argument("action", choices=["open", "show", "list", "topup", "add-card", "set-balance", "remove-card",
                                      "freeze", "unfreeze", "refill", "offers", "raise", "refund", "settle",
                                      "statement"])
    p.add_argument("id", nargs="?", help="funding id (set-balance, remove-card) or request id (refund)")
    p.add_argument("--user")
    p.add_argument("--agent", help="freeze: omit to freeze every agent")
    p.add_argument("--allowance", type=float, help="open: starting allowance")
    p.add_argument("--max-allowance", type=float, help="open: most the allowance may hold (default: tier limit)")
    p.add_argument("--topup-to", type=float, help="open: refill the allowance to this...")
    p.add_argument("--every-days", type=int, help="open: ...this often (run `account refill` daily)")
    p.add_argument("--amount", type=float, help="topup / refund amount")
    p.add_argument("--kind", choices=["gift_card", "prepaid"], help="add-card")
    p.add_argument("--merchant", help="add-card: the store a gift card is for, e.g. target.com")
    p.add_argument("--balance", type=float, help="add-card / set-balance")
    p.add_argument("--last4", help="add-card: last 4 digits only (never the code)")
    p.add_argument("--label", help="add-card: a name, e.g. 'Target birthday card'")
    p.add_argument("--reason", help="freeze: why")
    p.add_argument("--days", type=int, default=30, help="statement period")
    p.add_argument("--json", action="store_true", help="statement: full entries")
    p = sub.add_parser("shadow", help="read-only shadow mode: an agent learns from one card, spends nothing")
    p.add_argument("action", choices=["start", "cards", "profile", "replay", "pending", "review", "match",
                                      "report", "live"])
    p.add_argument("id", nargs="?", help="review: request id")
    p.add_argument("answer", nargs="?", choices=["yes", "no"], help="review: would you have let it buy this?")
    p.add_argument("--user")
    p.add_argument("--agent")
    p.add_argument("--card", help="the card label used at import, e.g. 'Visa 4417'")
    p.add_argument("--task", default="shopping", help="replay: task type for the tier")
    p.add_argument("--days", type=int, default=90, help="replay: how far back")
    p.add_argument("--allowance", type=float, help="live: open an agent account with this allowance")

    p = sub.add_parser("gifts", help="gift concierge: people, dates, budgets, and a gift ready before each one")
    p.add_argument("action", choices=["add-person", "person", "people", "add-date", "like", "forget-like",
                                      "import-people", "import-calendar", "policy", "upcoming", "plan", "plans",
                                      "show", "brief", "propose", "catalog", "budget", "approve", "skip",
                                      "feedback"])
    p.add_argument("target", nargs="?", help="a person's name, a plan id, or a file")
    p.add_argument("value", nargs="?", help="like: the text; budget: the amount; feedback: loved|liked|returned|disliked")
    p.add_argument("--user")
    p.add_argument("--relationship", help="add-person: partner, spouse, parent, child, sibling, friend, colleague...")
    p.add_argument("--budget", type=float, help="add-person: per-gift budget")
    p.add_argument("--auto-buy-max", type=float,
                   help="add-person: buy without asking up to this (also needs `policy --auto-buy on`)")
    p.add_argument("--address", help="add-person: where gifts go (only shown to the agent once a gift is approved)")
    p.add_argument("--likes", help="add-person: ';'-separated")
    p.add_argument("--dislikes", help="add-person: ';'-separated")
    p.add_argument("--wishes", help="add-person: ';'-separated things they asked for")
    p.add_argument("--size", action="append", help="add-person: e.g. --size shirt=M")
    p.add_argument("--notes")
    p.add_argument("--kind", default="birthday", choices=["birthday", "anniversary", "holiday", "other"],
                   help="add-date")
    p.add_argument("--date", help="add-date: YYYY-MM-DD (the real year enables milestones) or MM-DD")
    p.add_argument("--label", help="add-date: e.g. 'Diwali'")
    p.add_argument("--pref", dest="kind_pref", default="like", choices=["like", "dislike", "wish"], help="like")
    p.add_argument("--source", default="you", choices=["you", "wishlist", "conversation", "social", "feedback"],
                   help="like: where you learned it")
    p.add_argument("--agent", help="policy: the shopping agent; propose: who proposes")
    p.add_argument("--lead-days", type=int, help="policy: start this many days ahead")
    p.add_argument("--annual-budget", type=float, help="policy: total for gifts per year")
    p.add_argument("--auto-buy", choices=["on", "off"], help="policy: allow buying without asking (per person)")
    p.add_argument("--budget-for", action="append", help="policy: e.g. --budget-for friend=50")
    p.add_argument("--days", type=int, default=60, help="upcoming: how far ahead")
    p.add_argument("--options", help="propose: JSON file of options (default: the popular-items catalog)")
    p.add_argument("--option", type=int, help="approve: pick this shortlisted option instead")

    p = sub.add_parser("eval", help="score purchases on your rubric: price, time saved, utility, added value")
    p.add_argument("action", choices=["policy", "score", "show", "review", "agent"])
    p.add_argument("id", nargs="?", help="show / review: request id")
    p.add_argument("--user")
    p.add_argument("--agent")
    p.add_argument("--goal", choices=["balanced", "save_money", "save_time", "wellbeing"], help="policy")
    p.add_argument("--weights", help="policy: price=30,time_saved=25,utility=30,added_value=15 (adds to 100)")
    p.add_argument("--threshold", type=float, help="policy: the score a purchase must reach (1-10)")
    p.add_argument("--hourly-value", type=float, help="policy: what an hour of your time is worth")
    p.add_argument("--gate", choices=["on", "off"], help="policy: send requests below the bar to you")
    p.add_argument("--net-check-over", type=float, help="policy: price from which net value must be >= 0")
    p.add_argument("--price", type=float, help="score")
    p.add_argument("--market-price", type=float, help="score")
    p.add_argument("--lifespan-months", type=float)
    p.add_argument("--uses-per-month", type=float)
    p.add_argument("--total-uses", type=float)
    p.add_argument("--hours-saved", type=float, help="hours saved per year")
    p.add_argument("--solves", type=float, help="1-10: how well it solves the problem")
    p.add_argument("--core", choices=["yes", "no"], help="a core, frequent function?")
    p.add_argument("--benefit", action="append", help="e.g. --benefit stress=2 (stress, health, skill, social, comfort: 0-3)")
    p.add_argument("--resale", type=float, help="resale value after a year")
    p.add_argument("--added-value", type=float, help="other value over a year, in dollars")
    p.add_argument("--score", action="append", help="direct 1-10 score, e.g. --score utility=7")
    p.add_argument("--days", type=int, default=365, help="agent: period")
    p.add_argument("--json", action="store_true", help="agent: full report")

    # --- spending insights ---
    p = sub.add_parser("import", help="manually import a card statement CSV")
    p.add_argument("file")
    p.add_argument("--user", required=True)
    p.add_argument("--card", help="which card this statement is for, e.g. 'Visa 4417' (for shadow mode)")
    p = sub.add_parser("sync", help="automatically import new CSVs dropped into an inbox folder")
    p.add_argument("--user", required=True)
    p.add_argument("--card", help="which card the inbox is for")
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
        elif args.cmd == "account":
            return _account_cmd(args, ap, gw)
        elif args.cmd == "shadow":
            return _shadow_cmd(args, ap, gw)
        elif args.cmd == "gifts":
            return _gifts_cmd(args, ap, gw)
        elif args.cmd == "eval":
            return _eval_cmd(args, ap, gw)
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
