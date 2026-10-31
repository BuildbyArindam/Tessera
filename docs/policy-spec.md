# Policy engine specification

Status: Day 8 (core engine and ten rules, property-tested; the tenth is simulation-based).
Day 14 adds the autonomy gate.

## What it is

The policy engine is the only gate between a proposal and any money movement. A proposal comes
from the LLM planner, which has no authority. The engine answers one question: **may this
proposal run?** It answers with exactly one of three verdicts.

| Verdict | Meaning | What happens next |
| --- | --- | --- |
| `APPROVE` | Every rule passed. | The worker may execute it (after re-checking policy at execution time, Day 13). |
| `ESCALATE` | No rule forbids it, but a rule wants a human to look. | Queued for one-tap approval. |
| `BLOCK` | A rule forbids it, or the engine could not be sure. | Logged. Never executed. |

The engine is plain Python code. It is deterministic: no clock, no randomness, no network, no
database, no LLM. The same proposal, context and rules always produce the same decision.

## The call

```python
decision = evaluate(proposal, context, ruleset)   # -> PolicyDecision
```

- **proposal**: what is being asked (`action_type`, `params`, `rationale`, `confidence`). The
  `rationale` and any other free text is never read by a rule and never copied into a reason.
- **context** (`PolicyContext`): a read-only snapshot of the world, assembled by the caller:
  balances, spend so far today, the beneficiary allowlist and when each beneficiary was added,
  forecast confidence per currency, FX reference rates and the executable quote's rate, recent
  approval times, card value loaded this month, the decision time (`now`), the autonomy level, and
  whether the kill switch is engaged. No rule reads a clock: `now` comes from the caller.
- **ruleset**: the validated rules, loaded from YAML.
- **decision**: `verdict`, `reasons` (human-readable), `rule_ids` (which rules objected), and
  `ruleset_hash` (which exact rules were in force).

`rule_ids` lists the rules that returned something other than PASS. An `APPROVE` therefore has
empty `reasons` and `rule_ids`; the `ruleset_hash` is what says which rules it passed.

## How rules combine

1. Every rule runs, in alphabetical order of rule id (so the order they are listed never matters).
2. Each rule returns `PASS`, `ESCALATE` or `BLOCK`, with a reason for the last two.
3. Any `BLOCK` makes the decision `BLOCK`. Otherwise any `ESCALATE` makes it `ESCALATE`.
   Otherwise it is `APPROVE`.

A single `BLOCK` can never be outvoted.

## Fail closed

When the engine is unsure, the answer is `BLOCK`, never `APPROVE`. The first reason is then the
fixed string `policy_error`, followed by which rule had the problem. This happens when:

| Situation | Result |
| --- | --- |
| A rule raises an exception | `BLOCK`, `policy_error`; only the exception *type* is reported |
| A rule needs a context field the caller left out (`None`) *for this action* | `BLOCK`, `policy_error`; the rule is not run |
| A rule returns something that is not a valid result for itself | `BLOCK`, `policy_error` |
| The proposal, context or ruleset is not the expected type | `BLOCK`, `policy_error` |
| Anything unexpected inside the engine itself | `BLOCK`, `policy_error` |

Other rules still run, so one broken rule does not hide a second problem.

**Missing context is action-aware (Day 7).** A rule declares what it needs *for the proposal in
front of it* (`required_context_for`). A `HOLD` or `FLAG_INVOICE` therefore does not fail for lack
of an FX quote, but a `CONVERT` without one does. The one deliberate exception is the forecast
confidence gate, where missing data is itself a reason to ask a human (`ESCALATE`, not an error).

## Proposal fields the rules read

`params` is loose JSON from the planner, so each rule reads it strictly and blocks anything
unreadable. Amounts are a string of ASCII digits with an optional fraction (`"250.50"`) or an
integer: not a float, boolean, list, `"1e3"`, `"1_000"`, `" 5"`, `"+5"`, a full-width digit, or
more than two decimal places. Ids are 1 to 64 ASCII letters, digits, `_` or `-`.

