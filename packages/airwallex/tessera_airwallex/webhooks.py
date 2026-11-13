"""Airwallex webhook verification and parsing. Pure: no HTTP framework, no database.

The HTTP endpoint lives in apps/worker (the only app allowed to import this package); it calls these functions.

What the reference documents (docs/airwallex-notes.md, "Webhooks"):
* each delivery carries `x-timestamp` (Unix milliseconds) and `x-signature`;
* the signature is the hex HMAC-SHA256, keyed with the notification URL's secret, of the timestamp string followed by
  the raw request body, so the body must be verified exactly as received, before it is parsed;
* the page says to check the timestamp against a tolerance but gives no value, so the default here (5 minutes) is OUR
  choice, not the vendor's;
* the same event may be delivered more than once and out of order, so the event `id` is the deduplication key.
"""

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import SecretStr

from tessera_airwallex import _json
from tessera_airwallex._timeparse import parse_timestamp

SIGNATURE_HEADER = "x-signature"
TIMESTAMP_HEADER = "x-timestamp"
DEFAULT_TOLERANCE = timedelta(minutes=5)
MAX_BODY_BYTES = 1_000_000


class WebhookFormatError(ValueError):
    """The body is not an event Tessera can read. The message never includes the body."""


def compute_signature(secret: SecretStr, timestamp: str, body: bytes) -> str:
    """Hex HMAC-SHA256 of `timestamp` + `body` (what Airwallex sends in x-signature)."""
    key = secret.get_secret_value().encode()
    return hmac.new(key, timestamp.encode() + body, hashlib.sha256).hexdigest()


def verify_signature(secret: SecretStr, timestamp: str | None, signature: str | None, body: bytes) -> bool:
    """True only if both headers are present and the signature matches, compared in constant time."""
    if not timestamp or not signature or not secret.get_secret_value():
        return False
    expected = compute_signature(secret, timestamp, body)
    return hmac.compare_digest(expected.encode(), signature.strip().lower().encode())


def timestamp_is_fresh(timestamp: str | None, now: datetime, tolerance: timedelta = DEFAULT_TOLERANCE) -> bool:
    """True if `timestamp` (Unix milliseconds) is within `tolerance` of `now`, in either direction."""
    if not timestamp or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 16:
        return False
    sent = datetime.fromtimestamp(int(timestamp) / 1000, tz=UTC)
    return abs(now - sent) <= tolerance


@dataclass(frozen=True)
class WebhookEvent:
    id: str
    name: str
    created_at: datetime | None
    resource_id: str | None  # data.object.id: the transfer, conversion or card the event is about
    request_id: str | None  # data.object.request_id: ties the event to the request_id Tessera sent


def parse_event(body: bytes) -> WebhookEvent:
    """The event envelope. Only the fields Tessera uses are kept: a payload can carry bank details; none is stored."""
    if len(body) > MAX_BODY_BYTES:
        raise WebhookFormatError("body too large")
    try:
        data = _json.loads(body)
    except ValueError:
        raise WebhookFormatError("body is not valid JSON") from None
    if not isinstance(data, dict):
        raise WebhookFormatError("event is not an object")
    event_id, name = data.get("id"), data.get("name")
    if not isinstance(event_id, str) or not event_id or len(event_id) > 128:
        raise WebhookFormatError("event has no usable id")
    if not isinstance(name, str) or not name or len(name) > 128:
        raise WebhookFormatError("event has no usable name")
    payload = data.get("data")
    resource = payload.get("object") if isinstance(payload, dict) else None
    resource = resource if isinstance(resource, dict) else {}
    return WebhookEvent(
        id=event_id,
        name=name,
        created_at=_timestamp(data.get("created_at")),
        resource_id=_short(resource.get("id")),
        request_id=_short(resource.get("request_id")),
    )


def _short(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= 128 else None


def _timestamp(value: object) -> datetime | None:
    try:
        return parse_timestamp(value)
    except ValueError:
        return None  # created_at only orders events; an odd one must not make a genuine event unreadable
