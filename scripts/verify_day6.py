"""Day 6 verification: policy engine core. Run: .venv/bin/python scripts/verify_day6.py

Prints PASS/FAIL per check and exits non-zero on any failure. Checks (a)-(f) mirror the Day 6
verification prompt; the unit tests in tests/unit/test_policy_*.py cover them in more depth.
"""

import ast
import sys
from datetime import UTC, datetime
from pathlib import Path

from tessera_core.domain import ActionType, Proposal, Verdict
from tessera_core.policy import POLICY_ERROR, PolicyContext, evaluate, load_ruleset, ruleset_from_yaml_texts

POLICY_DIR = Path(__file__).resolve().parents[1] / "packages/core/tessera_core/policy"
FORBIDDEN_IMPORTS = {
    "requests", "httpx", "urllib", "urllib3", "http", "socket", "aiohttp", "websockets", "ftplib", "smtplib",
    "sqlalchemy", "psycopg", "psycopg2", "asyncpg", "sqlite3", "pymongo", "redis",
    "openai", "anthropic", "langchain", "litellm", "subprocess",
    "tessera_agent", "tessera_airwallex", "tessera_audit", "tessera_api", "tessera_worker",
}  # fmt: skip
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}

failures = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def proposal(action: ActionType = ActionType.CONVERT, amount: str = "100.00", currency: str = "USD") -> Proposal:
    return Proposal(
        id="p", action_type=action, params={"amount": amount, "currency": currency},
        rationale="x", confidence=0.5, created_at=datetime(2026, 10, 30, tzinfo=UTC),
    )  # fmt: skip


def ctx(kill: bool | None = False) -> PolicyContext:
    return PolicyContext(kill_switch_engaged=kill)


rs = load_ruleset()
print(f"ruleset: {[r.rule_id for r in rs.rules]}  hash={rs.hash}\n")

# (a) kill switch on => every proposal BLOCK
verdicts = {a: evaluate(proposal(a), ctx(kill=True), rs).verdict for a in ActionType}
check("(a) kill switch blocks every action type", all(v is Verdict.BLOCK for v in verdicts.values()), str(len(verdicts)))

# (b) just under cap APPROVE, at cap APPROVE, just over BLOCK
under, at, over = (evaluate(proposal(amount=a), ctx(), rs).verdict for a in ("4999.99", "5000.00", "5000.01"))
check("(b) 4999.99 APPROVE / 5000.00 APPROVE / 5000.01 BLOCK",
      (under, at, over) == (Verdict.APPROVE, Verdict.APPROVE, Verdict.BLOCK), f"{under.value}/{at.value}/{over.value}")  # fmt: skip
print("      reason for 5000.01:", evaluate(proposal(amount="5000.01"), ctx(), rs).reasons)

# (c) a rule that raises => BLOCK with policy_error (test-only rule type)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests/unit"))
from policy_helpers import ruleset_with  # noqa: E402

d = evaluate(proposal(), ctx(), ruleset_with("test_boom"))
check("(c) raising rule => BLOCK policy_error", d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR, str(d.reasons))

# (d) missing context => BLOCK
d = evaluate(proposal(), ctx(kill=None), rs)
check("(d) missing kill-switch state => BLOCK policy_error", d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR, str(d.reasons))
d = evaluate(proposal(), ctx(), ruleset_with("test_needs_balances"))
check("(d) rule needing absent balances => BLOCK policy_error", d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR)  # fmt: skip

# (e) same inputs => same decision and hash across 100 runs
p, c = proposal(amount="5000.01"), ctx()
first = evaluate(p, c, rs)
check("(e) identical decision and ruleset_hash over 100 runs",
      all(evaluate(p, c, rs) == first for _ in range(100)) and first.ruleset_hash == rs.hash)  # fmt: skip

# (f) editing a YAML value changes the hash
cap_yaml = (POLICY_DIR / "rules/max_amount_per_action.yaml").read_text()
kill_yaml = (POLICY_DIR / "rules/kill_switch.yaml").read_text()
base = ruleset_from_yaml_texts({"kill_switch.yaml": kill_yaml, "max_amount_per_action.yaml": cap_yaml}).hash
edited = ruleset_from_yaml_texts(
    {"kill_switch.yaml": kill_yaml, "max_amount_per_action.yaml": cap_yaml.replace('"5000.00"', '"5000.01"', 1)}
).hash
check("(f) editing one YAML value changes the hash", base == rs.hash and edited != base, f"{base[:12]} -> {edited[:12]}")

# Purity: AST scan of the policy package for forbidden imports and eval/exec
bad_imports: list[str] = []
bad_calls: list[str] = []
for path in sorted(POLICY_DIR.rglob("*.py")):
    for node in ast.walk(ast.parse(path.read_text())):
        mods: list[str] = []
        if isinstance(node, ast.Import):
            mods = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods = [node.module]
        bad_imports += [f"{path.name}: {m}" for m in mods if m.split(".")[0] in FORBIDDEN_IMPORTS]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_CALLS:
            bad_calls.append(f"{path.name}:{node.lineno} {node.func.id}()")
n_files = len(list(POLICY_DIR.rglob("*.py")))
check(f"no network/db/LLM/agent/airwallex imports in policy ({n_files} files scanned)", not bad_imports, "; ".join(bad_imports))
check("no eval/exec/compile/__import__ calls in policy", not bad_calls, "; ".join(bad_calls))

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
