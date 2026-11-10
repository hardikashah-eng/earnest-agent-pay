# earnest

**Agents earn spending limits by behaving well.**

`earnest` is an open spec and reference gateway for giving AI agents money safely. Agents never hold a card.
They ask the gateway to spend; the gateway decides based on three things:

1. **Who you are**: a *profile* of how you like money spent (frugal or convenience-first, hands-off or cautious, privacy-conscious, …).
2. **What this task is**: a *mandate* ("surprise birthday gift for Sam, $50–80, ship to me, by the 14th, show me 3 options first").
3. **What the agent has earned**: a *trust score* per agent and task type, built from staying inside mandates and from outcomes you accept.

Approved requests get a scoped, expiring payment credential (a capped, single-use virtual card in test mode). The outcome feeds back into the trust score: limits **rise slowly** with good behavior and **fall sharply** on violations, like a credit line.

> **Status: v0.1.** Spec, gateway, mock payment backend, MCP server and demo. Nothing here handles real money: mock backend only.

![Trust score over time for a faithful and a sloppy agent](demo/limits.svg)

*Same user, same 40 tasks. The faithful agent reaches `trusted` and stops needing approval; the sloppy one ends up `restricted`.*

## Quickstart

Needs Python 3.10+. The core has no dependencies.

```bash
git clone https://github.com/hardikashah-eng/earnest-agent-pay.git
cd earnest-agent-pay
python -m earnest demo                # two-agent simulation, writes demo/limits.svg
python -m unittest discover -s tests  # run the tests
```

```
agent    final tier    score  accepted  violations  asked you  vs fair price
steady   trusted       191.4        40           0          6          -8.3%
sloppy   restricted    -53.8        22          15         25         +18.4%
```

### Try the flow yourself

```bash
python -m earnest profile examples/profile-frugal.json
python -m earnest mandate examples/mandate-software.json
python -m earnest request examples/request-subscription.json   # denied: you said no subscriptions
python -m earnest request examples/request-ok.json             # escalated: new agent, new merchant
python -m earnest pending --user user_demo
python -m earnest approve <request_id>                         # issues a $45 single-use card
python -m earnest outcome <request_id> accepted                # you rate it; the score updates
python -m earnest limits --user user_demo --agent agent_shopper --task software
```

### Connect an agent over MCP

```bash
pip install -e ".[mcp]"
```

Then add the server to your MCP client (e.g. Claude Desktop / Claude Code config):

```json
{
  "mcpServers": {
    "earnest": {
      "command": "python",
      "args": ["-m", "earnest.mcp_server"],
      "cwd": "/path/to/earnest-agent-pay",
      "env": { "EARNEST_DB": "earnest.db", "EARNEST_USER_ID": "user_demo", "EARNEST_AGENT_ID": "agent_shopper" }
    }
  }
}
```

The agent gets four tools: `list_mandates`, `get_limits`, `check_merchant` and `request_spend`. Its identity comes from the server's environment, not tool arguments, and only you report outcomes (via the CLI), so the agent can't grade itself.

## Merchant trust and purchase confidence

Every request gets a **confidence score** (0–1) from four things, each with a plain-language reason:

| Factor | Weight | High when |
|---|---|---|
| Merchant authenticity | 35% | it's a verified store, you're a member there (Prime, Costco, Walmart+, Target Circle), or you've bought there before |
| Price | 25% | it's in the range you paid for the same item (or similar items) in the last year |
| Purchase history / interest | 25% | you bought this or something like it before, or it's on your wishlist / interest list |
| Budget | 15% | the mandate's remaining budget easily covers it |

