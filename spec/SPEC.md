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

**Approve** otherwise: the backend issues a credential capped at `amount`, locked to the merchant, expiring after the tier's lifetime. It's single-use if the tier says so or if profile `privacy` ≥ 0.8. An agent with an **account** (3i) pays from it instead, and escalates when nothing in it covers the amount; a frozen account is denied (`account_frozen`, unscored). An agent in **shadow mode** (3j) gets the decision as `shadow.would_be` and nothing is paid.

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

**Product IDs** (ASIN, GTIN, retailer SKU) win when both sides have one: equal IDs are the same item whatever the titles say; different IDs are at most similar. Otherwise items are compared by word overlap with plurals folded; a specific description (3+ words) whose words all appear in a longer retail title also counts (`AA batteries 24 pack` = `Amazon Basics AA Batteries, 24 Pack`). ≥ 0.8 is the *same item*, ≥ 0.4 a *similar item*. Cancelled, returned and refunded order lines are ignored. Only prices from the last 365 days (latest 5) are used as a price reference.

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

### 3e. Learned weights and market prices

**Learned weights.** Every scored purchase keeps its four factor scores. A verdict of *accepted* is a positive label; *rejected*, *disputed* or an item mismatch is negative. `earnest confidence fit` fits a logistic regression on those labels (L2-pulled toward the default weights), turns the positive coefficients into weights that sum to 1, and blends them with the defaults by `n / (n + 30)`. Nothing is learned below 10 verdicts or without both outcomes. The fit is kept only if it predicts held-out verdicts (5-fold Brier score) at least as well as the defaults; the report shows both, plus how often purchases in each confidence band were kept.

**Market prices.** Quotes for the same item (product ID or same-item text match) from verified merchants in the last 7 days come from a price connector (`PriceSource`), a user CSV, or agents (`report_price`).
- Another verified store ≥ 15% cheaper → signal `cheaper_elsewhere:<store>:<price>`, price factor capped at 0.6, and **escalated for frugal users** (`price_sensitivity` ≥ 0.7).
- Within 2% of the best *non-agent* quote → `best_market_price`, price factor at least 0.85. Agent-reported quotes can lower confidence but never raise it.
- No personal price history → the market median is the price reference.
- Paying ≥ 20% over the lowest recent price the user paid, with ≥ 14 days to the deadline (or none) → `consider_waiting:<price>`.

### 3f. Reorders, follow-ups, returns and disputes

**Reorder prediction.** Items bought 3+ times (same product ID or same-item text, any source) get an interval (median gap), a regularity score, the usual store, a typical recent price and a next-due date.

**Standing mandates** pre-approve a repeat purchase (`mandate.standing`: `item`, optional `product_id`, `max_unit_price`, `min_interval_days`). Requests against one are denied as violations if the item isn't the same (`standing:item_mismatch`), the unit price is over the limit (`standing:over_price`) or the last purchase of that item was too recent (`standing:too_soon`). The budget is per cycle. A valid standing purchase skips the approval-threshold check (check 9) like a routine repeat; the tier cap still applies.

**Return checker.** Standard return windows for major merchants are data ([earnest/data/return_policies.json](../earnest/data/return_policies.json), with category exceptions and membership extensions). Every confidence block carries `returnable:<days>` (or `open_ended`, or `return_policy_unknown`). A mandate with `requires_returnable` escalates when the policy is unknown, and sets AP2 `requires_refundability`. After purchase, rejected or disputed items that can still be returned are flagged first, then windows closing within 7 days.

**Follow-ups** (`earnest followup`): verdict reminders after 7 days; "did it arrive?" after 10 days without a matching order; price drops of 5%+ at the same store (newer orders or non-agent quotes) while returnable; subscription renewals within 5 days. Purchases that were **verified and kept past the return window** (30 days if unknown) are accepted automatically (profile `auto_accept_kept`, default on).

**Dispute pack** (`earnest dispute <request_id>`): mandate, agent request and reason, decision and confidence, approvals, credential, item verification, verdict, matching orders, AP2 intent mandate and a timeline, as JSON with a SHA-256 and a Markdown summary with a suggested dispute reason.

### 3g. Households

A household groups users as `adult` or `child`:
- Children's requests always escalate to an adult (`household:child_needs_guardian`); a child can't approve.
- A child's `monthly_allowance` and the household `monthly_budget` cap approved spending this calendar month (deny: `household:over_allowance`, `household:over_monthly_budget`).
- Purchases over `second_approval_over` need two different adults (`household:needs_two_adults`; after the first approval the request stays escalated with `awaiting_second_approval`).
- Approval messages go to each adult with links bound to that adult.

