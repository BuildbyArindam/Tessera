"""Day 11: canonical JSON and the chain hash. Pure functions, no database."""

from __future__ import annotations

import math
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from tessera_audit import GENESIS_HASH, AuditEventType, CanonicalError, canonical_json, compute_hash, format_ts
from tessera_audit.canonical import MAX_PAYLOAD_BYTES, canonical_payload

TS = "2026-03-02T09:00:00.000000Z"


def test_key_order_and_whitespace_do_not_change_the_text() -> None:
    assert canonical_json({"b": 1, "a": [1, 2]}) == canonical_json({"a": [1, 2], "b": 1}) == '{"a":[1,2],"b":1}'


def test_money_is_written_as_a_string_never_a_float() -> None:
    assert canonical_json({"amount": Decimal("1200.50")}) == '{"amount":"1200.50"}'
    assert canonical_json({"amount": Decimal("1E+2")}) == '{"amount":"100"}'  # no exponent form


def test_non_ascii_is_escaped_so_the_bytes_are_stable() -> None:
    assert canonical_json({"n": "café ☃"}) == '{"n":"caf\\u00e9 \\u2603"}'
    canonical_json({"n": "\ud800"})  # a lone surrogate is escaped, not an encoding error


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_numbers_are_refused(bad: float) -> None:
    with pytest.raises(CanonicalError):
        canonical_json({"x": bad})
    with pytest.raises(CanonicalError):
        canonical_json({"x": Decimal("NaN")})


@pytest.mark.parametrize("bad", [{"s": {1, 2}}, {"b": b"raw"}, {"o": object()}, {1: "int key"}])
def test_values_that_cannot_be_written_exactly_are_refused_not_guessed(bad: dict[object, object]) -> None:
    with pytest.raises(CanonicalError):
        canonical_json(bad)


def test_timestamps_must_be_timezone_aware_and_the_same_instant_has_one_spelling() -> None:
    with pytest.raises(CanonicalError):
        format_ts(datetime(2026, 3, 2, 9, 0))
    utc = datetime(2026, 3, 2, 9, 0, 0, 123456, tzinfo=UTC)
    ist = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert format_ts(utc) == format_ts(ist) == "2026-03-02T09:00:00.123456Z"
    assert canonical_json({"d": date(2026, 3, 2), "t": ist}) == '{"d":"2026-03-02","t":"2026-03-02T09:00:00.123456Z"}'


def test_enums_are_written_by_value() -> None:
    assert canonical_json({"e": AuditEventType.APPROVED}) == '{"e":"approved"}'


def test_deeply_nested_and_oversized_payloads_are_refused() -> None:
    deep: dict[str, object] = {}
    node = deep
    for _ in range(40):
        node["k"] = {}
        node = node["k"]  # type: ignore[assignment]
    with pytest.raises(CanonicalError):
        canonical_json(deep)
    with pytest.raises(CanonicalError):
        canonical_payload({"blob": "x" * (MAX_PAYLOAD_BYTES + 1)})
    with pytest.raises(CanonicalError):
        canonical_payload(["not", "an", "object"])  # type: ignore[arg-type]


def _h(prev: str = GENESIS_HASH, seq: int = 1, ts: str = TS, typ: str = "approved", payload: str = '{"a":1}') -> str:
    return compute_hash(prev, seq, ts, typ, payload)


def test_hash_is_deterministic_and_64_hex() -> None:
    assert _h() == _h()
    assert len(_h()) == 64 and set(_h()) <= set("0123456789abcdef")


@pytest.mark.parametrize(
    "change",
    [
        {"prev": "1" * 64},
        {"seq": 2},
        {"ts": "2026-03-02T09:00:00.000001Z"},
        {"typ": "rejected"},
        {"payload": '{"a":2}'},
    ],
)
def test_the_hash_covers_every_field_so_relabelling_an_event_is_detected(change: dict[str, object]) -> None:
    assert _h(**change) != _h()  # type: ignore[arg-type]


def test_a_payload_cannot_smuggle_a_different_envelope() -> None:
    """Moving text between payload and type must not give the same hash."""
    assert _h(typ="approved", payload='{"a":1}') != _h(typ='approved","x":"', payload='{"a":1}')
