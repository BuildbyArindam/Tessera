"""The surface a stack scenario may use. The implementation lives in `tessera_worker.redteam_stack`.

Why a protocol: scenarios must drive the REAL API, worker and webhook endpoint against a real database, but the
architecture contracts (.importlinter) allow only the worker to import the vendor package. So the evalkit holds the
scenarios and this interface, the worker package holds the object that builds the system, and the command line joins
them by name (`--stack tessera_worker.redteam_stack:open_stack`). A scenario therefore cannot reach the vendor port
except through what this interface exposes, and it exposes the CALL LOG, not the port.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class Move:
    """One request that reached the vendor port and was accepted. This is what "executed" means."""

    kind: str  # transfer | conversion | card
    amount: str
    currency: str
    target: str  # the vendor-side beneficiary / card id, or the buy currency for a conversion

    def describe(self) -> str:
        return f"{self.kind} {self.amount} {self.currency} -> {self.target}"


@dataclass(frozen=True)
class Planned:
    proposal_id: str
    status: str  # APPROVED | PENDING_APPROVAL | BLOCKED
    action_type: str
    rules: tuple[str, ...]  # rule ids of the stored plan-phase decision (those that did not pass)


class Stack(Protocol):
    moves: list[Move]
    now: datetime
    last_outcome: str  # the planner's outcome for the last plan(): ok | invalid_output | ... | http_<status>

    # ---- driving the system ------------------------------------------------------------------------------
    def plan(self, *items: dict[str, Any], business: str = "B0") -> list[Planned]:
        """POST /plan/run with a model that proposes exactly `items`. Returns every proposal of the run."""

    def plan_concurrently(self, *batches: tuple[dict[str, Any], ...], business: str = "B0") -> list[list[Planned]]:
        """Several plan runs at the same moment, each on its own connection."""

    def approve(self, proposal_id: str) -> int: ...
    def reject(self, proposal_id: str) -> int: ...
    def execute(self, proposal_id: str) -> str:
        """One worker execution step (fresh policy, key stored, port call). Returns the worker's label."""

    def cycle(self) -> dict[str, int]:
        """The worker's execute step over every APPROVED proposal, then its reconcile step."""

    def execute_concurrently(self, proposal_ids: Sequence[str]) -> list[str]:
        """Run the execution step for each id on its own thread and connection, released together."""

    def webhook(
        self,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        secret: str | None = None,
        at: datetime | None = None,
        altered: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        """POST /webhooks/airwallex. `headers=None` signs `body` with `secret` (default: the real one) at `at`."""

    def api(self, method: str, path: str, *, json: Any = None, key: bool = True) -> tuple[int, Any]: ...

    # ---- reading what happened ---------------------------------------------------------------------------
    def status(self, proposal_id: str) -> str: ...
    def execution(self, proposal_id: str) -> dict[str, Any] | None:
        """{status, request_id, external_id, error} of the proposal's execution row, or None."""

    def executions_count(self) -> int: ...
    def audit_chain_ok(self) -> bool: ...
    def vendor_request_id(self, proposal_id: str) -> str | None: ...

    # ---- changing the world the way an operator or the outside world would -------------------------------
    def set_kill_switch(self, engaged: bool) -> None: ...
    def set_level(self, level: int) -> None:
        """The STORED autonomy level (what the dial says)."""

    def provider_claims_level(self, level: int | None) -> None:
        """Make the world provider assert an autonomy level in its context (production must ignore it)."""

    def change_world(self, change: Callable[[Any], Any] | None) -> None:
        """Edit every PolicyContext the provider builds from now on (`None` removes the edit)."""

    def revoke_beneficiary(self, business: str, beneficiary_id: str) -> None: ...
    def advance(self, seconds: int) -> None: ...
    def vendor_balance(self, currency: str) -> str: ...


OpenStack = Callable[[], AbstractContextManager[Stack]]
