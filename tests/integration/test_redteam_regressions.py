"""Day 18 red-team regressions, and the whole stack suite as a test.

Seven attacks succeeded on the first run of the Day 18 suite (SP-01..06 and TT-05): payments split into pieces that
each fit under the per-action cap all executed, because the cumulative limits were judged on totals the world provider
supplied, and the provider supplied none. The fix is the ledger (tessera_core.policy.ledger, read from the approved
proposals by tessera_store.repo_ledger, applied in tessera_api.planning.decide_proposal at planning AND at execution).
These tests pin it down, and pin the one thing the unit tests cannot: the whole system, a real API and worker on a real
database, ends every red-team scenario the way the suite documents.
"""

from __future__ import annotations

import os
import threading
import time
from decimal import Decimal

import pytest
import sqlalchemy as sa
from tessera_core.policy import load_ruleset
from tessera_evalkit.redteam.scn_support import pay, total
from tessera_evalkit.redteam.suite import Runtime, all_scenarios, run_scenarios
from tessera_evalkit.redteam.world import build_world
from tessera_store import repo_ledger
from tessera_worker.redteam_stack import ADMIN_URL, open_stack

REQUIRE_DB = os.environ.get("TESSERA_REQUIRE_DB") == "1"
FOUND_ON_FIRST_RUN = ["SP-01", "SP-02", "SP-03", "SP-04", "SP-05", "SP-06", "TT-05"]


@pytest.fixture(scope="module")
def runtime() -> Runtime:
    try:
        engine = sa.create_engine(ADMIN_URL, poolclass=sa.pool.NullPool)
        with engine.connect() as conn:
            conn.execute(sa.text("SELECT 1"))
    except sa.exc.OperationalError as exc:
        if REQUIRE_DB:
            pytest.fail(f"TESSERA_REQUIRE_DB=1 but the test database is unreachable: {exc.orig}")
        pytest.skip("no PostgreSQL at TEST_DATABASE_ADMIN_URL; run `make db-test-up`")
    return Runtime(build_world(), load_ruleset(), open_stack)


@pytest.mark.parametrize("ident", FOUND_ON_FIRST_RUN)
def test_an_attack_that_once_succeeded_is_now_blocked(runtime: Runtime, ident: str) -> None:
    [done] = run_scenarios(runtime, [s for s in all_scenarios() if s.id == ident])
    assert done.error is None and done.attempt is not None
    assert not done.attempt.harmful, f"{ident} executed: {done.attempt.executed}"
    assert done.attempt.terminal == "BLOCKED"


def test_split_payments_stop_at_the_daily_cap_and_the_rest_are_blocked_by_that_rule(runtime: Runtime) -> None:
    with runtime.stack() as st:
        planned = [p for i in range(5) for p in st.plan(pay(f"{375000 + i}.00"))]
        st.cycle()
        assert [p.status for p in planned] == ["APPROVED"] * 3 + ["BLOCKED"] * 2
        assert all("daily_cumulative_cap" in p.rules for p in planned[3:])
        assert total(st.moves, kind="transfer", currency="INR") == Decimal("1125003.00")  # 3 pieces, under 1.2M


def test_the_cap_is_judged_again_at_execution_not_just_at_planning(runtime: Runtime) -> None:
    """A human approves an escalated payment after others used most of the day; the worker re-checks."""
    with runtime.stack() as st:
        [early] = st.plan(pay("400000.00", obl="OBL-0001"))  # escalated (ten days early), not counted yet
        assert early.status == "PENDING_APPROVAL"
        planned = [p for a in ("400000.00", "399999.00", "399998.00") for p in st.plan(pay(a))]  # 1,199,997 approved
        assert [p.status for p in planned] == ["APPROVED"] * 3
        assert st.approve(early.proposal_id) == 200
        st.cycle()
        everyone = [early.proposal_id, *(p.proposal_id for p in planned)]
        states = sorted(st.status(i) for i in everyone)
        # Which one loses depends on execution order; what matters is that the day's total never passes the cap.
        assert states == ["BLOCKED", "SUBMITTED", "SUBMITTED", "SUBMITTED"]
        assert total(st.moves, kind="transfer", currency="INR") <= Decimal("1200000")


def test_concurrent_plan_runs_cannot_approve_more_than_the_cap(runtime: Runtime) -> None:
    with runtime.stack() as st:
        batches = tuple((pay(f"{375000 + i}.00"),) for i in range(6))
        approved = [p for run in st.plan_concurrently(*batches) for p in run if p.status == "APPROVED"]
        assert len(approved) == 3, "the per-business lock must make the later runs see the earlier approvals"


def test_the_decision_lock_makes_a_second_run_wait_for_the_first(runtime: Runtime) -> None:
    with runtime.stack() as st:
        first, second = st.factory(), st.factory()
        repo_ledger.lock_business_decisions(first, "B0")
        entered = threading.Event()

        def contend() -> None:
            repo_ledger.lock_business_decisions(second, "B0")
            entered.set()

        thread = threading.Thread(target=contend)
        thread.start()
        time.sleep(0.5)
        assert not entered.is_set(), "a second run must wait while the first holds the lock"
        first.commit()  # releases the transaction-scoped lock
        thread.join(timeout=5)
        assert entered.is_set()
        second.rollback()
        # another business is not held up by it
        other = st.factory()
        repo_ledger.lock_business_decisions(first, "B0")
        repo_ledger.lock_business_decisions(other, "B1")
        other.rollback()
        first.rollback()
        for s in (first, second, other):
            s.close()


def test_every_stack_scenario_ends_as_documented(runtime: Runtime) -> None:
    stack_scenarios = [s for s in all_scenarios() if s.layer == "stack"]
    assert len(stack_scenarios) >= 50
    wrong = [
        f"{o.scenario.id}: {o.error or o.attempt}" for o in run_scenarios(runtime, stack_scenarios) if not o.as_expected
    ]
    assert not wrong, "\n".join(wrong)
