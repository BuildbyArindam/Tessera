"""tessera_audit: the append-only, hash-chained audit log.

May import `tessera_core` (domain types) and `tessera_store` (the shared declarative base). Never imports the agent
or Airwallex packages. See log.py for what the chain does and does not prove.
"""

from tessera_audit.canonical import GENESIS_HASH, CanonicalError, canonical_json, compute_hash, format_ts
from tessera_audit.events import EVENT_TYPE_VALUES, AuditEventType
from tessera_audit.log import AuditLog, AuditRecord, ChainStatus
from tessera_audit.model import AuditEvent

__all__ = [
    "EVENT_TYPE_VALUES",
    "GENESIS_HASH",
    "AuditEvent",
    "AuditEventType",
    "AuditLog",
    "AuditRecord",
    "CanonicalError",
    "ChainStatus",
    "canonical_json",
    "compute_hash",
    "format_ts",
]
