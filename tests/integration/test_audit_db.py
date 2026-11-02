"""Day 11: the audit log against a real PostgreSQL. Append, chain, trigger, tamper evidence, concurrency."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from tessera_audit import GENESIS_HASH, AuditEvent, AuditEventType, AuditLog, CanonicalError, canonical_json

T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def fill(session: Session, n: int = 10) -> AuditLog:
    log = AuditLog(session, clock=lambda: T0)
    for i in range(n):
        log.append(AuditEventType.PROPOSAL_CREATED, {"i": i, "note": f"event {i}"}, at=T0 + timedelta(seconds=i))
    session.commit()
    return log


def raw(engine: sa.Engine, sql: str, **params: object) -> None:
    with engine.begin() as conn:
        conn.execute(sa.text(sql), params)


def tamper(engine: sa.Engine, sql: str, **params: object) -> None:
    """What an attacker with table-owner rights could do: switch the guard off, edit, switch it back on."""
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE audit_events DISABLE TRIGGER USER"))
        conn.execute(sa.text(sql), params)
        conn.execute(sa.text("ALTER TABLE audit_events ENABLE TRIGGER USER"))


# ---- the chain ---------------------------------------------------------------------------------------
def test_ten_events_form_a_valid_chain(session: Session) -> None:
    log = fill(session)
    records = log.read(limit=100)
    assert [r.seq for r in records] == list(range(1, 11))
    assert records[0].prev_hash == GENESIS_HASH
    assert all(records[i].prev_hash == records[i - 1].hash for i in range(1, 10))
    status = log.verify_chain()
    assert status.ok and status.checked == 10 and status.head_seq == 10 and status.head_hash == records[-1].hash
    assert status.broken_seq is None and status.reason is None
    assert log.head() == (10, records[-1].hash)


def test_an_empty_log_verifies(session: Session) -> None:
    status = AuditLog(session).verify_chain()
    assert status.ok and status.checked == 0 and status.head_seq is None and AuditLog(session).head() is None


def test_the_stored_payload_is_the_exact_canonical_text(session: Session) -> None:
    log = AuditLog(session, clock=lambda: T0)
    record = log.append(AuditEventType.FORECAST_MADE, {"z": 1, "a": {"amount": "12.50"}})
    session.commit()
    assert record.payload_json == canonical_json({"a": {"amount": "12.50"}, "z": 1}) == '{"a":{"amount":"12.50"},"z":1}'
    assert session.scalar(sa.select(AuditEvent.payload)) == record.payload_json
    assert record.payload == {"a": {"amount": "12.50"}, "z": 1}


def test_a_bad_payload_or_naive_time_writes_nothing(session: Session) -> None:
    log = AuditLog(session)
    with pytest.raises(CanonicalError):
        log.append(AuditEventType.APPROVED, {"x": float("nan")})
    with pytest.raises(CanonicalError):
        log.append(AuditEventType.APPROVED, {"x": 1}, at=datetime(2026, 3, 2, 9, 0))
    assert session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 0


def test_a_rolled_back_append_leaves_no_gap(session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        log = AuditLog(s, clock=lambda: T0)
        log.append(AuditEventType.APPROVED, {"n": 1})
        s.commit()
        log.append(AuditEventType.APPROVED, {"n": 2})
        s.rollback()
        assert log.append(AuditEventType.APPROVED, {"n": 3}).seq == 2
        s.commit()
        assert log.verify_chain().ok


# ---- the trigger ---------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE audit_events SET payload = '{}' WHERE seq = 3",
        "UPDATE audit_events SET hash = repeat('a', 64) WHERE seq = 3",
        "DELETE FROM audit_events WHERE seq = 3",
        "DELETE FROM audit_events",
        "TRUNCATE audit_events",
    ],
)
def test_update_delete_and_truncate_are_rejected_by_the_database(session: Session, engine: sa.Engine, sql: str) -> None:
    fill(session)
    with pytest.raises(DBAPIError, match="append-only"), engine.begin() as conn:
        conn.execute(sa.text(sql))
    with session.begin():  # nothing changed, and the chain still verifies
        assert session.scalar(sa.select(sa.func.count()).select_from(AuditEvent)) == 10
        assert AuditLog(session).verify_chain().ok


def test_the_trigger_error_is_a_restrict_violation(session: Session, engine: sa.Engine) -> None:
    fill(session, 2)
    with pytest.raises(IntegrityError) as caught, engine.begin() as conn:
        conn.execute(sa.text("UPDATE audit_events SET payload = '{}'"))
    assert getattr(caught.value.orig, "sqlstate", None) == "23001"


# ---- tamper evidence (the trigger is switched off, as a table owner could) ------------------------
def test_a_modified_payload_names_the_exact_seq(session: Session, engine: sa.Engine) -> None:
    fill(session)
    tamper(engine, "UPDATE audit_events SET payload = :p WHERE seq = 4", p='{"i":999}')
    with session.begin():
        status = AuditLog(session).verify_chain()
    assert not status.ok and status.broken_seq == 4 and status.checked == 3
    assert status.head_seq is None and "altered" in (status.reason or "")


@pytest.mark.parametrize(
    ("column", "value"),
    [("event_type", "approved"), ("timestamp", T0 + timedelta(days=1))],
)
def test_relabelling_an_event_or_changing_its_time_is_detected(
    session: Session, engine: sa.Engine, column: str, value: object
) -> None:
    fill(session)
    tamper(engine, f"UPDATE audit_events SET {column} = :v WHERE seq = 6", v=value)  # noqa: S608
    with session.begin():
        status = AuditLog(session).verify_chain()
    assert not status.ok and status.broken_seq == 6


def test_verify_reads_the_database_not_a_cached_copy(session: Session, engine: sa.Engine) -> None:
    fill(session)
    cached = session.scalars(sa.select(AuditEvent)).all()  # the session now holds every row in memory
    tamper(engine, "UPDATE audit_events SET payload = :p WHERE seq = 2", p='{"i":42}')
    assert next(r for r in cached if r.seq == 2).payload != '{"i":42}'  # the stale copy still looks fine
    status = AuditLog(session).verify_chain()
    assert not status.ok and status.broken_seq == 2


def test_a_deleted_middle_row_is_named(session: Session, engine: sa.Engine) -> None:
    fill(session)
    tamper(engine, "DELETE FROM audit_events WHERE seq = 5")
    with session.begin():
        status = AuditLog(session).verify_chain()
    assert not status.ok and status.broken_seq == 5 and "missing" in (status.reason or "")


def test_a_row_edited_and_re_hashed_is_caught_at_the_next_row(session: Session, engine: sa.Engine) -> None:
    """Fixing up the edited row's own hash is not enough: the row after it still points at the old hash."""
    log = fill(session)
    edited = next(r for r in log.read(limit=100) if r.seq == 3)
    from tessera_audit import compute_hash, format_ts

    payload = '{"i":777}'
    new_hash = compute_hash(edited.prev_hash, 3, format_ts(edited.timestamp), edited.event_type, payload)
    tamper(engine, "UPDATE audit_events SET payload = :p, hash = :h WHERE seq = 3", p=payload, h=new_hash)
    status = AuditLog(session).verify_chain()
    assert not status.ok and status.broken_seq == 4 and "prev_hash" in (status.reason or "")