| Action | Required params | Notes |
| --- | --- | --- |
| `CONVERT` | `amount`, `currency`, `to_currency` | `amount` is the sell side; `to_currency` must differ |
| `PAY_SUPPLIER` | `amount`, `currency`, `beneficiary_id` | the id is looked up, never trusted |
| `TOPUP_CARD` | `amount`, `currency`, `vendor_id` | |
| `HOLD`, `FLAG_INVOICE` | none | move no money; only the kill switch can stop them |

Rates are never taken from a proposal: the reference rate and the executable quote come from the
context, so the planner cannot name the rate it is judged against.

## Rules in force today

### `kill_switch` (required)

Blocks **every** proposal, of every action type, while the operator has engaged it. Whether it is
engaged is runtime state (`context.kill_switch_engaged`), not a YAML value, so it can be flipped
instantly without redeploying rules. If the caller does not say whether it is engaged
(`None`), the engine fails closed.

A ruleset that does not contain a `kill_switch` rule cannot be loaded.

File: `rules/kill_switch.yaml`. Parameters: none.

### `max_amount_per_action`

A hard ceiling on the money a single action may move, per currency, measured in the currency
leaving the account.

| Parameter | Meaning |
| --- | --- |
| `max_amount` | Map of currency code to cap. Quote the amounts. |

Behaviour:

- An amount **equal** to the cap is allowed; one cent more is `BLOCK`.
- A currency with **no** cap configured is `BLOCK` (it is not assumed unlimited).
- `CONVERT`, `PAY_SUPPLIER` and `TOPUP_CARD` are checked. `HOLD` and `FLAG_INVOICE` move no
  money and pass.
- The amount must be a string or integer in `params.amount` with a valid `params.currency`. These
  are `BLOCK`, never rounded or guessed: floats, booleans, lists, `NaN`, `Infinity`, zero,
  negative numbers, more than two decimal places (for example `100.001`), unknown or lower-case
  currencies, and a missing amount or currency.

Example (`rules/max_amount_per_action.yaml`):

```yaml
id: max_amount_per_action
type: max_amount_per_action
params:
  max_amount:
    USD: "5000.00"
    INR: "400000.00"
```

| Proposal | Verdict |
| --- | --- |
| CONVERT 5000.00 USD | `APPROVE` |
| CONVERT 5000.01 USD | `BLOCK` ("amount 5000.01 USD exceeds the per-action cap of 5000.00 USD") |
| CONVERT 100.001 USD | `BLOCK` (more than 2 decimal places) |
| PAY_SUPPLIER 10 in a currency with no cap | `BLOCK` |
| any proposal, kill switch engaged | `BLOCK` |

### The seven Day 7 rules

All amounts and percentages in YAML are quoted strings (`"15000.00"`); an unquoted `15000.5` is a
float and is refused at load time. Equal to a limit passes everywhere; one cent (or one tick) over
does not. Every rule below ignores the actions it does not apply to.

| Rule | Applies to | Parameters (default) | Verdict when it objects |
| --- | --- | --- | --- |
| `daily_cumulative_cap` | CONVERT, PAY_SUPPLIER, TOPUP_CARD | `daily_cap`: per-currency total (USD `"15000.00"`) | `BLOCK` if today's approved spend + this amount > cap, or no cap for the currency |
| `beneficiary_allowlist` | PAY_SUPPLIER | `new_beneficiary_cooldown_hours` (24) | `BLOCK` if not on the allowlist or `beneficiary_id` malformed; `ESCALATE` if added < 24h ago, added time unknown, or added in the future |
| `currency_concentration_cap` | CONVERT | `max_percent_of_balance` (`"40.00"`) | `BLOCK` if amount > that percent of the source balance (no balance = nothing to convert) |
| `forecast_confidence_gate` | CONVERT, PAY_SUPPLIER, TOPUP_CARD | `max_relative_width` (`"0.5000"`) | `ESCALATE` if interval width / point estimate exceeds it, **or if confidence data is missing** |
| `quote_slippage_guard` | CONVERT | `max_slippage_percent` (`"0.50"`) | `BLOCK` if the quote differs from the reference rate by more than that, in either direction; also if either rate is missing |
| `velocity_limit` | CONVERT, PAY_SUPPLIER, TOPUP_CARD | `max_actions_per_hour` (10) | `BLOCK` once that many actions were approved in the rolling hour before `now` |
| `card_limit_cap` | TOPUP_CARD | `vendors`: per vendor `currency`, `per_top_up_max`, `per_month_max` | `BLOCK` if the vendor is unknown, the currency differs, or either ceiling would be exceeded |

