# Threat model

Scope: Tessera as built for the hackathon, running against a **mock** Airwallex adapter on synthetic data. It says what is
protected, from whom, by what, and what is still exposed. It does not claim the system is safe for real funds; the last
section lists why not. Evidence for the controls is in [redteam-report.md](redteam-report.md) (156 attacks and 44 benign
cases, 0 harmful actions executed on the mock), [policy-spec.md](policy-spec.md) and [architecture.md](architecture.md).

## What a failure looks like

The worst outcome is **money moving that the business did not intend**: to the wrong payee, in the wrong amount, twice, or
past a limit. Next worst: a loss nobody can reconstruct (no audit trail), and an agent that gained authority it was not
given. Everything below is ranked against those.

## Assets

| Asset | Why it matters | Where it lives |
| --- | --- | --- |
| Funds in multi-currency balances | The thing being protected | Airwallex (mock in this build) |
| The beneficiary allowlist | Decides who may be paid | Database; the planner names a beneficiary by ID only |
| The policy ruleset and its hash | Defines what is allowed | YAML in the repository, hash stored with every decision |
| The autonomy level and the kill switch | Decide how much runs without a person | Autonomy: one database row. Kill switch: from the world provider (see residual risk) |
| The audit log | The only way to reconstruct and dispute a decision | `audit_events`, append-only, hash-chained |
| API key, webhook secret, Airwallex and LLM credentials | Give access to all of the above | Environment variables; never in code |
| Business data (invoices, customers, balances) | Confidential, and also the planner's input | PostgreSQL |

## Actors

