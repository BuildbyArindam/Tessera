"""Proposals (what the LLM may emit) and policy decisions (what the engine returns)."""

from enum import StrEnum

from pydantic import AwareDatetime, Field, JsonValue, model_validator

from tessera_core.domain.base import DomainModel


class ActionType(StrEnum):
    CONVERT = "CONVERT"
    HOLD = "HOLD"
    PAY_SUPPLIER = "PAY_SUPPLIER"
    TOPUP_CARD = "TOPUP_CARD"
    FLAG_INVOICE = "FLAG_INVOICE"


class Proposal(DomainModel):
    """A request to act. It carries no authority: only the policy engine can approve it.

    `params` is deliberately loose today; Day 7's rules validate per-action fields and fail
    closed on anything malformed. Money amounts inside params must be strings, not floats.
    """

    id: str = Field(min_length=1)
    action_type: ActionType
    params: dict[str, JsonValue]
    rationale: str = Field(max_length=4000)
    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)
    created_at: AwareDatetime


class Verdict(StrEnum):
    APPROVE = "APPROVE"
    ESCALATE = "ESCALATE"
    BLOCK = "BLOCK"


class PolicyDecision(DomainModel):
    """The engine's answer. `rule_ids` lists the rules that produced a non-PASS result.

    `ruleset_hash` pins the decision to the exact rules in force. It is optional only so that
    hand-built decisions in tests stay valid; `policy.evaluate` always sets it.
    """

    verdict: Verdict
    reasons: tuple[str, ...] = ()
    rule_ids: tuple[str, ...] = ()
    ruleset_hash: str | None = None

    @model_validator(mode="after")
    def _non_approve_needs_reason(self) -> "PolicyDecision":
        if self.verdict is not Verdict.APPROVE and not self.reasons:
            raise ValueError("an ESCALATE or BLOCK decision must state at least one reason")
        return self
