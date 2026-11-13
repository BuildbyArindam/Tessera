"""The action lifecycle: where a proposal (and the execution behind it) may go next.

Pure and table-driven, so the API, the worker and the webhook path all enforce the same rules and a test can
enumerate every pair. An illegal move raises IllegalTransitionError; nothing here ever "repairs" one.

Proposal states (what the database stores in proposals.status):

    PROPOSED -> APPROVED            policy said APPROVE (PROPOSED is the instant before the first verdict;
              -> PENDING_APPROVAL   it is never stored: a proposal is inserted with its first verdict applied)
              -> BLOCKED
    PENDING_APPROVAL -> APPROVED | REJECTED          a human decided
    APPROVED -> SUBMITTED           the worker sent it to Airwallex
             -> PENDING_APPROVAL    policy re-run at execution time now says ESCALATE (nobody has approved it yet)
             -> BLOCKED             policy re-run at execution time now says BLOCK
             -> FAILED              Airwallex refused it outright, before anything was submitted
    SUBMITTED -> SETTLED | FAILED   a webhook (or the synchronous result) said how it ended
    REJECTED, BLOCKED, SETTLED, FAILED are terminal.

Execution states (executions.status), one execution per proposal:

    PENDING -> SUBMITTED | FAILED;  SUBMITTED -> SETTLED | FAILED;  SETTLED and FAILED are terminal.

A webhook can arrive late or out of order, so callers treat IllegalTransitionError from a webhook as "already
past that state, ignore it", never as a reason to move backwards.
"""

from enum import StrEnum
from typing import TypeVar


class ActionState(StrEnum):
    PROPOSED = "PROPOSED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    BLOCKED = "BLOCKED"
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class ExecutionState(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    SETTLED = "SETTLED"
    FAILED = "FAILED"


class IllegalTransitionError(ValueError):
    """The requested move is not in the lifecycle table (or one of the states is unknown)."""


A, E = ActionState, ExecutionState

ACTION_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    A.PROPOSED: frozenset({A.APPROVED, A.PENDING_APPROVAL, A.BLOCKED}),
    A.PENDING_APPROVAL: frozenset({A.APPROVED, A.REJECTED}),
    A.APPROVED: frozenset({A.SUBMITTED, A.PENDING_APPROVAL, A.BLOCKED, A.FAILED}),
    A.SUBMITTED: frozenset({A.SETTLED, A.FAILED}),
    A.REJECTED: frozenset(),
    A.BLOCKED: frozenset(),
    A.SETTLED: frozenset(),
    A.FAILED: frozenset(),
}

EXECUTION_TRANSITIONS: dict[ExecutionState, frozenset[ExecutionState]] = {
    E.PENDING: frozenset({E.SUBMITTED, E.FAILED}),
    E.SUBMITTED: frozenset({E.SETTLED, E.FAILED}),
    E.SETTLED: frozenset(),
    E.FAILED: frozenset(),
}

# States stored in proposals.status: every action state except the transient PROPOSED.
PERSISTED_ACTION_STATES: tuple[str, ...] = tuple(s.value for s in ActionState if s is not ActionState.PROPOSED)


S = TypeVar("S", bound=StrEnum)


def _coerce(enum: type[S], value: str | S, what: str) -> S:
    try:
        return enum(value)
    except ValueError:
        raise IllegalTransitionError(f"unknown {what} state {value!r}") from None


def transition_action(current: str | ActionState, target: str | ActionState) -> ActionState:
    """`target` if the move is legal, else IllegalTransitionError. Accepts the strings the database holds."""
    now, nxt = _coerce(ActionState, current, "action"), _coerce(ActionState, target, "action")
    if nxt not in ACTION_TRANSITIONS[now]:
        raise IllegalTransitionError(f"an action cannot move from {now.value} to {nxt.value}")
    return nxt


def transition_execution(current: str | ExecutionState, target: str | ExecutionState) -> ExecutionState:
    now, nxt = _coerce(ExecutionState, current, "execution"), _coerce(ExecutionState, target, "execution")
    if nxt not in EXECUTION_TRANSITIONS[now]:
        raise IllegalTransitionError(f"an execution cannot move from {now.value} to {nxt.value}")
    return nxt


def is_terminal(state: str | ActionState) -> bool:
    return not ACTION_TRANSITIONS[_coerce(ActionState, state, "action")]
