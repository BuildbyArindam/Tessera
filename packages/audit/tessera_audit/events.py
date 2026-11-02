"""The audit event types. The database holds the same list as a CHECK constraint (see model.py)."""

from enum import StrEnum


class AuditEventType(StrEnum):
    FORECAST_MADE = "forecast_made"
    PROPOSAL_CREATED = "proposal_created"
    POLICY_DECISION = "policy_decision"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTION_SUBMITTED = "execution_submitted"
    EXECUTION_RESULT = "execution_result"
    WEBHOOK_RECEIVED = "webhook_received"
    AUTONOMY_CHANGED = "autonomy_changed"


EVENT_TYPE_VALUES: tuple[str, ...] = tuple(e.value for e in AuditEventType)
