"""Day 7 verification: policy rules and property tests. Run: .venv/bin/python scripts/verify_day7.py

Mirrors the Day 7 verification prompt. Prints PASS/FAIL per check and exits non-zero on any failure.
  1. runs the whole policy test suite, Hypothesis included, and checks each property ran >= 500 examples;
  2. tries five adversarial proposals (negative, zero, huge Decimal, wrong currency, missing beneficiary id);
  3. prints a table of every rule, its YAML parameters, and one proposal that triggers it (decided live).
"""

import re
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests/unit")]

from policy_helpers import NOW, full_context, make_proposal, usd  # noqa: E402
from pydantic import ValidationError  # noqa: E402
from tessera_core.domain import ActionType, Proposal, Verdict  # noqa: E402
from tessera_core.domain.currency import Currency  # noqa: E402
from tessera_core.policy import DEFAULT_RULES_DIR, evaluate, ruleset_from_yaml_texts  # noqa: E402

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


# Scoped to the nine Day 7 rules. Day 8 added `worst_case_loss_gate` to the shipped directory; it is
# verified by scripts/verify_day8.py, and leaving it in here would make check (c) fail for lack of an example.
DAY7_FILES = {
    p.name: p.read_text(encoding="utf-8") for p in DEFAULT_RULES_DIR.glob("*.yaml") if p.stem != "worst_case_loss_gate"
}
RULESET = ruleset_from_yaml_texts(DAY7_FILES)
print(f"ruleset: {[r.rule_id for r in RULESET.rules]}\nhash: {RULESET.hash}\n")

# ---- 1. the whole policy suite, Hypothesis statistics on ------------------------------------
print("=== 1. policy test suite (pytest, Hypothesis statistics) ===")
run = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/unit/test_policy_engine.py", "tests/unit/test_policy_ruleset.py",
     "tests/unit/test_policy_rules_money.py", "tests/unit/test_policy_rules_gates.py",
     "tests/unit/test_policy_properties.py", "-q", "-p", "no:cacheprovider", "--hypothesis-show-statistics"],
    cwd=ROOT, capture_output=True, text=True,
)  # fmt: skip
stats = re.findall(r"::(test_\w+):\n(?:.*\n)*?\s+- (\d+) passing, (\d+) failing, and (\d+) invalid", run.stdout)
print(f"{'property':62s} {'passing':>8s} {'failing':>8s} {'invalid':>8s}")
for name, passing, failing, invalid in stats:
    print(f"{name:62s} {passing:>8s} {failing:>8s} {invalid:>8s}")
print(run.stdout.strip().splitlines()[-1])
check("(a) whole policy suite passes", run.returncode == 0, run.stdout.strip().splitlines()[-1])
check(
    "(b) all 6 Hypothesis properties ran >= 500 passing examples, 0 failing",
    len(stats) == 6 and all(int(p) >= 500 and int(f) == 0 for _, p, f, _ in stats),
    f"{len(stats)} properties, min passing {min((int(p) for _, p, _, _ in stats), default=0)}",
)

# ---- 2. five adversarial proposals ------------------------------------------------------------
print("\n=== 2. adversarial proposals (full shipped ruleset, otherwise clean context) ===")
ADVERSARIAL: list[tuple[str, ActionType, dict[str, object]]] = [
    ("negative amount", ActionType.CONVERT, {"amount": "-100.00", "currency": "USD", "to_currency": "EUR"}),
    ("zero amount", ActionType.CONVERT, {"amount": "0.00", "currency": "USD", "to_currency": "EUR"}),
    ("huge Decimal", ActionType.CONVERT, {"amount": "9" * 40 + ".99", "currency": "USD", "to_currency": "EUR"}),
    ("wrong currency", ActionType.PAY_SUPPLIER, {"amount": "10.00", "currency": "XYZ", "beneficiary_id": "sup_old"}),
    ("missing beneficiary id", ActionType.PAY_SUPPLIER, {"amount": "10.00", "currency": "USD"}),
]
for label, action, params in ADVERSARIAL:
    try:
        p = Proposal(id="adv", action_type=action, params=params, rationale="x", confidence=0.5, created_at=NOW)
    except ValidationError:
        check(f"{label}: rejected at validation", True)
        continue
    d = evaluate(p, full_context(), RULESET)
    check(f"{label}: {d.verdict.value}", d.verdict is Verdict.BLOCK, d.reasons[0][:96])
