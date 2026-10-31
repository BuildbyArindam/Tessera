"""Day 7 property-based tests (Hypothesis): the four invariants the policy engine must never break.

1. Fail closed: a malformed proposal (or any non-proposal object) is never approved.
2. Monotonic: raising an amount never makes a verdict more permissive (BLOCK never becomes APPROVE).
3. Order independent: the order the rules are listed in never changes the decision.
4. A single BLOCK cannot be outvoted: if any one rule would BLOCK, the engine BLOCKs.
"""

import random
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from policy_helpers import DEFAULT_RULES_DIR, full_context, make_proposal, sim_for
from policy_strategies import CODES, MONEY_MOVING, SIM_MODES, contexts, proposals, well_formed, with_simulation
from tessera_core.domain import ActionType, Verdict
from tessera_core.policy import (
    POLICY_ERROR,
    PolicyContext,
    Ruleset,
    build_ruleset,
    evaluate,
    load_ruleset,
    ruleset_from_yaml_texts,
)
from tessera_core.policy.loader import parse_rule_yaml

PROP = settings(
    max_examples=500, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large]
)
RANK = {Verdict.APPROVE: 0, Verdict.ESCALATE: 1, Verdict.BLOCK: 2}
SHIPPED = {p.name: p.read_text(encoding="utf-8") for p in DEFAULT_RULES_DIR.glob("*.yaml")}
FULL = load_ruleset()
# Test-only rule types (registered by policy_helpers) mixed in so errors and escalations are exercised too.
EXTRAS = {f"{t}.yaml": f"id: {t}\ntype: {t}\nparams: {{}}\n" for t in ("test_escalate", "test_block", "test_boom")}
POOL = {**SHIPPED, **EXTRAS}
OPTIONAL = sorted(set(POOL) - {"kill_switch.yaml"})


def _ruleset(*files: str) -> Ruleset:
    return ruleset_from_yaml_texts({"kill_switch.yaml": POOL["kill_switch.yaml"], **{f: POOL[f] for f in files}})


ISOLATED = {f: _ruleset(f) for f in OPTIONAL}  # kill_switch plus exactly one other rule


# ---- 1. fail closed under any malformed proposal ------------------------------------------
@PROP
@given(p=proposals(MONEY_MOVING), ctx=st.one_of(st.just(full_context()), contexts()))
def test_malformed_money_proposals_are_always_blocked(p: Any, ctx: PolicyContext) -> None:
    d = evaluate(p, ctx, FULL)
    if not well_formed(p.action_type, p.params):
        assert d.verdict is Verdict.BLOCK, (p.params, d)
    assert (d.verdict is Verdict.APPROVE) == (d.reasons == ())


@PROP
@given(
    junk=st.one_of(
        st.none(),
        st.integers(),
        st.text(),
        st.binary(),
        st.lists(st.integers()),
        st.dictionaries(st.text(), st.integers()),
        st.just(object()),
        st.just(ActionType.HOLD),
    ),  # fmt: skip
    where=st.sampled_from(["proposal", "context", "ruleset"]),
)
def test_any_non_domain_object_is_blocked_not_raised(junk: object, where: str) -> None:
    args: dict[str, Any] = {"proposal": make_proposal(ActionType.HOLD), "context": full_context(), "ruleset": FULL}
    args[where] = junk
    d = evaluate(args["proposal"], args["context"], args["ruleset"])
    assert d.verdict is Verdict.BLOCK and d.reasons[0] == POLICY_ERROR


# ---- 2. monotonicity ----------------------------------------------------------------------
# Hypothesis favours small integers, so plain ranges never reach the large magnitudes where overflow or
# limit-edge bugs live. Spread amounts evenly over magnitudes (cents, 1 .. ~1e17) and add values hugging
# each shipped limit (card 200.00, 40% of 10,000, per-action 5,000.00, daily 15,000.00) and 2**31 / 2**53.
_LIMITS = [20_000, 60_000, 400_000, 500_000, 1_500_000, 2_000_000, 2**31, 2**53]
CENTS = st.one_of(
    st.builds(lambda m, e: m * 10**e, st.integers(1, 99), st.integers(0, 15)),
    st.builds(lambda t, d: max(1, t + d), st.sampled_from(_LIMITS), st.integers(-2, 2)),
    st.integers(1, 30_000),
)