### 3h. Portable trust and shared reputation

**Portable trust records.** `earnest trust export` issues a JWT signed with EdDSA (Ed25519) by the user's install, about one agent and task type: tier, purchase-count band, verified-item band, kept rate (one decimal), violations in the last 90 days, valid 30 days. No merchants, items, amounts or user identity beyond a pseudonymous key. On import the signature, issuer and expiry are checked, and the issuer must be one the importing user chose to trust. The head start is `trusted` → up to 20 points (`established`), `established` → 10, nothing with recent violations or a kept rate under 0.8, once per agent, task type and issuer.

**Shared merchant reputation (opt-in).** A contribution holds per-merchant purchase and problem counts (item mismatch, rejected/disputed, never verified) with Laplace noise added locally (ε per count, default 1) under a one-time random id. An aggregator publishes merchants with ≥ 5 contributors. Imported feed entries with ≥ 10 contributors change decisions: an unknown merchant with problem rate ≥ 30% becomes **suspicious** (escalated); an unknown merchant with ≤ 5% over 50+ purchases gets authenticity 0.6; verified merchants only get a signal. The user's own lists still win.

### 3i. Agent accounts

An agent account caps the most an agent can ever spend, whatever happens. It belongs to one `(user, agent)` and holds money from three kinds of **funding source**:

| Source | Pays with | Where | What enforces the cap |
|---|---|---|---|
| `allowance` | a single-use virtual card from the payment backend, as without an account | the approved merchant | earnest holds the amount from the allowance when it issues the card |
| `gift_card` | the store gift card the user loaded into the **agent's own store account**; no card is issued | that store only | the store: the card can't spend more than its balance, or anywhere else |
| `prepaid` | a prepaid Visa / Mastercard the user gave the agent | any store | the card's balance; earnest's rules are the only merchant check |

earnest never holds money. The allowance is a limit on the user's own card program. Gift and prepaid cards stay with the store or issuer; earnest records only the last 4 digits and the balance, never a card number or code. The user tops up and adds cards. Agents can't, and agents still can't *buy* gift cards (a high-risk category, check 11).

**Which source pays.** For an approved request, the first that covers the whole amount: a gift card for this merchant (the smallest balance that covers it), then the allowance, then a prepaid card (smallest first). The response carries a `payment` block (`method`, `funding_id`, `pay_with`, `amount`, `remaining`). If no source covers it, the request escalates with `account:insufficient_balance:<most any one source could pay here>`. Approving it then fails until the user tops up or adds a card, and nothing is recorded.

**Allowance ceiling.** The allowance can hold at most `min(approved, tier ceiling)`. The tier ceiling is the highest per-purchase cap among the agent's tiers across task types (`new` $25, `established` $200, `trusted` $1,000; `new` if it has no score yet). `approved` defaults to the tier ceiling when the account is opened.
- **Decreases are automatic.** When the tier falls, the ceiling falls, and any allowance above it is returned (entry `ceiling`).
- **Increases need the user.** When the tier rises above `approved`, `earnest account offers` lists a raise and `earnest account raise` accepts it. Nothing rises on its own.
- Top-ups beyond the ceiling are capped and say so. Gift and prepaid cards aren't capped, but tier caps and approval thresholds still apply to each purchase.

**Refills.** An account can refill its allowance to `topup_to` every `topup_every_days` (`earnest account refill`, scheduled daily), never above the ceiling.

**Freezing.** A violation, an item mismatch (3d) or a disputed outcome **freezes** the account and returns the allowance to the user (entry `sweep`). `earnest account freeze --user U` freezes every agent at once. A frozen account's requests are denied with `account_frozen`, unscored. Gift and prepaid cards can't be swept, so the freeze lists them for the user to take off the agent's store accounts. Only the user unfreezes.

**Holds, releases and refunds.** An allowance purchase holds the full amount. When the card expires having spent less, `earnest account settle` releases the rest (with backends that report card spend; the mock backend does). `earnest account refund <request_id> [--amount]` credits a return to the source that paid. `earnest account set-balance` corrects a card's balance to what the store shows.

**Statement.** `earnest account statement` gives a one-line summary ("shopper: $37.20 left, 3 purchase(s) in 30 days: all kept") and every entry: `topup`, `load`, `hold`, `spend`, `release`, `refund`, `adjust`, `ceiling`, `sweep`.

