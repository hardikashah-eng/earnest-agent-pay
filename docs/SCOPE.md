# Initial scope

## Goal for v0.1

A runnable demo, using no real money, that shows two agents on the same tasks: one stays faithful and its limits grow, the other overspends and gets throttled. Quickstart in under 5 minutes, no accounts needed.

## In scope (v0.1) — done

All items below shipped in v0.1. One change from the plan: outcomes are reported by the user through the CLI, not through an MCP tool, so an agent can never grade its own work. The MCP server exposes `list_mandates`, `get_limits` and `request_spend`.

**Spec**
- JSON Schemas for `profile`, `mandate`, `trust_tier`, `spend_request`, `outcome`.
- `SPEC.md`: field meanings, how profile + mandate + trust combine into a decision.

**Gateway**
- SQLite ledger: agents, profiles, mandates, transactions, outcomes.
- Rules engine: each spend request is approved, denied or escalated to the user.
- Trust score per (user, agent, task type); tiers map the score to cap, credential lifetime and approval threshold.
- Scoring: +faithfulness for in-mandate purchases and correct escalation, a sharp penalty for violations, efficiency only on accepted outcomes, decay over time.
- MCP server exposing `request_spend`, `get_limits`, `report_outcome`.

**Backends**
- `mock`: simulated cards and authorizations (default).

**Demo**
- Two-agent simulation script plus a limits-over-time chart.

## Spending insights (v0.2) — done

- Import card statements manually (`earnest import`) or automatically (`earnest sync` on an inbox folder, `--watch`, or a `TransactionSource` connector), with deduplication across overlapping exports.
- Merchant cleanup and categories.
- Detectors: card fees, subscription price creep, overlapping streaming subscriptions, cheaper store you already use, food delivery habits, category spikes vs. your usual.
- Monthly top 5, ranked by saving × confidence; dismissed insights stay hidden.

Next for insights: receipt / order import for item-level advice ("buy X at Y instead"), a real bank-aggregator connector, learning the profile (`price_sensitivity`, store loyalty) from spending, and using real spend as fair-price benchmarks for agent scoring.

## Merchant trust (v0.2) — done

- Default known-good list of top e-commerce merchants; lookalike, suspicious-domain and high-risk-category detection; user trusted / untrusted lists and blocklist import.
- Purchase history (earnest purchases + imported statements) as a confidence signal, with same / similar item matching.
- `check_merchant` MCP tool; `confidence` on every decision.
- Retailer order-history import (Amazon export, generic CSV for Costco / Walmart / Target), memberships and interests (wishlist / cart).
- Weighted purchase confidence (authenticity, price vs. recent purchases, history / interest, budget) used on every request: low confidence escalates, routine repeats skip the approval threshold.

Next: a maintained blocklist feed connector, `OrderSource` connectors (retailer APIs, email receipts), and per-category verified lists outside the US.

## Real money rails and approvals (v0.3) — done

- `stripe_issuing` backend (test mode only; live keys refused): capped virtual cards, real-time authorization webhook enforcing merchant, expiry, cap, single use and item lock.
- AP2 interop: Intent mandate export with an earnest attestation; Cart mandate checks before paying.
- Approvals in Slack, Lark / Feishu, ntfy push or any webhook, with signed expiring one-tap links and a confirm page (`earnest serve`).

## Data (v0.3) — done

- Email receipts from mbox (Google Takeout), Maildir, .eml or IMAP, read only from verified merchant domains with DKIM pass; refunds/returns skipped; order + shipping emails deduplicated.
- Product IDs (ASIN, GTIN with check digit, retailer SKU) from exports, receipt links and agent requests; used for same-item matching and cart checks.

## Smarter scoring (v0.3) — done

- Per-user confidence weights learned from verdicts, shrunk to defaults, kept only when they beat defaults on held-out data, with a calibration report.
- Market price quotes (connector / CSV / agent), cheaper-elsewhere and wait suggestions. Next: a price-tracker connector (e.g. Keepa for Amazon).

## User features (v0.3) — done

- Reorder prediction and standing mandates; follow-ups (verdicts, delivery, price drops, renewals) with auto-accept of kept purchases; return checker (policy data, pre-purchase signal, deadlines); households (allowances, monthly budget, two-adult approvals); dispute evidence packs.

## Network (v0.3) — done

- Portable Ed25519-signed agent trust records with capped, issuer-gated head starts.
- Opt-in shared merchant reputation with local differential privacy and k-anonymous feeds.
- Next: bind records to an agent identity key (challenge-response, or an agent-identity protocol such as Visa TAP), and a hosted aggregator.

## Next (v0.2+)

- Plain language to mandate: an LLM turns "find a surprise gift for my brother, into hiking, under $80" into a structured mandate.
- Learn profile from approvals and rejections (revealed preference), with the user able to see and edit it.
- Modes: personal / work / gift.
- Outcome feedback from the approval page.
- Migrate the MCP server to the mcp 2.x SDK (currently pinned to 1.x).
- Rate-limit `ask_first` so an agent can't flood the user with questions.

## Out of scope (for now)

- Real money, production card issuing, KYC.
- A hosted, cross-bank reputation service (v0.3 ships the file-based, opt-in version).
- Crypto rails (x402, smart-wallet spend permissions); possible future backends.

## Design principles

- **Agents never hold credentials**; they get scoped, expiring ones per approved request.
- **Explicit instructions win**: the profile never overrides what a mandate says.
- **Profile stays with the user**: merchants only see what's needed (amount, shipping, deadline).
- **Rise slowly, fall fast**: trust is earned gradually and lost quickly.
- **Score outcomes, not just spend**: reward value delivered, not underspending.
- **Open spec, swappable backends.**

## Open questions

- How to benchmark "fair price" per task type without a data source?
- Should trust reset fully on a model change, or decay partly?
- How to score open-ended tasks (gifts) where success is known only later?

## Agent accounts and shadow mode (v0.4) — done

- Agent accounts: a balance per (user, agent) from an allowance (single-use virtual cards), store gift cards (store-locked; last 4 digits only) or prepaid cards. Allowance ceiling follows the trust tier: decreases automatic, raises opt-in. Refills, holds and releases, refunds, statements. Violations, item mismatches and disputes freeze the account and return the allowance; one command freezes every agent.
- Shadow mode: card-labelled statement imports; a read-only card profile with suggested limits; point-in-time replay of a card's purchases at every tier; agents in shadow get real decisions but no money or points; proposals matched to later card purchases (small capped trust) and reviewed by the user; a readiness report that suggests going live with an agent account.

Next: gift-card balance checks through retailer APIs, a bank-aggregator `TransactionSource` so shadow matching runs without exports, and per-category allowances.

## Gift concierge (v0.5) — done

- People, yearly occasions (manual, contacts CSV, calendar .ics), preferences with sources (wishlist, feedback, you, conversation, social, popular), gift policy (agent, lead time, yearly budget, relationship budgets, auto-buy switch).
- Daily planning with budgets (milestones, yearly cap); option ranking (fit, budget, arrival, quality, linked gift cards; dislikes, repeats, late, untrusted ruled out); per-plan gift mandates through the usual spend/approval path; authorized auto-buy per person; addresses only after approval; feedback into preferences and trust.

Next: price-drop and stock watching for shortlisted gifts, shared household gift lists, and reminders for occasions with no plan.