def _amount(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


@PROP
@given(
    kind=st.sampled_from(MONEY_MOVING),
    amounts=st.lists(CENTS, min_size=2, max_size=8),
    currency=st.sampled_from(CODES),
    beneficiary=st.sampled_from(["sup_old", "sup_new"]),
    ctx=st.one_of(contexts(complete=True), contexts(complete=True), contexts()),  # mostly complete: APPROVE is common
    sim=st.tuples(st.sampled_from(SIM_MODES), st.integers(0, 200_000)),
)
def test_raising_an_amount_never_loosens_the_verdict(
    kind: ActionType, amounts: list[int], currency: str, beneficiary: str, ctx: PolicyContext, sim: tuple[str, int]
) -> None:
    """Along any increasing run of amounts the verdict never gets more permissive (BLOCK stays BLOCK)."""
    extra = {
        "to_currency": "EUR" if currency != "EUR" else "USD",
        "beneficiary_id": beneficiary,
        "vendor_id": "workspace_suite",
    }

    # The simulation does not depend on the amount, so it stays fixed along the run.
    def decide(a: int) -> Verdict:
        p = make_proposal(kind, _amount(a), currency, **extra)
        return evaluate(p, with_simulation(ctx, p, *sim), FULL).verdict

    run = [(a, decide(a)) for a in sorted(amounts)]
    for (lo, before), (hi, after) in zip(run, run[1:], strict=False):
        assert RANK[after] >= RANK[before], (lo, hi, before, after)
        if before is Verdict.BLOCK:
            assert after is Verdict.BLOCK, (lo, hi)


# ---- 3. order independence ----------------------------------------------------------------
@PROP
@given(
    p=proposals(),
    ctx=contexts(),
    extras=st.sets(st.sampled_from(sorted(EXTRAS))),
    seed=st.integers(0, 2**32),
    sim=st.tuples(st.sampled_from(SIM_MODES), st.integers(0, 200_000)),
)
def test_shuffling_the_rule_list_never_changes_the_decision(
    p: Any, ctx: PolicyContext, extras: set[str], seed: int, sim: tuple[str, int]
) -> None:
    ctx = with_simulation(ctx, p, *sim)
    base = _ruleset(*sorted(set(SHIPPED) - {"kill_switch.yaml"} | extras))
    rules = list(base.rules)
    random.Random(seed).shuffle(rules)
    shuffled = Ruleset(rules=tuple(rules), hash=base.hash)  # bypasses the loader's own sorting
    assert evaluate(p, ctx, shuffled) == evaluate(p, ctx, base)

    specs = [parse_rule_yaml(text, name) for name, text in {**SHIPPED, **{k: EXTRAS[k] for k in extras}}.items()]
    random.Random(seed + 1).shuffle(specs)
    assert build_ruleset(specs).hash == base.hash


# ---- 4. a single BLOCK is never outvoted --------------------------------------------------
@PROP
@given(
    p=proposals(),
    ctx=contexts(),
    members=st.sets(st.sampled_from(OPTIONAL), min_size=1),
    sim=st.tuples(st.sampled_from(SIM_MODES), st.integers(0, 200_000)),
)
def test_no_approve_when_any_single_rule_would_block(
    p: Any, ctx: PolicyContext, members: set[str], sim: tuple[str, int]
) -> None:
    ctx = with_simulation(ctx, p, *sim)
    full = evaluate(p, ctx, _ruleset(*members))
    alone = {f: evaluate(p, ctx, ISOLATED[f]).verdict for f in members}
    if any(v is Verdict.BLOCK for v in alone.values()):
        assert full.verdict is Verdict.BLOCK, (alone, full)
    # The exact combination law: the verdict is the strictest verdict any member gives on its own.
    assert RANK[full.verdict] == max(RANK[v] for v in alone.values()), (alone, full)


@PROP
@given(p=proposals(), ctx=contexts())
def test_kill_switch_alone_is_enough_to_block(p: Any, ctx: PolicyContext) -> None:
    engaged = ctx.model_copy(update={"kill_switch_engaged": True})
    assert evaluate(p, engaged, FULL).verdict is Verdict.BLOCK


def test_the_approve_path_is_actually_reached() -> None:
    """Guard against vacuous properties: with a clean context some generated shapes must APPROVE."""
    p = make_proposal(ActionType.CONVERT, "10.00", "USD", to_currency="EUR")
    assert evaluate(p, full_context(simulation=sim_for(p)), FULL).verdict is Verdict.APPROVE
