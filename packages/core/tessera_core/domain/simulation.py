"""SimulationSummary: the slice of a simulation report that policy is allowed to see.

It lives in `domain` (not in `simulate`) so that `policy` can read it without importing the
simulator: policy stays a pure rule engine, and the simulator stays free to change how it works.

All money fields are in the SELL currency, the currency the business holds and gives up. The
simulator's report says exactly how each number is defined; this type only carries the three
numbers a rule needs, plus the identity of the proposal they were computed for.
"""

from pydantic import Field, model_validator

from tessera_core.domain.actions import ActionType
from tessera_core.domain.base import DomainModel
from tessera_core.domain.money import Money


class SimulationSummary(DomainModel):
    # Which proposal this was computed for. The gate refuses a summary that belongs to another
    # proposal, so a stale or swapped report cannot vouch for a different action.
    proposal_id: str = Field(min_length=1)
    action_type: ActionType
    # The amount that was simulated, exactly as the proposal states it (sell units for a CONVERT or
    # HOLD, payout units for a PAY_SUPPLIER). The gate compares it with the proposal's own amount, so
    # a small simulation cannot vouch for a larger action that reuses the same id.
    amount: Money
    # 5th-percentile shortfall versus the comparator, never negative (0 means "no loss at p05").
    worst_case_loss: Money
    expected_saving: Money
    n_paths: int = Field(ge=1)

    @model_validator(mode="after")
    def _consistent(self) -> "SimulationSummary":
        if not self.amount.is_positive():
            raise ValueError("amount must be strictly positive")
        if self.worst_case_loss.is_negative():
            raise ValueError("worst_case_loss must not be negative")
        if self.expected_saving.currency != self.worst_case_loss.currency:
            raise ValueError("expected_saving and worst_case_loss must be in the same currency")
        return self
