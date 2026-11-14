"""Shadow decisions: what an advise-only (or level-limited) agent proposed but may not run.

A proposal is a shadow decision when the ONLY thing that stopped it is the autonomy gate: every other rule would have
let it through (or escalated it), but the current level does not permit the action. It never executes, and it is kept
and shown to a person so they can say whether they agree. That agreement is the evidence the next promotion needs.
"""

from __future__ import annotations

from tessera_core.domain.actions import PolicyDecision, Verdict
from tessera_core.policy.engine import POLICY_ERROR

AUTONOMY_GATE_RULE_ID = "autonomy_level_gate"


def is_shadow_decision(decision: PolicyDecision) -> bool:
    """BLOCK, caused by the autonomy gate alone, and not a policy error (an error is never "the agent was fine")."""
    if decision.verdict is not Verdict.BLOCK or POLICY_ERROR in decision.reasons[:1]:
        return False
    return decision.rule_ids == (AUTONOMY_GATE_RULE_ID,)
