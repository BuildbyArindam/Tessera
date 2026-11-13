"""Day 13: the worker cycle against a real database and the mock port."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa
from it_helpers import APPROVE_ITEM, BLOCK_ITEM, ESCALATE_ITEM, FixtureWorldProvider, add_business, scripted_llm
from sqlalchemy.orm import Session, sessionmaker
from tessera_audit import AuditEvent, AuditLog
from tessera_core.domain import Currency
from tessera_store import models as m
from tessera_worker.loop import _business_cycle, run_cycle
from tessera_worker.submit import execution_key, process_proposal
from worker_helpers import ChangingWorld, FlakyMock, balance, exec_deps, loop_deps

Factory = sessionmaker[Session]


@pytest.fixture
def world() -> ChangingWorld:
    return ChangingWorld(FixtureWorldProvider())


@pytest.fixture
def port() -> FlakyMock:
    return FlakyMock()


def cycle(factory: Factory, port: FlakyMock, world: ChangingWorld, *items: dict[str, object], **over: object):  # type: ignore[no-untyped-def]
    deps = loop_deps(exec_deps(factory, port, world, **over), lambda: scripted_llm(*items))  # type: ignore[arg-type]
    return run_cycle(deps)


def proposals(session: Session) -> dict[str, m.Proposal]:
    session.expire_all()
    return {p.action_type + ":" + p.status: p for p in session.scalars(sa.select(m.Proposal))}


def event_types(session: Session) -> list[str]:
    return [e.event_type for e in session.scalars(sa.select(AuditEvent).order_by(AuditEvent.seq))]


def test_an_approved_conversion_is_checked_again_sent_once_and_settled(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    report = cycle(session_factory, port, world, APPROVE_ITEM)
    assert report["proposals_created"] == 1 and report["exec_settled"] == 1
    [row] = session.scalars(sa.select(m.Proposal)).all()
    assert row.status == "SETTLED"
    execution = session.scalars(sa.select(m.Execution)).one()
    assert (execution.status, execution.idempotency_key) == ("SETTLED", execution_key(row.id))
    assert execution.vendor_request_id == execution.idempotency_key and execution.external_id
    phases = session.scalars(sa.select(m.PolicyDecision.phase).order_by(m.PolicyDecision.id)).all()
    assert phases == ["plan", "execution"]  # policy ran twice: when proposed and again before sending
    assert balance(port, Currency.EUR) == Decimal("49000.00")
    types = event_types(session)
    assert types.index("execution_submitted") < types.index("execution_result")
    assert AuditLog(session).verify_chain().ok


def test_a_second_cycle_changes_nothing(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    cycle(session_factory, port, world, APPROVE_ITEM)
    before = (balance(port, Currency.EUR), len(port.calls), len(event_types(session)))
    again = cycle(session_factory, port, world, APPROVE_ITEM)
    assert again["proposals_created"] == 0 and again["proposals_already_known"] == 1
    assert (balance(port, Currency.EUR), len(port.calls), len(event_types(session))) == before


def test_the_key_is_durable_before_the_vendor_is_called(
    session_factory: Factory, db_url: str, port: FlakyMock, world: ChangingWorld, session: Session
) -> None:
    add_business(session)
    seen: list[tuple[str, str]] = []

    def look(name: str) -> None:
        with session_factory() as other:  # a different connection sees only what was committed
            row = other.scalars(sa.select(m.Execution)).one()
            seen.append((row.status, row.idempotency_key))

    port.on_call = look
    cycle(session_factory, port, world, APPROVE_ITEM)
    [(status, key)] = seen
    assert status == "PENDING" and key.startswith("tsr-")


@pytest.mark.parametrize("failure", ["fail_before", "fail_after"])
def test_a_crash_around_the_call_is_replayed_with_the_same_key_and_pays_once(
    failure: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    setattr(port, failure, 1)  # first call: nothing happened (before) or it happened but we never heard (after)
    first = cycle(session_factory, port, world, APPROVE_ITEM)
    assert first["exec_retry_later"] == 1
    session.expire_all()
    assert session.scalars(sa.select(m.Execution.status)).one() == "PENDING"
    assert session.scalars(sa.select(m.Proposal.status)).one() == "APPROVED"
    second = cycle(session_factory, port, world, APPROVE_ITEM)
    assert second["exec_settled"] == 1
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "SETTLED"
    assert len(session.scalars(sa.select(m.Execution)).all()) == 1
    assert balance(port, Currency.EUR) == Decimal("49000.00")  # one conversion, however it failed
    assert AuditLog(session).verify_chain().ok


def _plan_only(
    session_factory: Factory, port: FlakyMock, world: ChangingWorld, session: Session, *items: dict[str, object]
):  # type: ignore[no-untyped-def]
    """Plan without executing, so the proposal sits APPROVED while the test changes the world."""
    deps = loop_deps(exec_deps(session_factory, port, world), lambda: scripted_llm(*items))  # type: ignore[arg-type]
    _business_cycle(deps, "B0", Counter())
    session.expire_all()
    return deps, session.scalars(sa.select(m.Proposal.id)).one()


def test_the_world_changing_after_approval_stops_the_send(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    deps, pid = _plan_only(session_factory, port, world, session, APPROVE_ITEM)
    assert session.scalars(sa.select(m.Proposal.status)).one() == "APPROVED"
    world.change = lambda ctx: ctx.model_copy(update={"kill_switch_engaged": True})  # the operator pulls the switch
    assert process_proposal(deps.exec, pid) == "blocked"
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "BLOCKED"
    assert port.calls == [] and session.scalars(sa.select(m.Execution)).all() == []
    last = session.scalars(sa.select(m.PolicyDecision).order_by(m.PolicyDecision.id.desc())).first()
    assert last is not None and (last.phase, last.verdict) == ("execution", "BLOCK")


def test_no_fresh_context_means_no_execution_and_the_proposal_waits(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    deps, pid = _plan_only(session_factory, port, world, session, APPROVE_ITEM)
    world.unavailable = True
    assert process_proposal(deps.exec, pid) == "deferred_no_world"
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "APPROVED"  # still waiting, not failed
    assert port.calls == [] and session.scalars(sa.select(m.Execution)).all() == []
    world.unavailable = False
    assert process_proposal(deps.exec, pid) == "settled"  # and it runs once the books are back


def test_a_pending_execution_the_fresh_check_no_longer_passes_is_held_not_failed(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    port.fail_after = 1  # the conversion happened, but we never heard back: PENDING, outcome unknown
    cycle(session_factory, port, world, APPROVE_ITEM)
    world.change = lambda ctx: ctx.model_copy(update={"kill_switch_engaged": True})
    report = cycle(session_factory, port, world, APPROVE_ITEM)
    assert report["exec_held_for_review"] == 1
    session.expire_all()
    assert session.scalars(sa.select(m.Execution.status)).one() == "PENDING"  # not relabelled FAILED
    assert session.scalars(sa.select(m.Proposal.status)).one() == "APPROVED"
    assert port.calls == ["conversion"]


def test_a_pending_execution_older_than_the_replay_window_is_held_for_a_person(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    port.fail_before = 1
    cycle(session_factory, port, world, APPROVE_ITEM)
    later = lambda: datetime.now(UTC) + timedelta(days=3)  # noqa: E731
    report = cycle(session_factory, port, world, APPROVE_ITEM, clock=later, max_age=timedelta(hours=24))
    assert report["exec_held_stale"] == 1
    assert port.calls == ["conversion"]  # the one failed attempt; nothing replayed


def test_policy_blocks_and_escalations_are_queued_not_executed(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    report = cycle(session_factory, port, world, ESCALATE_ITEM, BLOCK_ITEM)
    assert report["proposals_created"] == 2 and "exec_settled" not in report
    assert sorted(p.status for p in session.scalars(sa.select(m.Proposal))) == ["BLOCKED", "PENDING_APPROVAL"]
    assert port.calls == [] and session.scalars(sa.select(m.Execution)).all() == []


def test_a_human_approved_escalation_runs_through_the_gate_once_policy_still_agrees(
    session_factory: Factory,
    session: Session,
    port: FlakyMock,
    world: ChangingWorld,
    make_client: Callable[..., object],
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from it_helpers import HEADERS  # noqa: PLC0415

    add_business(session)
    cycle(session_factory, port, world, ESCALATE_ITEM)
    client = make_client(world_provider=None)
    assert isinstance(client, TestClient)
    pid = session.scalars(sa.select(m.Proposal.id)).one()
    assert client.post(f"/proposals/{pid}/approve", headers=HEADERS).status_code == 200
    report = cycle(session_factory, port, world)
    # The same rule that escalated it escalates it now, but the human saw that rule: the gate lets it through.
    # The payout then FAILS safely before any call, because this one funds a payout in INR from USD (a conversion
    # nobody asked for) and the directory knows no vendor beneficiary.
    assert report["exec_failed"] == 1 and "exec_re_escalated" not in report
    session.expire_all()
    execution = session.scalars(sa.select(m.Execution)).one()
    assert execution.status == "FAILED" and (execution.result or {}).get("error") == "cross_currency_payout_unsupported"
    assert port.calls == []


def test_a_human_approval_does_not_survive_a_block(
    session_factory: Factory,
    session: Session,
    port: FlakyMock,
    world: ChangingWorld,
    make_client: Callable[..., object],
) -> None:
    from fastapi.testclient import TestClient  # noqa: PLC0415
    from it_helpers import HEADERS  # noqa: PLC0415

    add_business(session)
    cycle(session_factory, port, world, ESCALATE_ITEM)
    client = make_client(world_provider=None)
    assert isinstance(client, TestClient)
    pid = session.scalars(sa.select(m.Proposal.id)).one()
    client.post(f"/proposals/{pid}/approve", headers=HEADERS)
    world.change = lambda ctx: ctx.model_copy(update={"kill_switch_engaged": True})
    report = cycle(session_factory, port, world)
    assert report["exec_blocked"] == 1
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "BLOCKED"
    assert port.calls == []
