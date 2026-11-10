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
9. `amount` > effective approval threshold.
10. The merchant is **suspicious** (section 3a): a verified brand on a domain we don't list, a frequently abused TLD, or a phishing-style word in the domain.
11. The category is a **high-risk payment rail** (gift cards, crypto, wire or money transfers, prepaid cards, money orders): hard to reverse, and what scammers ask for.
12. The user **rejected or disputed** the same or a similar item from this merchant before (section 3b).
13. Merchant is new to this user and the tier doesn't allow new merchants (profile `novelty` ≥ 0.8 also allows them). A merchant counts as known if the user has bought there through earnest **or** it appears in their imported card statements. Verified and user-trusted merchants skip this check, but the `no_new_merchants` hard rule still applies to them.

**Approve** otherwise: the backend issues a credential capped at `amount`, locked to the merchant, expiring after the tier's lifetime. It's single-use if the tier says so or if profile `privacy` ≥ 0.8.

Unknown mandates are denied without touching any score.

Every decision carries a `confidence` block (low / medium / high, plus the merchant status, history counts and signals) so the agent and the user can see why. It's informational: `status` and `reasons` are the decision.

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

### 3b. Purchase history

If the request names an `item`, the gateway compares it with what the user bought before at the same merchant:
- **earnest purchases**: merchant, item, amount and the user's verdict.
- **imported card statements** (see [Spending insights](../README.md#spending-insights)): merchant and amount only.

Items are compared by word overlap (plurals folded): ≥ 0.8 is the *same item*, ≥ 0.4 a *similar item*. Confidence starts from the merchant status (verified 0.6, user-trusted 0.7, unknown 0.3, suspicious 0.1, untrusted 0) and then:

| Signal | Effect |
|---|---|
| Bought from this merchant before | +0.15 (+0.05 more at 3+ purchases) |
| Same item bought and kept here before | +0.2 (similar item: +0.1) |
| Same or similar item here was rejected or disputed | −0.3, and the request escalates (check 12) |
| Price more than 1.5× what the user paid for that item before | −0.1 |

Level: high ≥ 0.75, medium ≥ 0.45, low otherwise. Untrusted merchants are always 0. Agents can call `check_merchant` before requesting to see the same block.

### Asking first

If the agent sets `ask_first`, the request is escalated instead of denied or approved. The reward depends on whether asking was needed:
- The request would have been denied or escalated anyway → escalate, **+1** (the agent asked before acting).
- The request would have been auto-approved → escalate, **0** (unnecessary interruptions earn nothing).

This rewards good judgment without making `ask_first` a way to farm points.

When the user approves an escalated request, a credential is issued exactly as in an approval. When they deny it, nothing is scored.

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
