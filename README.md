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

## Merchant trust and purchase history

Every request is checked against **who the merchant is** and **what you've bought before**:

- **Known-good list**: ~40 top e-commerce merchants (Amazon, Walmart, Target, Costco, Best Buy, Apple, …) with their real domains and statement aliases, in [earnest/data/merchant_trust.json](earnest/data/merchant_trust.json). A verified merchant doesn't trigger the "new merchant" approval.
- **Untrusted**: lookalikes of those merchants (`amaz0n.com`, `arnazon.com`, `amazon-giftcards.shop`, `targett.com`) and anything on your blocklist are **denied**, and count as a violation. Suspicious domains (a brand on an unlisted domain, abused TLDs, phishing words like `login` / `refund`) and hard-to-reverse payment rails (gift cards, crypto, wire transfers) are **escalated** to you.
- **Your purchase history**: if the agent names the `item`, the gateway looks for the same or a similar item from the same merchant in past earnest purchases and your imported statements. A repeat purchase you kept raises confidence; one you rejected makes the gateway ask you first; a big price jump is flagged.

Each decision includes a `confidence` block (low / medium / high and why). Your own lists always win:

```bash
python -m earnest merchant check amazon.com --user user_demo --item "AA batteries 24 pack"
python -m earnest merchant distrust sketchy-deals.shop --user user_demo --reason "never shipped"
python -m earnest merchant trust ikea.fr --user user_demo        # fix a false positive
python -m earnest merchant import-blocklist fraud-feed.txt --user user_demo   # one domain per line
python -m earnest merchant list --user user_demo
```

Rules and point values: [spec/SPEC.md](spec/SPEC.md) sections 3a and 3b.

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
