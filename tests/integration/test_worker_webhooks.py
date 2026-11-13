"""Day 13: the webhook endpoint (signature, freshness, replay, dedupe) and what an event does to an execution."""

from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy.orm import Session, sessionmaker
from tessera_airwallex.webhooks import compute_signature
from tessera_audit import AuditEvent, AuditLog
from tessera_store import models as m
from tessera_worker.loop import LoopDeps, _reconcile
from tessera_worker.settings import WorkerSettings
from tessera_worker.webhooks import create_webhook_app
from worker_helpers import FlakyMock, exec_deps, seed_proposal

SECRET = "whsec-test-0123456789"
NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
Factory = sessionmaker[Session]


def stamp(at: datetime = NOW) -> str:
    return str(int(at.timestamp() * 1000))


def event(
    event_id: str = "evt-1", name: str = "payout.transfer.paid", resource: str = "tr-1", request: str = "req-1"
) -> bytes:
    body = {"id": event_id, "name": name, "created_at": "2026-03-02T09:00:00+0000",
            "data": {"object": {"id": resource, "request_id": request}}}  # fmt: skip
    return json.dumps(body).encode()


def signed(body: bytes, at: datetime = NOW, secret: str = SECRET) -> dict[str, str]:
    ts = stamp(at)
    return {"x-timestamp": ts, "x-signature": compute_signature(SecretStr(secret), ts, body)}


@pytest.fixture
def client(db_url: str, session_factory: Factory) -> TestClient:
    settings = WorkerSettings(database_url=db_url, webhook_secret=SecretStr(SECRET))
    return TestClient(create_webhook_app(settings, session_factory=session_factory, clock=lambda: NOW))


def seed_execution(session: Session, status: str = "SUBMITTED", *, external_id: str | None = "tr-1") -> None:
    seed_proposal(session, status="SUBMITTED" if status != "PENDING" else "APPROVED")
    session.add(m.Execution(proposal_id="prp-1", idempotency_key="tsr-k1", vendor_request_id="req-1", status=status,
                            external_id=external_id))  # fmt: skip
    session.commit()


def states(session: Session) -> tuple[str, str]:
    session.expire_all()
    return session.scalars(sa.select(m.Proposal.status)).one(), session.scalars(sa.select(m.Execution.status)).one()


def audit_types(session: Session) -> list[str]:
    session.expire_all()
    return [e.event_type for e in session.scalars(sa.select(AuditEvent).order_by(AuditEvent.seq))]


def post(client: TestClient, body: bytes, headers: dict[str, str] | None = None) -> Any:
    return client.post("/webhooks/airwallex", content=body, headers=headers if headers is not None else signed(body))


def test_a_signed_paid_event_settles_the_execution_and_is_audited(client: TestClient, session: Session) -> None:
    seed_execution(session)
    body = event()
    response = post(client, body)
    assert (response.status_code, response.json()) == (200, {"status": "applied"})
    assert states(session) == ("SETTLED", "SETTLED")
    assert audit_types(session) == ["execution_result", "webhook_received"]
    row = session.scalars(sa.select(m.WebhookEvent)).one()
    assert (row.id, row.outcome) == ("evt-1", "applied") and row.execution_id is not None
    assert AuditLog(session).verify_chain().ok


def test_a_failed_event_fails_the_execution(client: TestClient, session: Session) -> None:
    seed_execution(session)
    assert post(client, event(name="payout.transfer.failed")).json() == {"status": "applied"}
    assert states(session) == ("FAILED", "FAILED")


def test_the_same_event_delivered_twice_is_applied_and_audited_once(client: TestClient, session: Session) -> None:
    seed_execution(session)
    body = event()
    assert post(client, body).json() == {"status": "applied"}
    again = post(client, body)
    assert (again.status_code, again.json()) == (200, {"status": "duplicate"})
    assert audit_types(session).count("webhook_received") == 1 and audit_types(session).count("execution_result") == 1
    assert len(session.scalars(sa.select(m.WebhookEvent)).all()) == 1


def test_a_bad_signature_is_refused_and_leaves_no_trace(client: TestClient, session: Session) -> None:
    seed_execution(session)
    body = event()
    for headers in (
        signed(body, secret="a-different-secret"),
        {"x-timestamp": stamp()},
        {"x-signature": "00" * 32},
        {},
    ):
        assert post(client, body, headers).status_code == 401
    tampered = signed(body)
    assert post(client, body.replace(b"paid", b"failed"), tampered).status_code == 401  # signed another body
    assert states(session) == ("SUBMITTED", "SUBMITTED")
    assert session.scalars(sa.select(m.WebhookEvent)).all() == [] and audit_types(session) == []


