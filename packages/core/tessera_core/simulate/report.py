"""The two result types: a Monte-Carlo SimulationReport and a one-path ReplayReport.

Every money figure is in the SELL currency (`sell_currency`) and `saving` means "proposal minus
comparator", so positive is good and negative is a loss. How each is computed is in scenario.py.
Only `SimulationReport` can become a `SimulationSummary` for policy; a `ReplayReport` reads the
realised future on purpose (for backtests) and must never reach a live decision, so it has no
`summary()`.
"""

from datetime import date

from pydantic import Field

from tessera_core.domain.actions import ActionType
from tessera_core.domain.base import DomainModel
from tessera_core.domain.currency import Currency
from tessera_core.domain.money import Money
from tessera_core.domain.simulation import SimulationSummary


class SimulationReport(DomainModel):
    proposal_id: str
    action_type: ActionType
    comparator: str  # "convert_on_due_date" (CONVERT, PAY_SUPPLIER) or "convert_now" (HOLD)
    pair: str | None  # "SELL/BUY"; None when the payout needs no conversion
    amount: Money  # the amount simulated, as the proposal wrote it
    sell_currency: Currency
    as_of: date
    due_date: date
    horizon_days: int  # calendar days from as_of to due_date
    steps: int  # business days simulated
    n_paths: int
    seed: int
    block_length: int
    n_returns: int  # historical daily returns available on or before as_of
    spot: float | None  # mid rate at as_of, buy units per 1 sell unit
    expected_saving: Money
    saving_std: Money
    saving_p05: Money  # 5th percentile of the saving; negative means the bad tail loses money
    saving_p50: Money
    worst_case_loss: Money  # max(0, -saving_p05), so never negative
    prob_beats_baseline: float = Field(ge=0.0, le=1.0)  # P(saving > 0)
    spread_cost: Money  # what the proposal's conversion pays in spread
    fee_cost: Money  # what it pays in fixed fee
    notes: tuple[str, ...] = ()

    def summary(self) -> SimulationSummary:
        """The part policy may see. Carries the proposal id so it cannot vouch for another action."""
        return SimulationSummary(
            proposal_id=self.proposal_id,
            action_type=self.action_type,
            amount=self.amount,
            worst_case_loss=self.worst_case_loss,
            expected_saving=self.expected_saving,
            n_paths=self.n_paths,
        )


class ReplayReport(DomainModel):
    """One proposal run against the rates that actually happened (Day 17 backtests only)."""

    proposal_id: str
    action_type: ActionType
    comparator: str
    pair: str | None
    amount: Money
    sell_currency: Currency
    as_of: date
    due_date: date
    spot_day: date | None  # observation used as the "now" rate
    due_day: date | None  # observation used as the realised due-date rate
    spot: float | None
    spot_at_due: float | None
    realised_saving: Money
    spread_cost: Money
    fee_cost: Money
    notes: tuple[str, ...] = ()
