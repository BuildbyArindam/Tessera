"""Day 13: the action lifecycle table. Every pair of states is checked, so a change to the table must change a test."""

from __future__ import annotations

import itertools

import pytest
from tessera_core.domain import ActionState, ExecutionState, IllegalTransitionError
from tessera_core.domain.lifecycle import (
    ACTION_TRANSITIONS,
    EXECUTION_TRANSITIONS,
    PERSISTED_ACTION_STATES,
    is_terminal,
    transition_action,
    transition_execution,
)

A, E = ActionState, ExecutionState

LEGAL_ACTION = {
    (A.PROPOSED, A.APPROVED), (A.PROPOSED, A.PENDING_APPROVAL), (A.PROPOSED, A.BLOCKED),
    (A.PENDING_APPROVAL, A.APPROVED), (A.PENDING_APPROVAL, A.REJECTED),
    (A.APPROVED, A.SUBMITTED), (A.APPROVED, A.PENDING_APPROVAL), (A.APPROVED, A.BLOCKED), (A.APPROVED, A.FAILED),
    (A.SUBMITTED, A.SETTLED), (A.SUBMITTED, A.FAILED),
}  # fmt: skip
LEGAL_EXECUTION = {(E.PENDING, E.SUBMITTED), (E.PENDING, E.FAILED), (E.SUBMITTED, E.SETTLED), (E.SUBMITTED, E.FAILED)}


@pytest.mark.parametrize(("now", "nxt"), list(itertools.product(A, A)))
def test_every_action_pair_is_legal_exactly_when_the_table_says_so(now: A, nxt: A) -> None:
    if (now, nxt) in LEGAL_ACTION:
        assert transition_action(now, nxt) is nxt
    else:
        with pytest.raises(IllegalTransitionError):
            transition_action(now, nxt)


@pytest.mark.parametrize(("now", "nxt"), list(itertools.product(E, E)))
def test_every_execution_pair_is_legal_exactly_when_the_table_says_so(now: E, nxt: E) -> None:
    if (now, nxt) in LEGAL_EXECUTION:
        assert transition_execution(now, nxt) is nxt
    else:
        with pytest.raises(IllegalTransitionError):
            transition_execution(now, nxt)


def test_the_happy_path_is_the_one_in_the_brief() -> None:
    state = A.PROPOSED
    for step in (A.APPROVED, A.SUBMITTED, A.SETTLED):
        state = transition_action(state, step)
    assert state is A.SETTLED


def test_terminal_states_have_no_way_out() -> None:
    for state in (A.REJECTED, A.BLOCKED, A.SETTLED, A.FAILED):
        assert is_terminal(state)
        assert ACTION_TRANSITIONS[state] == frozenset()
    for state in (E.SETTLED, E.FAILED):
        assert EXECUTION_TRANSITIONS[state] == frozenset()
    assert not is_terminal(A.APPROVED)


def test_a_late_processing_event_cannot_move_a_settled_action_back() -> None:
    with pytest.raises(IllegalTransitionError):
        transition_action(A.SETTLED, A.SUBMITTED)
    with pytest.raises(IllegalTransitionError):
        transition_execution(E.SETTLED, E.SUBMITTED)


def test_nothing_reaches_submitted_without_being_approved_first() -> None:
    sources = {s for s, targets in ACTION_TRANSITIONS.items() if A.SUBMITTED in targets}
    assert sources == {A.APPROVED}


def test_strings_from_the_database_are_accepted_and_junk_is_refused() -> None:
    assert transition_action("APPROVED", "SUBMITTED") is A.SUBMITTED
    for junk in ("approved", "DONE", "", "APPROVED "):
        with pytest.raises(IllegalTransitionError):
            transition_action(junk, "SUBMITTED")
        with pytest.raises(IllegalTransitionError):
            transition_action("APPROVED", junk)


def test_the_persisted_states_are_every_state_except_the_transient_proposed() -> None:
    assert set(PERSISTED_ACTION_STATES) == {s.value for s in A} - {"PROPOSED"}
