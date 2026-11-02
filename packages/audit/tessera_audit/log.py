"""AuditLog: append events to the hash chain and verify it.

Appending takes a transaction-scoped Postgres advisory lock, so two writers cannot pick the same seq; the lock is
released when the caller's transaction ends. That also means an event commits or rolls back together with the
business rows it describes. Keep transactions short: a long one makes other writers wait.

verify_chain walks the rows in order and stops at the first that fails: a seq out of order or missing, a prev_hash
that does not match the previous row's hash, or a stored hash that does not match the recomputed one.

What it cannot detect: removal of the newest rows (nothing later points back at them) and a full rewrite by someone
who recomputes every hash after the edit. Publishing `head()` somewhere the database owner cannot edit closes both.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from tessera_audit.canonical import GENESIS_HASH, canonical_payload, compute_hash, format_ts
from tessera_audit.events import EVENT_TYPE_VALUES, AuditEventType
from tessera_audit.model import AuditEvent

ADVISORY_LOCK_KEY = 7_700_011  # arbitrary but fixed: every appender must use the same key
VERIFY_BATCH = 2000


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    timestamp: datetime
    event_type: str
    payload_json: str  # the exact text that was hashed
    prev_hash: str
    hash: str

    @property
    def payload(self) -> dict[str, Any]:
        loaded = json.loads(self.payload_json)
        return loaded if isinstance(loaded, dict) else {}


@dataclass(frozen=True)
class ChainStatus:
    ok: bool
    checked: int
    head_seq: int | None
    head_hash: str | None
    broken_seq: int | None = None
    reason: str | None = None


def _record(row: AuditEvent) -> AuditRecord:
    return AuditRecord(row.seq, row.timestamp, row.event_type, row.payload, row.prev_hash, row.hash)


class AuditLog:
    def __init__(self, session: Session, clock: Callable[[], datetime] | None = None) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    def append(
        self, event_type: AuditEventType, payload: Mapping[str, Any], *, at: datetime | None = None
    ) -> AuditRecord:
        if event_type.value not in EVENT_TYPE_VALUES:
            raise ValueError(f"unknown audit event type {event_type!r}")
        payload_text = canonical_payload(payload)  # validates before any lock is taken
        when = at if at is not None else self._clock()
        ts_text = format_ts(when)  # raises on a naive datetime
        when_utc = when.astimezone(UTC)

        self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        last = self._session.execute(
            select(AuditEvent.seq, AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)
        ).first()
        seq = 1 if last is None else last.seq + 1
        prev_hash = GENESIS_HASH if last is None else last.hash
        row = AuditEvent(
            seq=seq,
            timestamp=when_utc,
            event_type=event_type.value,
            payload=payload_text,
            prev_hash=prev_hash,
            hash=compute_hash(prev_hash, seq, ts_text, event_type.value, payload_text),
        )
        self._session.add(row)
        self._session.flush()
        return _record(row)

    def head(self) -> tuple[int, str] | None:
        stmt = select(AuditEvent.seq, AuditEvent.hash).order_by(AuditEvent.seq.desc()).limit(1)
        row = self._session.execute(stmt).first()
        return None if row is None else (row.seq, row.hash)

    def read(self, *, after_seq: int = 0, limit: int = 100) -> list[AuditRecord]:
        stmt = select(AuditEvent).where(AuditEvent.seq > after_seq).order_by(AuditEvent.seq).limit(limit)
        return [_record(row) for row in self._session.scalars(stmt)]

    def verify_chain(self) -> ChainStatus:
        """OK, or the first seq that fails. `head_*` are only set when the whole chain verified."""
        stmt = (
            select(AuditEvent)
            .order_by(AuditEvent.seq)
            .execution_options(
                yield_per=VERIFY_BATCH,
                populate_existing=True,  # always the database's rows, never a session's cached copy
            )
        )
        expected_seq, expected_prev, checked = 1, GENESIS_HASH, 0
        for row in self._session.scalars(stmt):
            problem = _first_problem(row, expected_seq, expected_prev)
            if problem is not None:
                seq, reason = problem
                return ChainStatus(False, checked, None, None, seq, reason)
            checked, expected_seq, expected_prev = checked + 1, expected_seq + 1, row.hash
        if checked == 0:
            return ChainStatus(True, 0, None, None)
        return ChainStatus(True, checked, checked, expected_prev)


def _first_problem(row: AuditEvent, expected_seq: int, expected_prev: str) -> tuple[int, str] | None:
    """(broken seq, reason) for the first check this row fails, or None if it is sound."""
    if row.seq != expected_seq:
        return expected_seq, f"expected seq {expected_seq} but found {row.seq}: a row is missing or out of order"
    if row.event_type not in EVENT_TYPE_VALUES:
        return row.seq, f"seq {row.seq} has an unknown event type"
    if row.prev_hash != expected_prev:
        return row.seq, f"prev_hash at seq {row.seq} does not match the previous row's hash"
    recomputed = compute_hash(row.prev_hash, row.seq, format_ts(row.timestamp), row.event_type, row.payload)
    if row.hash != recomputed:
        return row.seq, f"hash at seq {row.seq} does not match its contents: the row was altered"
    return None
