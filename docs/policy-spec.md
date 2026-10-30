# Policy engine specification

Status: Day 6 (core engine and two rules). Day 7 adds the remaining rules; Day 8 adds a
simulation-based rule; Day 14 adds the autonomy gate.

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
  balances, spend so far today, beneficiary allowlist, forecast confidence per currency, FX
  reference rates, current autonomy level, and whether the kill switch is engaged.
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
| A rule needs a context field the caller left out (`None`) | `BLOCK`, `policy_error`; the rule is not run |
| A rule returns something that is not a valid result for itself | `BLOCK`, `policy_error` |
| The proposal, context or ruleset is not the expected type | `BLOCK`, `policy_error` |
| Anything unexpected inside the engine itself | `BLOCK`, `policy_error` |

Other rules still run, so one broken rule does not hide a second problem.

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

## Known limits at Day 6

- Only two rules exist. **This ruleset is not yet enough to protect a payout:** there is no
  beneficiary allowlist, daily cumulative cap, velocity limit or confidence gate until Day 7.
  Do not wire the engine to execution before then.
- The money convention for `params` (`amount` and `currency` meaning value leaving the account) is
  a Day 6 assumption. Day 7 rules refine the per-action fields.
- The per-action cap does not look at what was already spent today; that is the daily cumulative
  cap (Day 7). Splitting a large payment into several capped ones is not yet stopped.
- `PolicyContext` fields beyond `kill_switch_engaged` are defined but unused until Day 7.