Low confidence (below your profile's `min_confidence`, default 0.35) goes to you for approval. A **routine repeat** (same item, same store, price in range, high confidence) skips the approval threshold but never the tier cap.

**Merchant lists.** ~40 top e-commerce merchants with their real domains and statement aliases are verified ([earnest/data/merchant_trust.json](earnest/data/merchant_trust.json)). Lookalikes (`amaz0n.com`, `arnazon.com`, `amazon-giftcards.shop`, `targett.com`) and your blocklist are **denied** and count as a violation. Suspicious domains and hard-to-reverse payment rails (gift cards, crypto, wire transfers) are escalated. Your own lists always win.

**What you've bought and want.** Order history carries the items that card bills don't:

```bash
# Amazon: Account > Request your data > Your Orders (Retail.OrderHistory.*.csv)
python -m earnest orders import examples/orders/amazon-order-history.csv --user user_demo
# Costco / Walmart / Target receipts: any CSV with date, item, price
python -m earnest orders import examples/orders/costco-receipts.csv --retailer costco.com --user user_demo
python -m earnest membership add costco.com --program executive --user user_demo
python -m earnest interest import examples/orders/wishlist.csv --user user_demo
python -m earnest interest add "standing desk" --max-price 350 --user user_demo

python -m earnest merchant check costco.com --item "Kirkland paper towels 12 rolls" --amount 24.99 --user user_demo
python -m earnest merchant distrust sketchy-deals.shop --user user_demo --reason "never shipped"
python -m earnest merchant import-blocklist fraud-feed.txt --user user_demo   # one domain per line
```

**Agents can't fake the item.** Cards are locked to the claimed item: a cart without it is declined. Orders are reconciled after the fact (`earnest reconcile`); a mismatch costs −10 and suspends the routine pass for 90 days.

Card statements (`earnest import`, below) also count: a store on your card bill is a store you know. Rules and numbers: [spec/SPEC.md](spec/SPEC.md) sections 3a–3c.

## Real cards (Stripe test mode), AP2 and approvals on your phone

**Stripe Issuing (test mode).** Approved requests become real virtual cards with an all-time limit equal to the approved amount; merchant lock, expiry, single use and the item lock are enforced in Stripe's real-time authorization webhook. Live keys are refused.

```bash
export EARNEST_BACKEND=stripe STRIPE_API_KEY=sk_test_... STRIPE_CARDHOLDER_ID=ich_... STRIPE_WEBHOOK_SECRET=whsec_...
python -m earnest serve --port 8787     # point Stripe's Issuing authorization webhook at /stripe/webhook
```

**AP2.** `earnest ap2 intent <request_id>` exports an approved request as an AP2 Intent mandate with an earnest attestation; `earnest ap2 check-cart <request_id> cart.json` checks a merchant's Cart mandate (merchant, total, expiry, claimed item) before paying. Agents get the same as MCP tools.

**Approvals in Slack, Lark or on your phone.** Escalations are sent with the four confidence factors and one-tap Approve / Deny links. Opening a link shows a confirm page; only the button acts, so chat link previews can't approve anything.

```bash
export EARNEST_NOTIFY="slack:https://hooks.slack.com/...,ntfy:https://ntfy.sh/my-earnest"   # also lark:, webhook:
export EARNEST_APPROVAL_URL=https://earnest.my-tunnel.example
python -m earnest serve
```

## Spending insights

Import your card statements and get the **top 5 things worth knowing each month**: fees you could avoid, prices that crept up, overlapping subscriptions, cheaper stores you already use, delivery habits and unusual spikes. Everything stays in your local SQLite file.

**Manual import:** download a CSV from your bank and import it.

```bash
python -m earnest import examples/statements/statement-jul-sep.csv --user user_demo
```

**Automated import:** drop exports into an inbox folder; `sync` picks up only new files. Run it on a schedule (cron, Windows Task Scheduler) or keep it running with `--watch`.

```bash
python -m earnest sync --user user_demo --inbox statements/inbox
python -m earnest sync --user user_demo --inbox statements/inbox --watch --every 3600
```

Bank-API connectors (e.g. an aggregator) plug in through the `TransactionSource` interface in [earnest/insights/sources.py](earnest/insights/sources.py).

Re-importing a file, or importing overlapping exports (Jul–Sep then Sep–Oct), never double counts. Common CSV shapes are supported: one signed `Amount` column (either sign convention) or separate `Debit`/`Credit` columns. Card payments and refunds aren't counted as spending.

```bash
python -m earnest insights --user user_demo            # latest month; --month 2026-10, --json
python -m earnest dismiss-insight fees:2026-10 --user user_demo   # "I know about this"
```

Sample output (made-up data from [examples/make_sample_statements.py](examples/make_sample_statements.py)):

```
Top 5 insights for October 2026
(114 transactions considered, $2,018.61 spent this month)

1. $643.75 at Whole Foods this month
   You already shop at Aldi; a typical basket there costs about 40% less (rough estimate, not item prices).
   -> Move staples (pantry, dairy, produce basics) to Aldi; keep specialty items at Whole Foods.
   ~$129/month possible | confidence 55% | dismiss: switch:Whole Foods->Aldi

2. Shopping spending is 4.9x your usual
3. $43.12 in card fees this month
4. 9 food delivery orders this month ($357.83)
5. 5 streaming subscriptions: $64.95 a month
```

Insights are ranked by estimated monthly saving × confidence. **Limits:** statements show the store and total, not the items, so store-switch savings use a rough price index ([detectors.py](earnest/insights/detectors.py)), not item prices. Item-level advice ("buy X at Y instead") needs receipt data, which is planned.

---

## Why

Today, agent spending is either too loose (a real card with a hard cap) or too tight (approve every purchase by hand). Neither scales.
People and credit systems handle trust the same way: start small, extend more as it's earned. `earnest` applies that to agents, and measures "good behavior" against *your* preferences, not generic rules.

## How it works

```
Agent ──(spend request + reason)──▶ earnest gateway ──▶ payment backend (mock / Stripe Issuing test mode)
                                      │                   issues a capped, expiring credential
                                      ├─ check against the mandate
                                      ├─ apply profile preferences
                                      ├─ apply limits from the agent's trust tier
                                      └─ record outcome ──▶ trust score updates future limits
```

The gateway is exposed as an **MCP server**, so Claude or any MCP-capable agent can call tools like
`request_spend(amount, merchant, category, reason)`.

Full rules and point values: [spec/SPEC.md](spec/SPEC.md).

### Trust tiers

| Tier | Per-transaction cap | Credential lifetime | Needs your approval |
|---|---|---|---|
| Restricted (score < 0) | $0 | — | Everything |
| New (0+) | $25 | 1 hour, single use | Over $10, or a new merchant |
| Established (20+) | $200 | 24 hours, single use | Over $150, or a new merchant |
| Trusted (60+) | $1,000 | 7 days | Over $500 |

Anything outside the mandate (over budget, wrong category, blocked merchant, past deadline, against your hard rules) is denied at every tier and costs the agent 10 points. Your `autonomy` setting scales the approval thresholds.

### What earns trust

- **Faithfulness**: purchases inside the mandate; no disputes or refunds; asking for approval *before* acting when unsure.
- **Efficiency**: scored *only for accepted outcomes*, as value against a benchmark. Underspending isn't rewarded by itself, since that invites cutting corners.
- **Profile-aware**: a frugal user's agent scores well for waiting for a deal; a convenience-first user's agent doesn't.

## Repository layout

```
spec/              JSON Schemas (profile, mandate, spend request, decision, outcome), trust tiers, SPEC.md
earnest/           the gateway
  models.py        spec objects + validation
  rules.py         decision procedure (approve / deny / escalate)
  scoring.py       trust points and decay
  ledger.py        SQLite ledger
  gateway.py       ties it together
  backends/mock.py simulated scoped, expiring cards
  mcp_server.py    agent-facing MCP tools
  cli.py           user-facing commands
  simulate.py      two-agent demo
  insights/        statement import (manual + automated), detectors, monthly report
examples/          sample profile, mandates, requests and fake card statements
demo/limits.svg    demo chart
tests/             unit tests
docs/SCOPE.md      scope and roadmap
```

## Related work

`earnest` aims to sit *on top of* existing rails, not replace them: Visa Intelligent Commerce and Mastercard Agent Pay (agentic tokens), Google AP2 (intent and cart mandates), OpenAI/Stripe Agentic Commerce Protocol, Coinbase x402, and ERC-8004 (agent identity and reputation).

## License

[Apache-2.0](LICENSE)