| Actor | Capability assumed | Goal |
| --- | --- | --- |
| **Third party who writes text the system reads** (a customer's invoice note, a memo, an email) | Controls free text that reaches the planner | Make the agent pay them, or someone, or change a payee |
| **Compromised or mistaken planner** (a model that follows injected text, hallucinates, or is swapped) | Can emit any schema-valid proposal | Whatever the injected text says; or none, by plain error |
| **Network attacker** | Can send requests to the webhook endpoint and the API | Forge a payment status, replay an approval or an event |
| **Malicious or careless insider with the API key** | Can call the API | Approve something they should not, raise the autonomy level, bypass the kill switch, touch another business |
| **A bug in Tessera's own plumbing** (a stale or empty data provider, a race, a crash mid-execution) | Not an attacker, but the red team shows it behaves like one | Double payment, a cap that never binds |
| **Operator with database superuser rights** | Full access | Out of scope as an adversary; see residual risk |

## Trust boundaries

1. **Outside text to planner.** Everything a third party wrote is data. It is normalised, length-capped and placed in a
   delimited block; it is never concatenated into instructions.
2. **Planner to policy.** The planner has no authority. Its output is a strict schema with IDs only, and extra fields are rejected.
3. **Policy to worker.** Only the worker may import the Airwallex package (import-linter contract). It re-runs policy before sending.
4. **Worker to vendor.** Every call carries an idempotency key stored before the call.
5. **Vendor to worker.** Webhooks are signature-verified, replay-rejected and de-duplicated by event ID.
6. **Everything to audit.** Each step appends a record; nothing updates or deletes one.

## Controls, by threat

| # | Threat | Controls | Evidence |
| --- | --- | --- | --- |
| T1 | **Prompt injection** makes the planner propose a payment or change of payee | Untrusted text delimited and sanitised; detector escalates instruction-like text and look-alike IDs; proposals reference IDs only, so the model cannot supply an account number; `beneficiary_allowlist`, caps and the other rules apply whatever the model was told | 47 injection cases (direct, indirect, multilingual, encoded, split): 0 executed. A planner that always does what the text says is also stopped by policy |
| T2 | **Payee substitution**: an unapproved or look-alike beneficiary | Allowlist by exact ID; a new beneficiary waits a 24-hour cooldown; look-alike IDs (letter O for zero, case, trailing space, zero-width space) do not match | 13 cases: 0 executed (76.9% blocked, 23.1% escalated) |
| T3 | **Amount manipulation**: floats, exponents, extra decimals, negatives | Amounts are strings or integers with at most two decimals; `Money` is Decimal-backed; anything else is a BLOCK, never rounded | 43 cases: 0 executed, 100.0% blocked |
| T4 | **Splitting**: many small payments under the per-action limit | The ledger folds approved proposals into daily, hourly and monthly totals, takes the larger of the ledger and the caller, applies it at planning and again at execution, under a per-business advisory lock | 6 cases plus a concurrent-run case: **the first full run let 7 through**; fixed at the ledger, now 0, with regression tests |
| T5 | **A fooled or wrong decision that is inside every limit** | `early_payment_gate`, `untrusted_input_gate`, `forecast_confidence_gate` and `worst_case_loss_gate` turn doubt into a person's decision; autonomy starts at L0 | See residual risk R1: policy cannot distinguish it from the agent's normal job |
| T6 | **Replay or reuse of an approval** | Approval only marks a proposal; the worker re-runs policy with fresh context; a fresh BLOCK always stops; an ESCALATE proceeds only if the person's approval covered the same ruleset and the same rules | 8 cases: 0 executed (one had no effect) |
| T7 | **Time-of-check to time-of-use** (state changes between approval and execution; two runs at once) | Policy runs at both moments; advisory lock per business; deterministic proposal IDs make a repeated run write nothing new | 7 cases: 0 executed |
| T8 | **Double execution** after a crash or a retry | Idempotency key derived from the proposal ID and stored (committed) before the call; the adapter looks up an earlier request rather than creating a second; a `PENDING` row is replayed, not recreated | Crash-restart integration tests; one idempotency case |
| T9 | **Forged or replayed webhook** | HMAC signature check, timestamp tolerance, event-ID de-duplication, lifecycle table rejects backwards moves | 9 cases: 0 executed, 100.0% blocked |
| T10 | **Agent gains authority** | Autonomy level is one stored row and overlays any context a provider supplies; promotion needs evidence and a person's confirmation; demotion is automatic; L0 allows nothing | 7 cases: 0 executed |
| T11 | **Kill-switch bypass** | `kill_switch` is a required rule, evaluated for every action, and a missing value fails closed | 5 cases: 0 executed (one had no effect) |
| T12 | **Cross-business access** | Every query is scoped by business ID | 5 cases: 0 executed. This rests on the data layer, not on authentication (R4) |
| T13 | **Tampering with the record** | Append-only table with a trigger that rejects UPDATE, DELETE and TRUNCATE; hash chain over sequence, time, type, payload and the previous hash; `verify_chain()` names the first break | Tamper tests; see R6 for what it cannot detect |
| T14 | **The engine itself fails** | Fail closed: an exception, a missing field, a wrong type is a BLOCK. Strict YAML loading (no floats, no duplicate keys, no unknown parameters). Property tests: fail-closed, monotone in the amount, order-independent, a BLOCK is never outvoted | Hypothesis, 500 examples per property |
| T15 | **A lazy or hallucinated explanation** | The dashboard's explanations are generated from templates over the stored numbers, not by an LLM | Unit tests on the templates |
| T16 | **Secrets in the repository** | Credentials come from the environment; `.env` is ignored; CI runs gitleaks over the full history | Scan results are in the Day 20 notes in `docs/daily-log.md` |

## Defence in depth: which layer stopped what

Across the 156 attacks, policy stopped 113, the edge (authentication and request validation) 12, a rejected planner reply 11,
the planner itself 6, lifecycle rules 5, the dispatcher 3, and a handful by other layers. No single layer carries the result:
the red team also uses a planner that obeys any injected text, and policy still stopped it; a cap that failed (T4) was caught by fixing the layer that
owned it rather than by adding a downstream check.

## Residual risk

These are known and unsolved in this build. Ordered by how much they matter.

| # | Residual risk | Consequence | What would reduce it |
| --- | --- | --- | --- |
| R1 | **An in-envelope harmful action.** A small payment to an approved payee that injected text asks for is inside every rule. The detector's ESCALATE is the only barrier, and it has known misses (30 of 71 new planner-layer cases flagged; misses include German, Portuguese, Korean and Turkish instructions, Base32, reversed text and spaced letters, all stopped later by other rules but not by the detector) | Money moves to an approved payee for a reason the business did not intend, within caps | Lower autonomy, lower caps, per-payee limits, a payee-and-purpose check against the real obligation, a stronger detector |
| R2 | **The planner has never been run against a real model.** Every evaluation uses a scripted planner or a deliberately compromised one | The evidence says the deterministic layers hold; it says nothing about how often a real model proposes harm or how it resists injection | Run the red team and the agent evaluation with the real model |
| R3 | **Currency consistency is not a policy rule.** A wrong-currency payout is stopped by the dispatcher and the vendor | A change in either would remove the only barrier | A `payment_currency_match` rule |
| R4 | **One API key, no per-business scoping, no TLS or rate limiting in this build** | Anyone with the key can approve, read every business and change the autonomy level | Per-business credentials, roles (approver versus viewer), TLS at the edge, rate limits |
| R5 | **The kill switch has no stored authority.** Its state comes from the data provider, not from a row the operator controls | A broken provider could report it as off | A stored row and an API to flip it, as the autonomy level has |
| R6 | **Audit chain limits.** It does not detect deletion of the newest records or a complete re-hash, and the trigger does not stop a database superuser | An operator with database rights could rewrite recent history undetected | Publish the head hash outside the database; a least-privilege database role |
| R7 | **Nothing marks a payable paid when its payout settles**, so the planner can re-propose the same gap | A duplicate payment proposal, caught by caps or a person, not by design | Close the loop from settlement to the obligation |
| R8 | **The Airwallex sandbox client has never been run against the real sandbox.** Behaviours marked UNVERIFIED in [airwallex-notes.md](airwallex-notes.md) are assumptions | Real vendor behaviour may differ from the mock the tests rely on | Run the contract suite and the demo against the sandbox |
| R9 | **Limits are global and illustrative**, not per business and not calibrated to a real customer | Too loose for some, too tight for others | Per-business configuration reviewed by the customer |
| R10 | **Detector and rules are English-first and fixed.** Adversaries adapt | New phrasings will get past the detector | Continuous red teaming, with each success becoming a regression test, as Day 18 did |
| R11 | **Clocks.** A person's approval carries the database clock and an automatic one the application's | Skew could shift a limit boundary slightly; future-dated approvals count against the limits (the conservative reading) | One time source |
| R12 | **The harness is not the world.** The database is a local throwaway, the vendor a recording mock, and the case counts are a floor, not coverage | The numbers are not a safety guarantee | Independent review and a real-sandbox red team |

## Out of scope

Compromise of the host or the database superuser, supply-chain attacks on dependencies, the security of Airwallex itself,
denial of service, and regulatory obligations (licensing, KYC, data protection). A real deployment needs a review of each
before any real funds move.

## What would have to be true before real money

1. R2 and R8 closed: a real model and the real sandbox, both red-teamed.
2. R4 and R5 closed: per-business authentication and a stored, operable kill switch.
3. R1 judged acceptable by the customer, with autonomy left at L0 or L1 until the evidence supports more.
4. A security and regulatory review.