extra = {"float amount": 100.5, "NaN": "NaN", "exponent": "1e3", "fullwidth digits": "１００", "bool": True}
for label, amount in extra.items():
    d = evaluate(make_proposal(amount=amount, to_currency="EUR"), full_context(), RULESET)
    check(f"extra: {label}: {d.verdict.value}", d.verdict is Verdict.BLOCK)

# ---- 3. every rule, its YAML parameters, one triggering proposal --------------------------------
print("\n=== 3. rule table (each example is decided live by the shipped ruleset) ===")
conv = lambda amt, **k: make_proposal(ActionType.CONVERT, amt, "USD", to_currency="EUR", **k)  # noqa: E731
pay = lambda ben: make_proposal(ActionType.PAY_SUPPLIER, "500.00", "USD", beneficiary_id=ben)  # noqa: E731
card = make_proposal(ActionType.TOPUP_CARD, "200.01", "USD", vendor_id="workspace_suite")
EXAMPLES = {
    "kill_switch": (conv("10.00"), full_context(kill_switch_engaged=True), "kill switch engaged"),
    "max_amount_per_action": (
        conv("5000.01"),
        full_context(balances={Currency.USD: usd("90000.00")}),
        "CONVERT 5000.01 USD",
    ),
    "daily_cumulative_cap": (
        conv("1000.00"),
        full_context(spend_today={Currency.USD: usd("14000.01")}),
        "14000.01 spent today + 1000.00",
    ),
    "beneficiary_allowlist": (pay("sup_ghost"), full_context(), "PAY_SUPPLIER to unlisted sup_ghost"),
    "currency_concentration_cap": (conv("4000.01"), full_context(), "CONVERT 4000.01 of a 10000.00 balance"),
    "forecast_confidence_gate": (conv("100.00"), full_context(forecast_confidence=None), "no confidence data"),
    "quote_slippage_guard": (
        conv("100.00"),
        full_context(fx_quote_rate={"USD/EUR": Decimal("0.9247")}),
        "quote 0.9247 vs ref 0.9200",
    ),
    "velocity_limit": (
        conv("100.00"),
        full_context(recent_approval_times=tuple(NOW - timedelta(minutes=m) for m in range(1, 11))),
        "10 approvals in the last hour",
    ),
    "card_limit_cap": (card, full_context(), "TOPUP_CARD 200.01 for workspace_suite"),
}
check("(c) every shipped rule has an example in the table", set(EXAMPLES) == {r.rule_id for r in RULESET.rules})


def compact(value: object) -> str:
    """Render validated config readably: Money as '5000.00 USD', mappings as 'k=v, k=v'."""
    if isinstance(value, dict):
        if set(value) == {"amount", "currency"}:
            return f"{value['amount']} {value['currency']}"
        return (
            ", ".join(
                f"{k}={compact(v)}" if isinstance(v, dict) and "amount" not in v else f"{k}: {compact(v)}"
                for k, v in value.items()
            )
            or "(none)"
        )
    return str(value)


rows = []
for rule in RULESET.rules:
    prop, ctx, what = EXAMPLES[rule.rule_id]
    d = evaluate(prop, ctx, RULESET)
    triggered = rule.rule_id in d.rule_ids and d.verdict is not Verdict.APPROVE
    check(f"{rule.rule_id} triggers", triggered, d.verdict.value)
    reason = next((r for r in d.reasons if r.startswith(rule.rule_id)), "-").split(": ", 1)[-1]
    rows.append((rule.rule_id, compact(rule.config.model_dump(mode="json")), what, d.verdict.value, reason))

print()
for rule_id, params, what, verdict, reason in rows:
    print(f"* {rule_id}  ->  {verdict}")
    print(f"    YAML params : {params}")
    print(f"    example     : {what}")
    print(f"    reason      : {reason}")
# the ESCALATE half of the beneficiary rule
d = evaluate(pay("sup_new"), full_context(), RULESET)
check("beneficiary added 2h ago => ESCALATE", d.verdict is Verdict.ESCALATE and "beneficiary_allowlist" in d.rule_ids)

print(f"\nYAML files (Day 7 scope): {sorted(DAY7_FILES)}")
print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
