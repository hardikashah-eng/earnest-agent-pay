# earnest spec v0.1

The spec defines five objects and one decision procedure. The schemas in this folder are normative; this document explains how they fit together.

| Object | Schema | Written by |
|---|---|---|
| Profile | [profile.schema.json](profile.schema.json) | the user (later: learned, user-editable) |
| Mandate | [mandate.schema.json](mandate.schema.json) | the user, per task |
| SpendRequest | [spend_request.schema.json](spend_request.schema.json) | the agent |
| Decision | [decision.schema.json](decision.schema.json) | the gateway |
| Outcome | [outcome.schema.json](outcome.schema.json) | the user, never the agent |

Trust tiers are configuration: [trust_tiers.json](trust_tiers.json). Default merchant trust lists are data: [earnest/data/merchant_trust.json](../earnest/data/merchant_trust.json).

## 1. Trust is keyed per (user, agent, task type)

An agent trusted with software purchases has not earned trust for gift shopping. Each `(user_id, agent_id, task_type)` has its own score. The mandate's `task_type` picks which score applies.

Scores **decay toward zero** with a 90-day half-life, so old behavior counts less and an inactive agent slowly drifts back to "new". Negative scores decay too, so a restricted agent can recover.

## 2. Tiers

The current score picks the highest tier whose `min_score` it meets. A score below 0 is `restricted`.

| Tier | min score | Cap | Credential lifetime | Approval over | Single use | New merchants OK |
|---|---|---|---|---|---|---|
| trusted | 60 | $1,000 | 168 h | $500 | no | yes |
| established | 20 | $200 | 24 h | $150 | yes | no |
| new | 0 | $25 | 1 h | $10 | yes | no |
| restricted | < 0 | $0 | 1 h | $0 | yes | no |

The profile's `autonomy` scales the approval threshold: `effective = approval_over × (0.5 + autonomy)`. At the default autonomy of 0.5 the threshold is unchanged.

## 3. Decision procedure

Checks run in this order. The first group **denies** the request and counts as a **violation**. The second group **escalates** to the user and carries no penalty.

**Deny (violation)**
1. The mandate's `deadline` has passed.
2. The merchant is in `blocked_merchants`, or is **untrusted** (section 3a): on the user's untrusted list, on the default blocklist, or a lookalike of a verified merchant.
3. `allowed_categories` is set and the category isn't in it.
4. Spent so far on the mandate + `amount` > `budget.max`.
5. A profile `hard_rule` is broken (`no_subscriptions` with `recurring`, `no_international` with `international`, `no_new_merchants` with a merchant the user has never bought from).

**Escalate**
6. Tier is `restricted`.
7. Mandate `approval` is `always` or `shortlist`.
8. `amount` > tier cap.
9. `amount` > effective approval threshold, unless it's a routine repeat purchase (section 3c).
10. The merchant is **suspicious** (section 3a): a verified brand on a domain we don't list, a frequently abused TLD, or a phishing-style word in the domain.
11. The category is a **high-risk payment rail** (gift cards, crypto, wire or money transfers, prepaid cards, money orders): hard to reverse, and what scammers ask for.
12. The user **rejected or disputed** the same or a similar item from this merchant before (section 3b).
13. Merchant is new to this user and the tier doesn't allow new merchants (profile `novelty` ≥ 0.8 also allows them). A merchant counts as known if the user has bought there through earnest **or** it appears in their imported card statements. Verified and user-trusted merchants skip this check, but the `no_new_merchants` hard rule still applies to them.

**Approve** otherwise: the backend issues a credential capped at `amount`, locked to the merchant, expiring after the tier's lifetime. It's single-use if the tier says so or if profile `privacy` ≥ 0.8.

9b. Purchase confidence is below the profile's `min_confidence` (section 3c).

Unknown mandates are denied without touching any score.

Every decision carries a `confidence` block (score, level, the four factors with reasons, merchant status, history and signals) so the agent and the user can see why.

### 3a. Merchant trust

Each merchant is classified before any history is considered. Merchants are matched however they're written (`amazon.com`, `https://www.amazon.com/dp/…`, `Amazon`, `AMZN Mktp` from a statement), and subdomains of a listed domain count as that merchant. First match wins:

| Status | From | Effect |
|---|---|---|
| `untrusted` | the user's own untrusted list | deny (violation) |
| `user_trusted` | the user's own trusted list | like verified |
| `verified` | the default known-good list (top e-commerce merchants with their domains and aliases) | skips the new-merchant escalation |
| `untrusted` | the default blocklist (`untrusted.entities`), or a **lookalike** of a verified merchant: a typo of a long brand name (`amazom.com`), a homoglyph spelling (`amaz0n`, `arnazon`, `rnicrosoft`), or the brand as a token in someone else's domain (`amazon-giftcards.shop`, `amazon.evil.com`) | deny (violation) |
| `suspicious` | brand on an unlisted domain (`ikea.fr`), abused TLD (`.xyz`, `.top`, …), or a phishing word (`login`, `verify`, `refund`, `giftcard`, …) | escalate |
| `unknown` | anything else | normal rules |

The user's lists always win over the defaults, so a false positive is fixed with `earnest merchant trust <merchant>`. The default blocklist ships with pattern rules and no named third parties, since scam domains change daily; load a current feed with `earnest merchant import-blocklist <file>`.

### 3b. Purchase history, memberships and interests

The gateway gathers what the user has bought and wants, from:

