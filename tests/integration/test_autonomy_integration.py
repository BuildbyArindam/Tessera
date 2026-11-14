"""Day 14: the autonomy dial against a real database, the real worker cycle and the real HTTP API."""

from __future__ import annotations

import dataclasses
import json
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from it_helpers import (
    APPROVE_ITEM,
    ESCALATE_ITEM,
    T0,
    FixtureWorldProvider,
    add_business,
    scripted_llm,
    set_stored_level,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from tessera_api.autonomy_service import AutonomyInputs
from tessera_audit import AuditEvent, AuditLog
from tessera_core.autonomy import (
    AutonomyController,
    CoverageObservation,
    Evidence,
    SavingObservation,
    ShadowObservation,
    ViolationObservation,
    load_autonomy_config,
)
from tessera_store import models as m
from tessera_worker.loop import AutonomyDeps, _business_cycle, run_cycle
from tessera_worker.submit import process_proposal
from worker_helpers import ChangingWorld, FlakyMock, exec_deps, loop_deps

Factory = sessionmaker[Session]
CONTROLLER = AutonomyController(load_autonomy_config())
OVER_L1_CAP = {
    "action_type": "CONVERT",
    "params": {"amount": "1500.00", "currency": "EUR", "to_currency": "INR", "obligation_id": "OBL-0002"},
}  # L1's EUR cap is 1000.00; the other rules are fine with 1500


@pytest.fixture
def db_url(new_db: Callable[[], str]) -> str:
    """These tests choose their own level: the database is exactly as the migration leaves it (L0)."""
    return new_db()


@pytest.fixture
def world() -> ChangingWorld:
    return ChangingWorld(FixtureWorldProvider())


@pytest.fixture
def port() -> FlakyMock:
    return FlakyMock()


# ---- evidence and a provider the tests can change ------------------------------------------------
def _times(n: int) -> list[Any]:
    return [T0 - timedelta(minutes=10 * (i + 1)) for i in range(n)]


def good_evidence() -> Evidence:
    values = [Decimal("10.00"), Decimal("15.00")]
    return Evidence(
        shadow=tuple(ShadowObservation(at=t, agreed=i < 36) for i, t in enumerate(_times(40))),
        savings=tuple(SavingObservation(at=t, saving_per_10k=values[i % 2]) for i, t in enumerate(_times(25))),
        coverage=tuple(
            CoverageObservation(at=t, covered=i < 32, target=Decimal("0.80")) for i, t in enumerate(_times(40))
        ),
    )


class Inputs:
    """An AutonomyInputsProvider whose answer the test can change between calls."""

    def __init__(self, evidence: Evidence | None = None, *, kill: bool = False) -> None:
        self.evidence, self.kill, self.broken = evidence or good_evidence(), kill, False

    def load(self, session: Session, now: Any) -> AutonomyInputs:
        if self.broken:
            raise RuntimeError("the evidence store is down")
        return AutonomyInputs(self.evidence, self.kill)


def set_level(url: str, level: int) -> None:
    set_stored_level(url, level, at=T0 - timedelta(days=10))  # the level began 10 days ago: evidence since then counts


def autonomy_events(session: Session) -> list[dict[str, Any]]:
    rows = session.scalars(
        sa.select(AuditEvent).where(AuditEvent.event_type == "autonomy_changed").order_by(AuditEvent.seq)
    )
    return [json.loads(r.payload) for r in rows]


def state(session: Session) -> m.AutonomyState:
    session.expire_all()
    return session.get(m.AutonomyState, 1)  # type: ignore[return-value]


def worker_cycle(
    factory: Factory, port: FlakyMock, world: ChangingWorld, inputs: Inputs | None, *items: Any
) -> Counter[str]:
    deps = loop_deps(exec_deps(factory, port, world), lambda: scripted_llm(*items))  # type: ignore[arg-type]
    if inputs is not None:
        deps = dataclasses.replace(deps, autonomy=AutonomyDeps(CONTROLLER, inputs))
    return run_cycle(deps)


# ---- the starting point --------------------------------------------------------------------------
def test_a_fresh_database_starts_advise_only(make_client: Callable[..., TestClient]) -> None:
    body = make_client().get("/autonomy").json()
    assert body["level"] == "L0" and body["pending_promotion"] is None
    assert body["evidence_available"] is False and body["assessment"] is None


def test_autonomy_needs_the_api_key_and_a_well_formed_body(make_client: Callable[..., TestClient]) -> None:
    assert make_client(authenticated=False).get("/autonomy").status_code == 401
    assert (
        make_client(authenticated=False).post("/autonomy", json={"action": "demote", "to_level": "L0"}).status_code
        == 401
    )
    client = make_client()
    for body in (
        {"action": "set_level", "to_level": "L3"},  # there is no way to raise the level directly
        {"action": "demote", "to_level": "L9"},
        {"action": "demote", "to_level": "L0", "extra": 1},
        {"action": "confirm_promotion", "promotion_id": "one"},
        {},
    ):
        assert client.post("/autonomy", json=body).status_code == 422, body


# ---- what each level lets through ----------------------------------------------------------------
def test_at_l0_a_conversion_is_never_executed_but_is_logged_as_a_shadow_decision(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    add_business(session)
    report = worker_cycle(session_factory, port, world, None, APPROVE_ITEM)
    assert report["proposals_created"] == 1 and not any(k.startswith("exec_") for k in report)
    session.expire_all()
    row = session.scalars(sa.select(m.Proposal)).one()
    decision = session.scalars(sa.select(m.PolicyDecision)).one()
    assert (row.action_type, row.status) == ("CONVERT", "BLOCKED") and decision.rule_ids == ["autonomy_level_gate"]
    assert port.calls == [] and session.scalars(sa.select(m.Execution)).all() == []
    shadow = [
        json.loads(e.payload)
        for e in session.scalars(sa.select(AuditEvent).where(AuditEvent.event_type == "policy_decision"))
    ]
    assert [p["shadow"] for p in shadow] == [True]  # kept and shown to a person, as evidence for the next promotion


def test_at_l1_a_conversion_over_the_small_cap_is_escalated_and_a_payout_is_blocked(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    set_level(db_url, 1)
    add_business(session)
    worker_cycle(session_factory, port, world, None, OVER_L1_CAP, ESCALATE_ITEM)
    session.expire_all()
    by_action = {p.action_type: p for p in session.scalars(sa.select(m.Proposal))}
    assert by_action["CONVERT"].status == "PENDING_APPROVAL" and by_action["PAY_SUPPLIER"].status == "BLOCKED"
    verdicts = {d.proposal_id: d for d in session.scalars(sa.select(m.PolicyDecision))}
    assert "autonomy_level_gate" in verdicts[by_action["CONVERT"].id].rule_ids
    assert "autonomy_level_gate" in verdicts[by_action["PAY_SUPPLIER"].id].rule_ids
    assert port.calls == []


def test_at_l1_a_small_conversion_runs(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    set_level(db_url, 1)
    add_business(session)
    report = worker_cycle(session_factory, port, world, None, APPROVE_ITEM)  # EUR 1000.00, equal to the L1 cap
    assert report["exec_settled"] == 1


def test_a_demotion_after_approval_stops_the_send(
    db_url: str,
    session_factory: Factory,
    session: Session,
    port: FlakyMock,
    world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:
    set_level(db_url, 3)
    add_business(session)
    deps = loop_deps(exec_deps(session_factory, port, world), lambda: scripted_llm(APPROVE_ITEM))  # type: ignore[arg-type]
    _business_cycle(deps, "B0", Counter())
    session.expire_all()
    pid = session.scalars(sa.select(m.Proposal.id)).one()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "APPROVED"  # approved while the level was L3
    assert make_client().post("/autonomy", json={"action": "demote", "to_level": "L0"}).status_code == 200
    assert process_proposal(deps.exec, pid) == "blocked"  # the fresh check sees L0
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "BLOCKED"
    assert port.calls == [] and session.scalars(sa.select(m.Execution)).all() == []
    last = session.scalars(sa.select(m.PolicyDecision).order_by(m.PolicyDecision.id.desc())).first()
    assert last is not None and last.phase == "execution" and "autonomy_level_gate" in last.rule_ids


def test_a_provider_cannot_claim_a_level_the_database_does_not_hold(
    session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    from tessera_core.policy import AutonomyLevel  # noqa: PLC0415

    world.change = lambda ctx: ctx.model_copy(update={"autonomy_level": AutonomyLevel.L3})  # the world says L3
    add_business(session)
    worker_cycle(session_factory, port, world, None, APPROVE_ITEM)
    session.expire_all()
    assert session.scalars(sa.select(m.Proposal.status)).one() == "BLOCKED"  # the stored level is L0, and wins
    ctx = session.scalars(sa.select(m.PolicyDecision)).one().context
    assert ctx is not None and ctx["autonomy_level"] == 0


# ---- promotion: proposed by the worker, confirmed by a person, re-checked at confirmation ---------
def test_the_worker_proposes_a_promotion_and_changes_nothing_until_a_person_confirms(
    db_url: str,
    session_factory: Factory,
    session: Session,
    port: FlakyMock,
    world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:
    set_level(db_url, 1)
    inputs = Inputs()
    assert worker_cycle(session_factory, port, world, inputs)["autonomy_promotion_proposed"] == 1
    assert state(session).level == 1  # proposed, not applied
    assert worker_cycle(session_factory, port, world, inputs)["autonomy_promotion_already_pending"] == 1
    assert session.scalars(sa.select(sa.func.count()).select_from(m.AutonomyPromotion)).one() == 1
    [proposed] = autonomy_events(session)
    assert proposed["kind"] == "promotion_proposed" and proposed["level_changed"] is False
    assert (proposed["from_level"], proposed["to_level"], proposed["actor"]) == ("L1", "L2", "controller")

    client = make_client(autonomy_inputs=inputs)
    body = client.get("/autonomy").json()
    promotion = body["pending_promotion"]
    assert body["level"] == "L1" and promotion["verdict"] == "ESCALATE" and promotion["status"] == "PENDING"
    assert body["evidence_available"] and body["assessment"]["kind"] == "PROPOSE_PROMOTION"

    confirmed = client.post(
        "/autonomy", json={"action": "confirm_promotion", "promotion_id": promotion["id"], "note": "ok"}
    )
    assert (
        confirmed.status_code == 200
        and confirmed.json()["level"] == "L2"
        and confirmed.json()["pending_promotion"] is None
    )
    assert state(session).level == 2
    last = autonomy_events(session)[-1]
    assert (last["kind"], last["level_changed"], last["actor"], last["note"]) == (
        "promotion_confirmed",
        True,
        "operator",
        "ok",
    )
    assert last["assessment"]["config_hash"] == CONTROLLER.config.hash and last["assessment"]["criteria"]
    assert AuditLog(session).verify_chain().ok


def test_confirming_re_checks_the_evidence_and_refuses_when_it_no_longer_supports_the_promotion(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:  # fmt: skip
    set_level(db_url, 1)
    inputs = Inputs()
    worker_cycle(session_factory, port, world, inputs)
    pid = session.scalars(sa.select(m.AutonomyPromotion.id)).one()
    inputs.evidence = Evidence(shadow=good_evidence().shadow)  # the saving and coverage evidence has gone
    refused = make_client(autonomy_inputs=inputs).post(
        "/autonomy", json={"action": "confirm_promotion", "promotion_id": pid}
    )
    assert refused.status_code == 409 and "no longer supports" in refused.json()["detail"]
    assert state(session).level == 1
    assert session.get(m.AutonomyPromotion, pid, populate_existing=True).status == "CANCELLED"  # type: ignore[union-attr]
    assert autonomy_events(session)[-1]["kind"] == "promotion_cancelled"


def test_confirming_when_a_violation_has_appeared_demotes_instead(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:  # fmt: skip
    set_level(db_url, 1)
    inputs = Inputs()
    worker_cycle(session_factory, port, world, inputs)
    pid = session.scalars(sa.select(m.AutonomyPromotion.id)).one()
    bad = ViolationObservation(at=T0 - timedelta(minutes=1), kind="executed_without_approval")
    inputs.evidence = good_evidence().model_copy(update={"violations": (bad,)})
    refused = make_client(autonomy_inputs=inputs).post(
        "/autonomy", json={"action": "confirm_promotion", "promotion_id": pid}
    )
    assert refused.status_code == 409 and "demoted instead" in refused.json()["detail"]
    assert state(session).level == 0
    kinds = [e["kind"] for e in autonomy_events(session)]
    assert kinds[-2:] == ["demoted", "promotion_cancelled"]


def test_without_an_evidence_source_nothing_can_be_confirmed(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:  # fmt: skip
    set_level(db_url, 1)
    worker_cycle(session_factory, port, world, Inputs())
    pid = session.scalars(sa.select(m.AutonomyPromotion.id)).one()
    refused = make_client().post("/autonomy", json={"action": "confirm_promotion", "promotion_id": pid})
    assert refused.status_code == 409 and "cannot be re-checked" in refused.json()["detail"]
    assert state(session).level == 1
    assert session.get(m.AutonomyPromotion, pid, populate_existing=True).status == "PENDING"  # type: ignore[union-attr]


def test_confirm_reject_and_demote_edge_cases(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:  # fmt: skip
    set_level(db_url, 1)
    inputs = Inputs()
    worker_cycle(session_factory, port, world, inputs)
    pid = session.scalars(sa.select(m.AutonomyPromotion.id)).one()
    client = make_client(autonomy_inputs=inputs)
    assert client.post("/autonomy", json={"action": "confirm_promotion", "promotion_id": 9999}).status_code == 404
    assert client.post("/autonomy", json={"action": "demote", "to_level": "L1"}).status_code == 409  # not lower
    assert client.post("/autonomy", json={"action": "demote", "to_level": "L3"}).status_code == 409  # never up
    assert (
        client.post(
            "/autonomy", json={"action": "reject_promotion", "promotion_id": pid, "note": "not yet"}
        ).status_code
        == 200
    )
    assert state(session).level == 1
    again = client.post("/autonomy", json={"action": "confirm_promotion", "promotion_id": pid})
    assert again.status_code == 409 and "REJECTED" in again.json()["detail"]
    assert [e["kind"] for e in autonomy_events(session)] == ["promotion_proposed", "promotion_rejected"]
    assert (
        client.post("/autonomy", json={"action": "demote", "to_level": "L0", "note": "stand down"}).status_code == 200
    )
    last = autonomy_events(session)[-1]
    assert (last["kind"], last["from_level"], last["to_level"], last["actor"]) == ("demoted", "L1", "L0", "operator")


# ---- automatic demotion --------------------------------------------------------------------------
def test_a_violation_demotes_in_the_worker_cycle_and_withdraws_an_open_promotion(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    set_level(db_url, 1)
    inputs = Inputs()
    worker_cycle(session_factory, port, world, inputs)  # a promotion is pending
    bad = ViolationObservation(at=T0 - timedelta(minutes=1), kind="executed_without_approval")
    inputs.evidence = good_evidence().model_copy(update={"violations": (bad,)})
    assert worker_cycle(session_factory, port, world, inputs)["autonomy_demoted"] == 1
    row = state(session)
    assert row.level == 0 and row.since > T0 - timedelta(days=1)  # the evidence clock restarted
    kinds = [e["kind"] for e in autonomy_events(session)]
    assert kinds == ["promotion_proposed", "demoted", "promotion_cancelled"]
    demoted = autonomy_events(session)[1]
    assert demoted["triggers"] == ["violation"] and demoted["actor"] == "controller" and demoted["level_changed"]
    assert worker_cycle(session_factory, port, world, inputs)["autonomy_no_change"] == 1  # and it does not repeat
    assert AuditLog(session).verify_chain().ok


def test_the_kill_switch_demotes_before_the_same_cycle_executes_anything(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    set_level(db_url, 3)
    add_business(session)
    planned = loop_deps(exec_deps(session_factory, port, world), lambda: scripted_llm(APPROVE_ITEM))  # type: ignore[arg-type]
    _business_cycle(planned, "B0", Counter())  # an APPROVED conversion is waiting
    report = worker_cycle(session_factory, port, world, Inputs(kill=True), APPROVE_ITEM)
    assert report["autonomy_demoted"] == 1 and report["exec_blocked"] == 1
    assert state(session).level == 0 and port.calls == []


def test_a_broken_evidence_source_leaves_the_level_alone_and_is_counted(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld,
    make_client: Callable[..., TestClient],
) -> None:  # fmt: skip
    set_level(db_url, 2)
    inputs = Inputs()
    inputs.broken = True
    assert worker_cycle(session_factory, port, world, inputs)["autonomy_errors"] == 1
    assert state(session).level == 2
    body = make_client(autonomy_inputs=inputs).get("/autonomy").json()  # the read survives a broken source
    assert body["level"] == "L2" and body["evidence_available"] is False


def test_without_a_provider_the_worker_says_so_and_the_dial_stays_put(
    db_url: str, session_factory: Factory, session: Session, port: FlakyMock, world: ChangingWorld
) -> None:
    set_level(db_url, 2)
    assert worker_cycle(session_factory, port, world, None)["autonomy_skipped_no_inputs"] == 1
    assert state(session).level == 2


# ---- the database enforces its own rules ---------------------------------------------------------
@pytest.fixture
def raw(session: Session) -> Iterator[Session]:
    yield session
    session.rollback()


def _promotion(**over: Any) -> m.AutonomyPromotion:
    return m.AutonomyPromotion(
        **{"from_level": 1, "to_level": 2, "status": "PENDING", "assessment": {}, "proposed_at": T0, **over}
    )


def test_only_one_promotion_can_be_pending(raw: Session) -> None:
    raw.add_all([_promotion(), _promotion()])
    with pytest.raises(IntegrityError):
        raw.flush()


def test_a_promotion_moves_one_level_and_the_state_is_one_row_in_range(raw: Session) -> None:
    for bad in (_promotion(to_level=3), _promotion(from_level=3, to_level=4), _promotion(status="MAYBE")):
        raw.add(bad)
        with pytest.raises(IntegrityError):
            raw.flush()
        raw.rollback()
    for statement in (
        "INSERT INTO autonomy_state (id, level, since) VALUES (2, 0, now())",
        "UPDATE autonomy_state SET level = 4 WHERE id = 1",
    ):
        with pytest.raises(IntegrityError):
            raw.execute(sa.text(statement))
        raw.rollback()