def test_known_limit_removing_the_newest_rows_is_not_detected_by_the_chain_alone(
    session: Session, engine: sa.Engine
) -> None:
    """Pinned so nobody assumes otherwise: nothing later points back at the last rows. Publish `head()` to fix."""
    log = fill(session)
    published_head = log.head()
    tamper(engine, "DELETE FROM audit_events WHERE seq > 7")
    status = AuditLog(session).verify_chain()
    assert status.ok and status.checked == 7
    assert AuditLog(session).head() != published_head  # an external anchor is what exposes it


def test_a_fork_is_impossible(session: Session, engine: sa.Engine) -> None:
    fill(session, 3)
    with pytest.raises(IntegrityError), engine.begin() as conn:  # a second row claiming seq 3's predecessor
        conn.execute(
            sa.text(
                "INSERT INTO audit_events (seq, timestamp, event_type, payload, prev_hash, hash) "
                "SELECT 4, now(), 'approved', '{}', prev_hash, repeat('b', 64) FROM audit_events WHERE seq = 3"
            )
        )


@pytest.mark.parametrize(
    "values",
    [
        "(1, now(), 'not_a_type', '{}', repeat('0', 64), repeat('a', 64))",
        "(1, now(), 'approved', '{}', 'short', repeat('a', 64))",
        "(1, now(), 'approved', '{}', repeat('0', 64), repeat('A', 64))",
        "(0, now(), 'approved', '{}', repeat('0', 64), repeat('a', 64))",
    ],
)
def test_malformed_rows_are_rejected_by_check_constraints(engine: sa.Engine, values: str) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO audit_events VALUES {values}"))  # noqa: S608


# ---- concurrency ---------------------------------------------------------------------------------------
def test_concurrent_writers_never_share_a_seq_or_leave_a_gap(session_factory: sessionmaker[Session]) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(w: int) -> None:
        try:
            barrier.wait()
            for i in range(10):
                with session_factory() as s:
                    AuditLog(s).append(AuditEventType.WEBHOOK_RECEIVED, {"worker": w, "i": i})
                    s.commit()
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors
    with session_factory() as s:
        log = AuditLog(s)
        assert [r.seq for r in log.read(limit=500)] == list(range(1, 81))
        status = log.verify_chain()
        assert status.ok and status.checked == 80
