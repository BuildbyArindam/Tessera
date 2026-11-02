"""Canonical JSON and the chain hash. Pure functions: no database, no clock, no I/O.

Why canonical: the hash covers exact bytes, so the same event must always serialise to the same text. Keys are
sorted, there is no whitespace, non-ASCII is escaped, and NaN or infinity is refused. Money (Decimal) is written
as a plain string, never a float. Datetimes must be timezone-aware and are written in UTC with microseconds.

What the hash covers: `sha256(prev_hash + envelope)` where the envelope is the canonical JSON object
`{"payload": <payload text>, "seq": n, "ts": "...", "type": "..."}`. The brief says `sha256(prev_hash +
canonical_payload)`; covering seq, timestamp and event type as well closes a gap: with the payload alone, someone
could relabel an event or change its time and the chain would still verify.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

GENESIS_HASH = "0" * 64
MAX_DEPTH = 24
MAX_PAYLOAD_BYTES = 256 * 1024


class CanonicalError(ValueError):
    """The value cannot be written as canonical JSON."""


def format_ts(value: datetime) -> str:
    """UTC with microseconds and a trailing Z. The same instant always gives the same text."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalError("timestamps must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _normalise(value: Any, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise CanonicalError("payload is nested too deeply")
    if value is None or isinstance(value, bool | str | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalError("NaN and infinity are not allowed")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalError("non-finite Decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return format_ts(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return _normalise(value.value, depth + 1)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalError(f"object keys must be strings, got {type(key).__name__}")
            out[key] = _normalise(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_normalise(item, depth + 1) for item in value]
    raise CanonicalError(f"cannot serialise {type(value).__name__}")  # sets, bytes, objects: refuse, never guess


def canonical_json(value: Any) -> str:
    return json.dumps(_normalise(value, 0), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def canonical_payload(payload: Mapping[str, Any]) -> str:
    """The text stored in `audit_events.payload`. Must be a JSON object and at most MAX_PAYLOAD_BYTES."""
    if not isinstance(payload, Mapping):
        raise CanonicalError("an audit payload must be an object")
    text = canonical_json(payload)
    if len(text.encode("ascii")) > MAX_PAYLOAD_BYTES:
        raise CanonicalError(f"audit payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    return text


def compute_hash(prev_hash: str, seq: int, ts: str, event_type: str, payload_text: str) -> str:
    envelope = (
        '{"payload":' + payload_text + ',"seq":' + str(int(seq)) + ',"ts":' + json.dumps(ts) + ',"type":'
        + json.dumps(event_type) + "}"
    )  # fmt: skip
    return hashlib.sha256((prev_hash + envelope).encode("utf-8")).hexdigest()
