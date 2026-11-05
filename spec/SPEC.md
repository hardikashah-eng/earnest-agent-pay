# earnest spec v0.1

The spec defines five objects and one decision procedure. The schemas in this folder are normative; this document explains how they fit together.

| Object | Schema | Written by |
|---|---|---|
| Profile | [profile.schema.json](profile.schema.json) | the user (later: learned, user-editable) |
| Mandate | [mandate.schema.json](mandate.schema.json) | the user, per task |
| SpendRequest | [spend_request.schema.json](spend_request.schema.json) | the agent |
| Decision | [decision.schema.json](decision.schema.json) | the gateway |
| Outcome | [outcome.schema.json](outcome.schema.json) | the user, never the agent |

Trust tiers are configuration: [trust_tiers.json](trust_tiers.json).

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
2. The merchant is in `blocked_merchants`.
3. `allowed_categories` is set and the category isn't in it.
4. Spent so far on the mandate + `amount` > `budget.max`.
5. A profile `hard_rule` is broken (`no_subscriptions` with `recurring`, `no_international` with `international`, `no_new_merchants` with a merchant the user has never bought from).

**Escalate**
6. Tier is `restricted`.
7. Mandate `approval` is `always` or `shortlist`.
8. `amount` > tier cap.
9. `amount` > effective approval threshold.
10. Merchant is new to this user and the tier doesn't allow new merchants (profile `novelty` ≥ 0.8 also allows them).

**Approve** otherwise: the backend issues a credential capped at `amount`, locked to the merchant, expiring after the tier's lifetime. It's single-use if the tier says so or if profile `privacy` ≥ 0.8.

Unknown mandates are denied without touching any score.

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