### `worst_case_loss_gate` (Day 8)

| Rule | Applies to | Parameters (default) | Verdict when it objects |
| --- | --- | --- | --- |
| `worst_case_loss_gate` | CONVERT, PAY_SUPPLIER (a simulation is required); HOLD (judged only if one is supplied) | `max_worst_case_loss`: per-currency ceiling (USD `"500.00"`) | `ESCALATE` if the simulated 5th-percentile loss is above the ceiling, **or if a CONVERT / PAY_SUPPLIER has no simulation**; `BLOCK` if the simulation belongs to another proposal, or the currency has no ceiling |

The simulation arrives in the context (`context.simulation`, a `SimulationSummary` built with
`simulate(...).summary()`), never in the proposal, so the model cannot state its own risk. The summary
carries the proposal id, action and amount; one that does not match the proposal in front of the rule
is `BLOCK`, so a stale report, or a small simulation for a larger action, cannot vouch for it. A loss
equal to the ceiling passes. The loss is in the SELL currency (the one the business holds). TOPUP_CARD
and FLAG_INVOICE always pass this rule. See "Simulation" below for what the number means.

Worked examples with the shipped defaults (USD, balance 10,000.00 unless stated):

| Proposal and context | Verdict | Why |
| --- | --- | --- |
| CONVERT 1,000 USD to EUR, clean context | `APPROVE` | all rules pass |
| CONVERT 4,000.01 USD | `BLOCK` | over 40% of the balance |
| PAY_SUPPLIER 500 USD to a beneficiary not on the list | `BLOCK` | `beneficiary_allowlist` |
| PAY_SUPPLIER 500 USD to a beneficiary added 2 hours ago | `ESCALATE` | inside the 24h cooldown |
| Fourth 5,000.00 USD conversion in a day (3 already approved) | `BLOCK` | 15,000.00 daily cap |
| CONVERT with quote 0.9247 against reference 0.9200 | `BLOCK` | 0.51% > 0.50% |
| Any money action when forecast confidence is missing | `ESCALATE` | cannot say how sure the forecast is |
| 10 actions approved in the last hour, then another | `BLOCK` | velocity limit |
| TOPUP_CARD 200.01 USD for `workspace_suite` | `BLOCK` | 200.00 per-top-up ceiling |
| TOPUP_CARD 200 USD when 450 already loaded this month | `BLOCK` | 600.00 monthly ceiling |
| CONVERT 4,000 USD whose simulated 5th-percentile loss is 929.49 USD | `ESCALATE` | 500.00 worst-case loss ceiling |
| CONVERT with no simulation in the context | `ESCALATE` | the downside is unknown |

The caller's duties (the engine is pure): `spend_today` must include approved-but-not-yet-executed
actions, `recent_approval_times` must list recent approvals, `card_loaded_this_month` must use the
same calendar-month boundary as the card ceilings, and a `CONVERT` needs an executable quote
fetched **before** asking for a decision.

## Simulation (Day 8)

`tessera_core.simulate.simulate(proposal, fx_history, as_of, n_paths, seed, *, due_date, config)` replays
a proposal over block-bootstrapped FX paths and returns a `SimulationReport`. `replay(...)` runs the
same proposal over the rates that actually happened, for backtests; it returns a `ReplayReport` that
has no `summary()`, so it cannot reach a live decision.

- **Paths.** Historical daily log returns up to `as_of` only (the series is sliced by `FxSeries.up_to`
  before anything else). Moving blocks of 5 business days by default, drift removed by default, steps =
  calendar days x 252/365 (the Day 5 convention). Seeded `numpy.random.Generator`: same inputs and seed,
  identical report. Fully vectorised: 5,000 paths over a year in about 20 ms.
- **Comparator.** CONVERT and PAY_SUPPLIER are scored against "convert on the due date" at the plain
  mid rate, so the proposal has to earn back its own spread and fee. HOLD is scored against "convert
  now". `saving` is proposal minus comparator in the sell currency; negative is a loss.
