"""Day 13: what a fresh policy verdict means for an already-approved proposal (pure)."""

from __future__ import annotations

from tessera_core.domain import PolicyDecision, Verdict
from tessera_worker.gate import Covered, effective_verdict

H = "hash-a"


def decision(verdict: Verdict, rules: tuple[str, ...] = (), h: str | None = H) -> PolicyDecision:
    return PolicyDecision(
        verdict=verdict,
        reasons=tuple(f"{r}: x" for r in rules) or (("n/a",) if verdict is not Verdict.APPROVE else ()),
        rule_ids=rules,
        ruleset_hash=h,
    )


def test_approve_and_block_pass_straight_through() -> None:
    assert effective_verdict(decision(Verdict.APPROVE), None) is Verdict.APPROVE
    assert effective_verdict(decision(Verdict.BLOCK, ("r1",)), None) is Verdict.BLOCK


def test_a_block_beats_any_human_approval() -> None:
    covered = Covered(frozenset({"r1"}), H)
    assert effective_verdict(decision(Verdict.BLOCK, ("r1",)), covered) is Verdict.BLOCK


def test_an_escalate_without_a_human_approval_goes_back_to_a_human() -> None:
    assert effective_verdict(decision(Verdict.ESCALATE, ("r1",)), None) is Verdict.ESCALATE


def test_an_escalate_the_human_already_saw_may_proceed() -> None:
    covered = Covered(frozenset({"r1", "r2"}), H)
    assert effective_verdict(decision(Verdict.ESCALATE, ("r1",)), covered) is Verdict.APPROVE
    assert effective_verdict(decision(Verdict.ESCALATE, ("r1", "r2")), covered) is Verdict.APPROVE


def test_a_new_rule_the_human_did_not_see_escalates_again() -> None:
    covered = Covered(frozenset({"r1"}), H)
    assert effective_verdict(decision(Verdict.ESCALATE, ("r1", "r9")), covered) is Verdict.ESCALATE


def test_a_different_ruleset_escalates_again() -> None:
    covered = Covered(frozenset({"r1"}), "hash-old")
    assert effective_verdict(decision(Verdict.ESCALATE, ("r1",)), covered) is Verdict.ESCALATE


def test_an_unknown_ruleset_hash_never_counts_as_covered() -> None:
    assert (
        effective_verdict(decision(Verdict.ESCALATE, ("r1",), h=None), Covered(frozenset({"r1"}), None))
        is Verdict.ESCALATE
    )
    assert (
        effective_verdict(decision(Verdict.ESCALATE, ("r1",), h=None), Covered(frozenset({"r1"}), H))
        is Verdict.ESCALATE
    )