### 3j. Shadow mode

Shadow mode lets an agent prove itself before it spends anything, using one of the user's own cards.

1. **Label a card's statements** at import: `earnest import FILE --user U --card "Visa 4417"` (or `sync --card`). Re-importing an unlabelled statement with `--card` labels the rows already stored.
2. **Learn from the card** (`earnest shadow profile`): purchase count, monthly spend, purchase sizes (median, p75, p90), top merchants with typical and largest amounts, categories, and **regulars** (3+ purchases at a steady interval). It also gives **suggestions**: an approval threshold near p75, a monthly budget near the median month, a starting allowance near p90, known merchants and standing restocks. Nothing is applied; the user decides. The card's purchases also count as purchase history for confidence (3b), as all imported statements do.
3. **Replay** (`earnest shadow replay`): for each of the card's purchases in a window, what earnest would have decided if an agent had requested it. Each purchase is judged only on what was known **the day before** it. Output: how many would have gone through automatically, asked, or been blocked, the top reasons, examples, and the automatic share at each tier (`new`, `established`, `trusted`). Nothing is recorded.
4. **Shadow an agent** (`earnest shadow start --agent A --card C`; needs transactions for that card). The agent's spend requests get the full decision, recorded with status `shadow` and `shadow.would_be`. It gets **no credential, no payment from its account, no trust points from the decision, and no notifications**. A would-be violation costs nothing, but blocks graduation for 30 days.
5. **Matches** (`earnest shadow match`): a proposal matches a later card purchase at the same merchant (canonical keys), within 15% or $2 of the amount, on the day of the proposal or up to 14 days after. Each card purchase matches at most one proposal. A match of a proposal earnest wouldn't have denied earns +1 point (`shadow_match`), **capped at 15 points in total** per task type, so shadow mode alone never reaches `established`.
6. **Reviews** (`earnest shadow pending`, `earnest shadow review <id> yes|no`): would you have let it buy this? It agrees when earnest would have approved and you say yes, or would have denied and you say no. Would-be escalations count as `asked`, neither agreeing nor disagreeing.
7. **Readiness** (`earnest shadow report`): ready with no would-be violations in the last 30 days and either 10+ reviews with 90%+ agreement, or 5+ matches. When ready, the report suggests `earnest shadow live --allowance X`, where X is the card's suggested starting allowance capped at the agent's tier ceiling. `live` ends shadow mode and opens (or tops up) an agent account, so the agent's first real spending is capped from day one.

### 3k. Gift concierge

earnest remembers the people you give gifts to and their dates. Before each date, a shopping agent finds options, earnest picks the one that fits best, and it's bought only with your approval, or within an authorization you gave for that person.

**People** (`gifts add-person`, or `gifts import-people` from a CSV with `name, relationship, birthday, anniversary, likes, dislikes, budget, auto_buy_max`) have:
- a relationship
- an optional per-gift budget
- sizes and notes
- `auto_buy_max`: the most the agent may spend on them without asking
- an address, shown to the agent only for an approved gift

**Occasions** are yearly dates: `birthday`, `anniversary`, `holiday` or `other`, written `YYYY-MM-DD` or `MM-DD`. With the real year, earnest knows which birthday or anniversary it is. They come from you, the contacts CSV, or a calendar export (`gifts import-calendar file.ics`). From a calendar, earnest takes events whose title names a birthday or anniversary ("Sam's birthday", "Anniversary - Priya") and ignores other events. A recurring event's first year is taken as the real year; malformed dates are skipped.

**Preferences** are `like`, `dislike` or `wish`, each with a source and weight:

| Source | Weight | Meaning |
|---|---|---|
| `wishlist` | 1.0 | they asked for it |
| `feedback` | 0.9 | how a past gift went |
| `you` | 0.8 | you said so |
| `conversation` | 0.6 | an agent heard it in your chats |
| `social` | 0.5 | a public post or wishlist you pointed to |
| `popular` | 0.3 | popular items, when nothing else is known |

earnest never looks through anyone's accounts: preferences come only from you, your conversations, or links you share. Agents can add `conversation`, `wishlist` and `social` preferences, not `you`.

**Policy** (`gifts policy`):
- the shopping agent
- `lead_days`, how far ahead to plan (default 14)
- an annual gift budget
- budgets by relationship, overriding the defaults (partner/spouse $150, parent $100, child and grandparent $75, sibling $60, friend $40, colleague $25, other $30)
- `auto_buy`, the master switch for buying without asking (default off)

