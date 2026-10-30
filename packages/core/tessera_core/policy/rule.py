"""Rule base class and per-rule result type.

A rule is a small deterministic class. It is built once from validated YAML config, then asked
about one proposal at a time. Rules never raise to signal a normal BLOCK; they return one. If a
rule does raise, the engine treats that as a policy error and fails closed.
"""

from abc import ABC, abstractmethod
from enum import StrEnum
from typing import ClassVar, Generic, TypeVar

from pydantic import Field

from tessera_core.domain.actions import Proposal
from tessera_core.domain.base import DomainModel
from tessera_core.policy.context import PolicyContext


class RuleOutcome(StrEnum):
    PASS = "PASS"
    ESCALATE = "ESCALATE"
    BLOCK = "BLOCK"


class RuleResult(DomainModel):
    rule_id: str = Field(min_length=1)
    outcome: RuleOutcome
    reason: str = ""

    def model_post_init(self, __context: object) -> None:
        if self.outcome is not RuleOutcome.PASS and not self.reason:
            raise ValueError("an ESCALATE or BLOCK rule result must state a reason")


class RuleConfig(DomainModel):
    """Base for a rule's YAML `params`. Unknown keys are errors (inherited from DomainModel)."""


ConfigT = TypeVar("ConfigT", bound=RuleConfig)


class Rule(ABC, Generic[ConfigT]):
    # Registered name used in YAML `type:`.
    type_name: ClassVar[str]
    # Bump when the rule's logic changes; it feeds the ruleset hash, so old decisions stay traceable.
    type_version: ClassVar[int] = 1
    config_model: ClassVar[type[RuleConfig]]
    # Names of PolicyContext fields this rule cannot run without.
    required_context: ClassVar[tuple[str, ...]] = ()

    def __init__(self, rule_id: str, config: ConfigT) -> None:
        self.rule_id = rule_id
        self.config = config

    def required_context_for(self, proposal: Proposal) -> tuple[str, ...]:
        """Context fields needed to judge THIS proposal; the engine blocks if any is None.

        Defaults to `required_context`. A rule that only applies to some action types overrides
        this to ask for nothing when it does not apply, so a HOLD is not blocked for lacking,
        say, an FX quote. For the actions a rule does apply to it must still list everything.
        """
        return self.required_context

    def result(self, outcome: RuleOutcome, reason: str = "") -> RuleResult:
        return RuleResult(rule_id=self.rule_id, outcome=outcome, reason=reason)

    @abstractmethod
    def evaluate(self, proposal: Proposal, context: PolicyContext) -> RuleResult:
        """Return PASS, ESCALATE or BLOCK. Must be a pure function of its arguments."""