def test_a_replayed_delivery_outside_the_window_is_refused_even_with_a_valid_signature(
    client: TestClient, session: Session
) -> None:
    seed_execution(session)
    body = event()
    old = signed(body, at=NOW - timedelta(minutes=6))
    response = post(client, body, old)
    assert response.status_code == 400
    assert post(client, body, signed(body, at=NOW + timedelta(minutes=6))).status_code == 400
    assert post(client, body, signed(body, at=NOW - timedelta(minutes=4))).status_code == 200  # inside the window
    assert states(session) == ("SETTLED", "SETTLED")


def test_an_unreadable_or_oversized_body_is_refused(client: TestClient, session: Session) -> None:
    junk = b"not json"
    assert post(client, junk).status_code == 400
    assert post(client, json.dumps({"id": "", "name": "x"}).encode()).status_code == 400
    assert post(client, b"x" * 1_100_000, {}).status_code == 413
    assert session.scalars(sa.select(m.WebhookEvent)).all() == []


def test_a_late_event_never_moves_a_state_backwards(client: TestClient, session: Session) -> None:
    seed_execution(session)
    post(client, event("evt-1", "payout.transfer.paid"))
    late = post(client, event("evt-2", "payout.transfer.failed"))  # out of order: failed after paid
    assert late.json() == {"status": "ignored"}
    assert states(session) == ("SETTLED", "SETTLED")
    assert session.scalars(sa.select(m.WebhookEvent.outcome).where(m.WebhookEvent.id == "evt-2")).one() == "ignored"
    assert audit_types(session).count("webhook_received") == 2  # still on the record


def test_an_in_flight_event_changes_nothing(client: TestClient, session: Session) -> None:
    seed_execution(session)
    assert post(client, event(name="payout.transfer.processing")).json() == {"status": "ignored"}
    assert states(session) == ("SUBMITTED", "SUBMITTED")


def test_the_execution_is_found_by_request_id_when_the_vendor_id_is_not_stored_yet(
    client: TestClient, session: Session
) -> None:
    seed_execution(session, external_id=None)
    assert post(client, event(resource="tr-unseen")).json() == {"status": "applied"}


def test_an_event_that_beats_the_recorded_submission_waits_and_the_worker_applies_it(
    client: TestClient, session: Session, session_factory: Factory
) -> None:
    seed_execution(session, status="PENDING", external_id=None)  # sent, but the worker has not recorded it yet
    assert post(client, event()).json() == {"status": "unmatched"}
    assert states(session) == ("APPROVED", "PENDING")
    with session_factory() as s:  # the worker records the submission...
        ex = s.scalars(sa.select(m.Execution)).one()
        ex.status, ex.external_id = "SUBMITTED", "tr-1"
        s.get(m.Proposal, "prp-1").status = "SUBMITTED"  # type: ignore[union-attr]
        s.commit()
    deps = LoopDeps(exec=exec_deps(session_factory, FlakyMock(), None, clock=lambda: NOW), llm_factory=lambda: None)  # type: ignore[arg-type,return-value]
    report: Counter[str] = Counter()
    _reconcile(deps, report)  # ...and its next reconcile applies the stored event
    assert report["reconciled_applied"] == 1
    assert states(session) == ("SETTLED", "SETTLED")
    _reconcile(deps, report)
    assert report["reconciled_applied"] == 1  # a second pass finds nothing left to do
    assert AuditLog(session).verify_chain().ok


def test_an_event_for_an_unknown_transfer_is_kept_unmatched(client: TestClient, session: Session) -> None:
    seed_execution(session)
    assert post(client, event(resource="tr-other", request="req-other")).json() == {"status": "unmatched"}
    assert states(session) == ("SUBMITTED", "SUBMITTED")


def test_the_app_will_not_start_without_a_secret(db_url: str) -> None:
    from tessera_worker.settings import SettingsError  # noqa: PLC0415

    with pytest.raises(SettingsError):
        WorkerSettings(database_url=db_url, webhook_secret=SecretStr(""))
    with pytest.raises(SettingsError):
        WorkerSettings.from_env({"DATABASE_URL": db_url})


def test_nothing_leaks_the_secret_or_the_body(client: TestClient, session: Session) -> None:
    seed_execution(session)
    body = event()
    for response in (post(client, body, {}), post(client, b"nope")):
        assert SECRET not in response.text and "tr-1" not in response.text
    assert SECRET not in repr(WorkerSettings(database_url="x", webhook_secret=SecretStr(SECRET)))
