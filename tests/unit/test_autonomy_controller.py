"""AutonomyController: promotion eligibility, automatic demotion, and the evidence rules between them."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from autonomy_helpers import CONTROLLER, NOW, coverage, good_evidence, savings, shadow, state, violation
from tessera_core.autonomy import (
    TRIGGER_COVERAGE_COLLAPSE,
    TRIGGER_KILL_SWITCH,
    TRIGGER_VIOLATION,
    Assessment,
    AutonomyLevel,
    Evidence,
    SavingObservation,
    TransitionKind,
)

L0, L1, L2, L3 = AutonomyLevel.L0, AutonomyLevel.L1, AutonomyLevel.L2, AutonomyLevel.L3


def assess(level: AutonomyLevel, evidence: Evidence, *, kill: bool = False, days_ago: int = 10) -> Assessment:
    return CONTROLLER.assess(state(level, days_ago), evidence, NOW, kill_switch_engaged=kill)


def failed(a: Assessment) -> set[str]:
    return {c.name for c in a.criteria if not c.passed}


# ---- promotion is proposed, never applied --------------------------------------------------
def test_good_evidence_proposes_the_next_level_up() -> None:
    a = assess(L1, good_evidence())
    assert a.kind is TransitionKind.PROPOSE_PROMOTION and a.eligible
    assert (a.from_level, a.to_level) == (L1, L2)
    assert not failed(a) and a.triggers == ()


def test_the_assessment_is_data_it_does_not_change_the_state() -> None:
    s = state(L1)
    CONTROLLER.assess(s, good_evidence(), NOW, kill_switch_engaged=False)
    assert s.level is L1  # the controller has nothing to apply it with: the caller decides, and a person confirms


def test_promotion_moves_one_level_at_a_time() -> None:
    assert assess(L0, good_evidence()).to_level is L1
    assert assess(L2, good_evidence()).to_level is L3


def test_the_top_level_is_not_promoted_further() -> None:
    a = assess(L3, good_evidence())
    assert a.kind is TransitionKind.NONE and not a.eligible and a.to_level is L3


# ---- insufficient evidence stays put -------------------------------------------------------
@pytest.mark.parametrize(
    ("evidence", "criterion"),
    [
        (Evidence(shadow=shadow(n=29, agreed=29), savings=savings(), coverage=coverage()), "shadow_agreement"),
        (Evidence(shadow=shadow(), savings=savings(n=19), coverage=coverage()), "saving_lower_bound"),
        (
            Evidence(shadow=shadow(), savings=savings(), coverage=coverage(n=29, covered=24)),
            "coverage_within_tolerance",
        ),
        (Evidence(), "shadow_agreement"),
    ],
)
def test_too_little_evidence_is_never_a_pass(evidence: Evidence, criterion: str) -> None:
    a = assess(L1, evidence)
    assert a.kind is TransitionKind.NONE and not a.eligible and criterion in failed(a)
    assert any("n >= " in c.required for c in a.criteria if c.name == criterion)


# ---- each criterion on its own -------------------------------------------------------------
def test_agreement_below_the_threshold_blocks_promotion() -> None:
    a = assess(L1, Evidence(shadow=shadow(40, agreed=31), savings=savings(), coverage=coverage()))  # 0.775 < 0.80
    assert failed(a) == {"shadow_agreement"} and a.kind is TransitionKind.NONE


def test_agreement_exactly_at_the_threshold_passes() -> None:
    a = assess(L1, Evidence(shadow=shadow(40, agreed=32), savings=savings(), coverage=coverage()))  # 0.80
    assert a.kind is TransitionKind.PROPOSE_PROMOTION


def test_a_saving_whose_lower_bound_is_not_above_zero_blocks_promotion() -> None:
    noisy = savings(25, low="-30.00", high="31.00")  # a positive mean that the data cannot tell from zero
    a = assess(L1, Evidence(shadow=shadow(), savings=noisy, coverage=coverage()))
    assert failed(a) == {"saving_lower_bound"}


def test_a_negative_saving_blocks_promotion() -> None:
    a = assess(L1, Evidence(shadow=shadow(), savings=savings(25, low="-5.00", high="-1.00"), coverage=coverage()))
    assert failed(a) == {"saving_lower_bound"}


def test_coverage_too_wide_is_also_outside_tolerance() -> None:
    a = assess(L1, Evidence(shadow=shadow(), savings=savings(), coverage=coverage(40, covered=40)))  # 1.00 vs 0.80
    assert failed(a) == {"coverage_within_tolerance"} and a.kind is TransitionKind.NONE


# ---- automatic demotion --------------------------------------------------------------------
def test_one_violation_demotes_immediately_to_l0() -> None:
    a = assess(L2, Evidence(violations=(violation(),), shadow=shadow(), savings=savings(), coverage=coverage()))
    assert a.kind is TransitionKind.DEMOTE and a.to_level is L0 and a.triggers == (TRIGGER_VIOLATION,)


def test_a_violation_blocks_promotion_even_with_everything_else_perfect() -> None:
    old = violation(NOW - timedelta(days=5))  # before this level began: no new demotion, but it is inside the window
    a = assess(L1, Evidence(violations=(old,), shadow=shadow(), savings=savings(), coverage=coverage()), days_ago=2)
    assert a.kind is TransitionKind.NONE and failed(a) == {"zero_violations"}


def test_a_violation_older_than_the_window_no_longer_blocks_promotion() -> None:
    old = violation(NOW - timedelta(days=45))
    a = assess(L1, Evidence(violations=(old,), shadow=shadow(), savings=savings(), coverage=coverage()))
    assert a.kind is TransitionKind.PROPOSE_PROMOTION


def test_a_coverage_collapse_demotes_one_level() -> None:
    a = assess(L2, Evidence(coverage=coverage(40, covered=20)))  # 0.50 against 0.80, margin 0.10
    assert a.kind is TransitionKind.DEMOTE and a.to_level is L1 and a.triggers == (TRIGGER_COVERAGE_COLLAPSE,)


def test_coverage_inside_the_margin_does_not_demote_but_does_not_promote() -> None:
    a = assess(L2, Evidence(shadow=shadow(), savings=savings(), coverage=coverage(40, covered=29)))  # 0.725
    assert a.kind is TransitionKind.NONE and a.triggers == () and failed(a) == {"coverage_within_tolerance"}


def test_a_collapse_needs_enough_observations_to_be_an_alarm() -> None:
    a = assess(L2, Evidence(coverage=coverage(14, covered=0)))  # 14 < the minimum of 15
    assert a.kind is TransitionKind.NONE and a.triggers == ()


def test_the_drift_alarm_watches_the_short_window_only() -> None:
    from autonomy_helpers import CONFIG
    from tessera_core.autonomy import CoverageObservation

    stale = tuple(
        CoverageObservation(
            at=NOW - timedelta(days=CONFIG.window.drift_days + 3, minutes=i), covered=False, target=Decimal("0.80")
        )
        for i in range(40)
    )
    assert assess(L2, Evidence(coverage=stale), days_ago=25).triggers == ()


def test_the_kill_switch_demotes_to_l0_from_any_level() -> None:
    for level in (L1, L2, L3):
        a = assess(level, good_evidence(), kill=True)
        assert a.kind is TransitionKind.DEMOTE and a.to_level is L0 and a.triggers == (TRIGGER_KILL_SWITCH,)


def test_the_kill_switch_at_l0_changes_nothing_and_blocks_promotion() -> None:
    a = assess(L0, good_evidence(), kill=True)
    assert a.kind is TransitionKind.NONE and a.to_level is L0 and not a.eligible


def test_the_deepest_trigger_wins() -> None:
    a = assess(L3, Evidence(violations=(violation(),), coverage=coverage(40, covered=20)))
    assert a.to_level is L0 and set(a.triggers) == {TRIGGER_VIOLATION, TRIGGER_COVERAGE_COLLAPSE}


# ---- which evidence counts -----------------------------------------------------------------
def test_evidence_from_before_the_current_level_began_does_not_count() -> None:
    a = assess(L1, good_evidence(), days_ago=0)  # the level began now; all the evidence is older
    assert a.kind is TransitionKind.NONE and {"shadow_agreement", "saving_lower_bound"} <= failed(a)


def test_a_demotion_does_not_fire_again_on_the_evidence_that_caused_it() -> None:
    evidence = Evidence(coverage=coverage(40, covered=20))
    first = assess(L2, evidence)
    assert first.kind is TransitionKind.DEMOTE
    after = CONTROLLER.assess(state(L1, days_ago=0), evidence, NOW, kill_switch_engaged=False)
    assert after.kind is TransitionKind.NONE and after.triggers == ()  # the collapse predates the new level


def test_future_dated_good_evidence_does_not_count() -> None:
    future = tuple(
        SavingObservation(at=NOW + timedelta(hours=i + 1), saving_per_10k=Decimal("12.00")) for i in range(30)
    )
    a = assess(L1, Evidence(shadow=shadow(), savings=future, coverage=coverage()))
    assert "saving_lower_bound" in failed(a)


def test_a_future_dated_violation_still_counts_against_the_agent() -> None:
    a = assess(L2, Evidence(violations=(violation(NOW + timedelta(hours=1)),)))
    assert a.kind is TransitionKind.DEMOTE


# ---- repeatability -------------------------------------------------------------------------
def test_the_same_inputs_give_the_same_assessment_and_name_the_config() -> None:
    first, second = assess(L1, good_evidence()), assess(L1, good_evidence())
    assert first == second and first.config_hash == CONTROLLER.config.hash and len(first.config_hash) == 64