**Plans.** `gifts plan` (run daily) opens a plan for each occasion inside the lead window, at most once per occasion per year. Plans not approved by the date are marked `missed`.

A plan's budget, in order:
1. The person's own budget, or the guidance for their relationship.
2. ×1.5 for a milestone: birthdays 1, 13, 16, 18, 21 and every 10th; the 1st anniversary and every 5th.
3. Capped at the annual budget minus what's already spent or planned this year. Nothing left → `declined`.

Statuses: `open` → `awaiting_approval` → `approved` → `ordered` → `done`. Also `declined`, `skipped` and `missed`.

**The agent's brief** (`get_gift_tasks`, `gifts brief`) contains:
- who the gift is for and the occasion
- the date and days left
- the budget
- wishes, likes and things to avoid, each with its source
- sizes, and gifts given before
- the agent's gift-card balances and account (3i)

There's no address in it.

**Options and ranking** (`propose_gift_options`, `gifts propose`). Up to 20 options, each with `title, merchant, price`, plus optional `category, url, product_id, eta_days, rating, popularity, digital`. Without options, the popular-items catalog (`gifts catalog`) is ranked instead. Each option gets a score from 0 to 1:

| Factor | Weight | |
|---|---|---|
| Fits the person | 0.45 | best overlap with a like or wish, × the source weight (likes count 0.8 as much as wishes) |
| Budget | 0.2 | 1.0 at 60–100% of the budget; less if it looks like an afterthought |
| Arrives in time | 0.1 | |
| Ratings and popularity | 0.15 | |
| Money already set aside | 0.1 | a linked gift card for that store covers it |

These problems rule an option out:
- `over_budget`
- `disliked` (half or more of a dislike's words)
- `arrives_late` (`eta_days` later than one day before the date; digital gifts always arrive in time)
- `already_given` (80%+ word overlap with a past gift)
- `untrusted_merchant`

A suspicious store halves the quality factor. Options scoring under 0.35 aren't suggested. The top five are kept as the shortlist. If nothing usable is left and an option failed only on budget, earnest suggests raising this gift's budget (`gifts budget PLAN AMOUNT`; only you can).

**Putting a gift forward.** The best usable option becomes a spend request (2) under a per-plan gift mandate (`mode: gift`, `task_type: gift_shopping`, `secrecy: true`, deadline = the date, `ship_to: recipient` when there's an address).

The mandate's `approval` is `auto` only when all of these hold:
- policy `auto_buy` is on
- the person has an `auto_buy_max`
- the price is within it

Otherwise it's `always`, so the request reaches you through the usual approval links, notifications and `earnest pending`. Even when authorized, the request still goes through every rule: tier caps, merchant checks and the agent's account (3i). Anything those escalate still comes to you. An option earnest denies is skipped for the next usable one.

**Approving.** `gifts approve PLAN` approves the suggestion. `--option N` picks another shortlisted option: the earlier request is withdrawn (a user denial, which costs the agent nothing) and the new one is approved as your decision. Approving or denying through `earnest approve` / `deny` or an approval link updates the plan too. If the agent's account can't cover the gift, approval fails until you top up; the plan keeps the request and says why.

**Buying.** For an approved plan, the agent gets:
- the item, the store and the price
- the payment (a card or the account source)
- `buy_before`, the card's expiry
- the address, or you when there's none
- `order_by`, the date minus delivery time minus one day
- a gift message, and a reminder to keep it secret

It reports `gift_ordered`.

**Feedback.** `gifts feedback PLAN loved|liked|returned|disliked`:
- records the gift as a `like` or `dislike` with source `feedback`
- reports your verdict on the purchase (accepted / rejected) for the agent's trust, if you hadn't already

Each approved gift goes into the person's gift history, so it isn't given again.

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
| Shadow match (3j): a shadow proposal you then bought yourself | +1, up to 15 in total per task type |

Design notes:
- **Rise slowly, fall fast.** A good purchase earns roughly +5 to +8 with its outcome; one violation costs −10.
- **Efficiency is never scored without an accepted outcome**, so cutting corners to save money doesn't pay.
- **The profile changes what "good" means.** For a frugal user (`price_sensitivity` near 1) savings count and lateness barely matters; for a convenience-first user it's the reverse.
- **Gifts aren't scored on savings**: fitting the budget range is what counts.

## 5. Out of scope for v0.1

Real money, live card issuing, cross-user reputation, LLM-generated mandates, learning the profile from behavior. See [docs/SCOPE.md](../docs/SCOPE.md).
