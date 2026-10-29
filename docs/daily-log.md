# Daily log

One paragraph per day: what changed, what surprised you, one thing you would do differently.

## Day 1

Monorepo scaffolded on Python 3.11 with editable installs for every package (`make setup`), ruff, mypy (strict on `tessera_core`), pytest, pre-commit and a Makefile whose `eval`, `redteam` and `demo` targets are placeholders for later days. `tessera_core.domain` has `Currency`, a Decimal-only `Money`, the entities, `ActionType`, `Proposal` and `PolicyDecision`. `tessera_airwallex` has the abstract `AirwallexPort`, typed errors and a deterministic in-memory `MockAirwallex` with a spread, quote expiry and idempotency keys. 68 unit tests, lint and typecheck are green. **Surprise:** my own test for a huge exponent (`1e999999999`) found a real bug: `abs()` on a Decimal runs in the global context and raised `decimal.Overflow` instead of a validation error; `copy_abs()` fixed it. **Do differently:** I would record the Airwallex reference (`docs/airwallex-notes.md`) before writing the port, since the port's method shapes are guesses until then.

### Assumptions and decisions (Day 1)

- **Floats are rejected, not converted.** `Money(0.1, USD)` raises; use `Money("0.1", USD)` or `Decimal`. Rule 5 says never use floats for money, and silent conversion would hide the error. JSON floats for amounts are rejected too, so amounts serialise as strings.
- **Rounding is banker's (ROUND_HALF_EVEN)** at 2 dp, with `|amount| <= 1e15` as a sanity bound.
- **Cross-currency ordering raises; equality is just False.** `Money(1, USD) == Money(1, EUR)` is False rather than an error.
- **`PolicyDecision.reasons` / `rule_ids` are tuples** (immutable), and a non-APPROVE verdict must state at least one reason.
- **`FxQuote` lives in `tessera_core`** (policy needs it for the slippage guard on Day 7, and core cannot import the airwallex package). Other result models (`Transfer`, `Card`, `ConversionResult`, `BeneficiaryRef`) live in `tessera_airwallex.models`.
- **`create_fx_quote` and `get_balances` take no idempotency key** (they move no money); all five mutating methods do. A key reused for a different request raises `IdempotencyConflictError`.
- **Beneficiary bank details are `SecretStr`** so they never appear in reprs or logs.
- **The mock books the spread as `fees_collected`**, giving the exact invariant `bought + fee == sold * mid_rate`.
- **Airwallex endpoints are not yet researched.** `docs/airwallex-notes.md` is a TODO template; the port is our own contract, and the sandbox client is Day 12.