- **Report.** `expected_saving`, `saving_std`, `saving_p05`, `saving_p50`, `worst_case_loss`
  (= max(0, -p05)), `prob_beats_baseline` (P(saving > 0)), `spread_cost`, `fee_cost`.
- **The due date is not in the proposal.** It is a keyword argument supplied by the caller from the
  obligation being worked on. If the planner could name it, it could set it to today and make the
  worst-case loss zero.
- **Costs are assumptions.** Spread defaults to 50 bps (the Day 1 mock's figure) and the fee to zero.
  They are not measured Airwallex prices.

## Configuration

One YAML file per rule in `policy/rules/`. The file name must equal the rule `id`.

```yaml
id: <lowercase_snake_case>     # unique
type: <registered rule type>   # e.g. max_amount_per_action
params: { ... }                # validated by that rule's own config model
```

Rules are Python classes registered under a string `type`. There is no expression language and
no `eval`/`exec`: a rule can only do what its Python code does, configured by the values above.

Mistakes are caught when the rules are **loaded**, not when money is moving. These all refuse to
load: an unknown `type`, an unknown or missing parameter, duplicate rule ids, a file name that
does not match its id, an empty ruleset, a ruleset with no `kill_switch`, a non-positive or
non-numeric amount, a float where an amount belongs, and a **duplicate YAML key** (stock YAML would
silently keep the last one, which is dangerous for a limit).

## Traceability: the ruleset hash

`ruleset_hash` is a SHA-256 over the *meaning* of the rules: each rule's id, type, a version
number for its logic, and its validated parameters in a canonical form. So:

- changing a limit, adding, removing or renaming a rule changes the hash;
- editing a comment, reordering keys, or writing `"5000"` instead of `"5000.00"` does not;
- changing a rule's code in a way that alters its behaviour requires bumping that rule's
  `type_version`, which changes the hash.

Every decision carries the hash, and the audit log (Day 11) stores it, so any decision can be tied
to the exact rules that produced it.

## Adding a rule (for Day 7 onward)

1. Subclass `Rule` with a config model, set `type_name` and, if needed, `required_context`.
2. Return `PASS` / `ESCALATE` / `BLOCK`; build reasons only from validated values and fixed text,
   never from proposal free text.
3. Decorate with `@register_rule` and import the module in `builtin_rules/__init__.py`.
4. Add a YAML file and tests, including a failure and a boundary case.

## Property tests (Day 7)

`tests/unit/test_policy_properties.py` uses Hypothesis (500 examples per property) to check four
invariants against the shipped ruleset, with generated proposals (valid ones, and ones with one to
three injected faults: hostile values, deleted fields, stray keys) and generated contexts:

1. **Fail closed.** A proposal an independent oracle judges malformed is `BLOCK`, never anything
   else; any non-proposal, non-context or non-ruleset object yields `BLOCK` / `policy_error`.
2. **Monotonic.** Raising an amount never makes the verdict more permissive; a `BLOCK` stays `BLOCK`.
3. **Order independent.** Shuffling the rules (even in a hand-built `Ruleset`) gives an identical
   decision, and shuffling the specs gives an identical ruleset hash.
4. **A BLOCK is never outvoted.** If any single rule would `BLOCK` on its own, the engine `BLOCK`s;
   more exactly, the verdict equals the strictest verdict any member gives alone. Test-only rules that
   raise or escalate are mixed in.

Monotonicity is a property of the shipped rules (each limit only gets tighter as an amount grows,
and the other checks ignore the amount). A future rule that is not monotonic in the amount, such as
"escalate small amounts, approve large ones", would fail it and must be reconsidered.

## Known limits at Day 7

- The ruleset has no autonomy gate yet (Day 14).
- `daily_cumulative_cap`, `velocity_limit` and `card_limit_cap` trust the caller's counters
  (`spend_today`, `recent_approval_times`, `card_loaded_this_month`). Two proposals decided at the same
  moment from the same snapshot could both pass; the worker must serialise decisions or re-check at
  execution time (Day 13).
- Limits are single global values (and per-vendor for cards), not per business; per-customer
  configuration comes later. Defaults are illustrative.
- Slippage compares against a reference rate the caller supplies; if that source is wrong, the check
  is only as good as it is.
- Currencies are compared one at a time. There is no cross-currency total (for example, a daily cap
  across all currencies in USD terms).