| Source | Has item? | Import |
|---|---|---|
| earnest purchases (with the user's verdict) | yes, if the agent sent `item` | automatic |
| Retailer order history: Amazon, Costco, Walmart, Target, … | yes, with unit price | `earnest orders import` (Amazon "Request your data" export, or a generic date/item/price CSV with `--retailer`), or an `OrderSource` connector |
| Card statements | no (merchant and amount) | `earnest import` / `earnest sync` |
| Memberships (Prime, Costco, Walmart+, Target Circle, a local co-op) | — | `earnest membership add` |
| Interests (wishlist, cart, "I want a standing desk under $350") | yes, optional max price and merchant | `earnest interest add` / `earnest interest import` |

Items are compared by word overlap with plurals folded; a specific description (3+ words) whose words all appear in a longer retail title also counts (`AA batteries 24 pack` = `Amazon Basics AA Batteries, 24 Pack`). ≥ 0.8 is the *same item*, ≥ 0.4 a *similar item*. Cancelled, returned and refunded order lines are ignored. Only prices from the last 365 days (latest 5) are used as a price reference.

A merchant counts as **known** (check 13 and the `no_new_merchants` rule) if it appears in any purchase source or the user is a member there. A same or similar item at this merchant that the user **rejected or disputed** escalates (check 12).

### 3c. Purchase confidence

Every request gets a 0–1 confidence from four weighted factors, each 0–1 with a plain-language reason:

| Factor | Weight | Scoring |
|---|---|---|
| **Authenticity** | 0.35 | verified / user-trusted 0.9, and 1.0 if the user is a member; unknown merchant 0.4, 0.7 if the user has bought there, 0.9 if a member; suspicious 0.15; untrusted 0 |
| **Price** | 0.25 | Same item in the last year: 1.0 up to 10% over the highest recent unit price, then falling (floor 0.1). Otherwise similar items (up to 0.85, 25% tolerance), the interest's max price (0.9 / 0.3), the mandate's `benchmark_price` (up to 0.8), or 0.5 with no reference |
| **Familiarity** | 0.25 | same item bought here 1.0; similar item here or same item elsewhere 0.75; shown interest 0.8; item matches the mandate intent 0.55; shops here but new item 0.5; nothing 0.3; rejected here before 0 |
| **Budget** | 0.15 | Share of the mandate's remaining budget: ≤ 50% → 1.0, 100% → 0.6; inside a gift's `[min, max]` range → 1.0; over → 0 (and denied by check 4) |

`score = Σ weight × factor`, forced to 0 for untrusted merchants. Level: high ≥ 0.75, medium ≥ 0.5, low below. The gateway uses it on every request:
- **Low confidence**: below the profile's `min_confidence` (default 0.35) → escalate, reason `low_confidence:<score>`.
- **Routine repeat purchase**: the same item bought here before, price in range, authenticity ≥ 0.7, not suspicious and score ≥ 0.8 → the approval-threshold escalation (check 9) is skipped. The **tier cap still applies**, as do every deny check and every other escalation.

Agents call `check_merchant` (optionally with `mandate_id` and `quantity`) to see the same score before requesting, and can compare merchants and prices with it.

### 3d. Verifying the item claim

The confidence score trusts the `item` the agent names, so earnest checks it:

1. **At checkout.** The credential carries an `item_lock`. When the merchant sends cart line items (ACP / UCP checkout data, an AP2 cart mandate, network line-item data), the charge is declined unless the claimed item is in the cart and other goods are at most 10% of it. Tax, shipping and fee lines are ignored.
2. **After the fact.** `earnest reconcile` (run automatically after `earnest orders import`) matches each approved request that named an item with orders at the same merchant from 1 day before to 7 days after. A matching item → `verified`. A different item at about the same price (±15%) → `mismatch`. Nothing after 14 days → `unverified` (no penalty).

| Event | Points |
|---|---|
| Item verified (cart or order) | +1 |
| Item mismatch | −10 (a violation) |

A mismatch also **suspends the agent's routine-repeat pass for 90 days** for that task type, and the false claim is dropped from the purchase history, so it can't build familiarity.

## 4. Scoring

| Event | Points |
|---|---|
| Purchase approved (directly or after escalation) | +2 |
| Asked first when it was needed | +1 |
| Violation | −10 |
| Outcome accepted | +3 |
| Outcome rejected | −5 |
| Outcome disputed | −15 |
| Efficiency (accepted only, non-gift, with `benchmark_price`) | `clamp(4 × saving × price_sensitivity, −3, +3)` where `saving = (benchmark − amount) / benchmark` |
| Budget fit (accepted gift with `budget.min`, amount in [min, max]) | +1 |
| Timeliness (accepted) | on time: `+1 × (1 − price_sensitivity)`; late: `−2 × (1 − price_sensitivity)` |

Design notes:
- **Rise slowly, fall fast.** A good purchase earns roughly +5 to +8 with its outcome; one violation costs −10.
- **Efficiency is never scored without an accepted outcome**, so cutting corners to save money doesn't pay.
- **The profile changes what "good" means.** For a frugal user (`price_sensitivity` near 1) savings count and lateness barely matters; for a convenience-first user it's the reverse.
- **Gifts aren't scored on savings**: fitting the budget range is what counts.

## 5. Out of scope for v0.1

Real money, live card issuing, cross-user reputation, LLM-generated mandates, learning the profile from behavior. See [docs/SCOPE.md](../docs/SCOPE.md).
