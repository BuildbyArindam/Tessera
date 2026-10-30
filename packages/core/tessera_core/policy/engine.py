"""The policy engine: evaluate(proposal, context, ruleset) -> PolicyDecision.

Properties the rest of the system relies on (each has a test):
- Deterministic: no clock, randomness, I/O or global state. Same inputs, same decision.
- Order independent: rules run in sorted rule_id order, so the decision never depends on how
  the rules were listed.
- Combination: any BLOCK -> BLOCK; else any ESCALATE -> ESCALATE; else APPROVE.
- Fail closed: a rule that raises, a rule that needs context the caller did not supply, a
  malformed rule result, or anything unexpected inside the engine yields BLOCK with first
  reason `policy_error`. There is no code path where an error becomes an APPROVE.
- Every decision carries the ruleset hash that produced it.

Rule reasons are built only from rule ids, validated amounts and fixed text. They never echo
`proposal.rationale` or other free text, which is untrusted.
"""

from tessera_core.domain.actions import PolicyDecision, Proposal, Verdict
from tessera_core.policy.context import PolicyContext
from tessera_core.policy.rule import RuleOutcome, RuleResult
from tessera_core.policy.ruleset import Ruleset

POLICY_ERROR = "policy_error"
UNKNOWN_RULESET_HASH = "unknown"
_ENGINE_ID = "engine"


def _policy_error(ruleset_hash: str, errors: list[tuple[str, str]], other: list[RuleResult]) -> PolicyDecision:
    reasons = [POLICY_ERROR, *(f"{rule_id}: {detail}" for rule_id, detail in errors)]
    reasons += [f"{r.rule_id}: {r.reason}" for r in other if r.outcome is not RuleOutcome.PASS]
    rule_ids = sorted(
        {rule_id for rule_id, _ in errors} | {r.rule_id for r in other if r.outcome is not RuleOutcome.PASS}
    )
    return PolicyDecision(
        verdict=Verdict.BLOCK, reasons=tuple(reasons), rule_ids=tuple(rule_ids), ruleset_hash=ruleset_hash
    )


def _run(proposal: Proposal, context: PolicyContext, ruleset: Ruleset) -> PolicyDecision:
    if not isinstance(proposal, Proposal):
        raise TypeError("proposal is not a Proposal")
    if not isinstance(context, PolicyContext):
        raise TypeError("context is not a PolicyContext")

    errors: list[tuple[str, str]] = []
    results: list[RuleResult] = []
    # Sorted here as well as at build time, so a hand-assembled Ruleset cannot make order matter.
    for rule in sorted(ruleset.rules, key=lambda r: r.rule_id):
        try:
            needed = rule.required_context_for(proposal)
            missing = [name for name in needed if getattr(context, name, None) is None]
        except Exception as exc:
            errors.append((rule.rule_id, f"context check raised {type(exc).__name__}"))
            continue
        if missing:
            errors.append((rule.rule_id, f"missing context: {', '.join(missing)}"))
            continue
        try:
            result = rule.evaluate(proposal, context)
        except Exception as exc:
            errors.append((rule.rule_id, f"rule raised {type(exc).__name__}"))
            continue
        if not isinstance(result, RuleResult) or result.rule_id != rule.rule_id:
            errors.append((rule.rule_id, "rule returned an invalid result"))
            continue
        results.append(result)

    if errors:
        return _policy_error(ruleset.hash, errors, results)

    flagged = [r for r in results if r.outcome is not RuleOutcome.PASS]
    blocks = [r for r in flagged if r.outcome is RuleOutcome.BLOCK]
    if blocks:
        verdict = Verdict.BLOCK
    elif flagged:
        verdict = Verdict.ESCALATE
    else:
        verdict = Verdict.APPROVE
    return PolicyDecision(
        verdict=verdict,
        reasons=tuple(f"{r.rule_id}: {r.reason}" for r in flagged),
        rule_ids=tuple(r.rule_id for r in flagged),
        ruleset_hash=ruleset.hash,
    )


def evaluate(proposal: Proposal, context: PolicyContext, ruleset: Ruleset) -> PolicyDecision:
    ruleset_hash = ruleset.hash if isinstance(ruleset, Ruleset) else UNKNOWN_RULESET_HASH
    try:
        if not isinstance(ruleset, Ruleset):
            raise TypeError("ruleset is not a Ruleset")
        return _run(proposal, context, ruleset)
    except Exception as exc:  # last resort: anything unexpected must still fail closed
        return _policy_error(ruleset_hash, [(_ENGINE_ID, f"engine raised {type(exc).__name__}")], [])
