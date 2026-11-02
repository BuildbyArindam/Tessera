"""The audit_events table. Rows are written only by `AuditLog.append`; a database trigger (see the migration)
rejects UPDATE, DELETE and TRUNCATE, so history cannot be rewritten through SQL by the application's role."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, CheckConstraint, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from tessera_store.base import Base
from tessera_store.models import in_list

from tessera_audit.events import EVENT_TYPE_VALUES

HEX64 = "^[0-9a-f]{64}$"


class AuditEvent(Base):
    __tablename__ = "audit_events"

    seq: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(32))
    # The exact canonical JSON text that was hashed. TEXT, not JSONB: JSONB reorders keys and rewrites numbers.
    payload: Mapped[str] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64), unique=True)  # unique: a chain cannot fork
    hash: Mapped[str] = mapped_column(String(64), unique=True)

    __table_args__ = (
        CheckConstraint("seq >= 1", name="seq_positive"),
        CheckConstraint(in_list("event_type", EVENT_TYPE_VALUES), name="event_type"),
        CheckConstraint(f"prev_hash ~ '{HEX64}'", name="prev_hash_format"),
        CheckConstraint(f"hash ~ '{HEX64}'", name="hash_format"),
    )
