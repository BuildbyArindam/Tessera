"""Canonical JSON, hashing and strict parsing of model output.

`canonical_json` is how the planner hashes its context and how proposal ids are derived, so the same
state always produces the same bytes. `parse_strict_json` is the only way model text becomes data: it
refuses the JSON that different parsers read differently (duplicate keys, NaN/Infinity) and anything
that is not a single JSON object, so what the schema validates is exactly what the model said.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

MAX_OUTPUT_CHARS = 50_000

# The model is allowed to wrap its whole answer in one ```json fence and nothing else.
_FENCE = re.compile(r"\A```(?:json)?[ \t]*\n(?P<body>.*)\n```\Z", re.DOTALL)


class OutputFormatError(ValueError):
    """Model output is not a single, unambiguous JSON object. The message is safe to show the model."""


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, ASCII only, NaN rejected: one value, one byte string."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def sha256_hex(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise OutputFormatError("a JSON object repeats a key")
        out[key] = value
    return out


def _reject_constant(name: str) -> Any:
    raise OutputFormatError(f"{name} is not valid JSON")


def parse_strict_json(text: str) -> dict[str, Any]:
    """Parse model output into one JSON object, or raise OutputFormatError."""
    if not isinstance(text, str) or not text.strip():
        raise OutputFormatError("the reply was empty; return one JSON object")
    if len(text) > MAX_OUTPUT_CHARS:
        raise OutputFormatError("the reply is too long")
    body = text.strip()
    fenced = _FENCE.match(body)
    if fenced is not None:
        body = fenced.group("body").strip()
    try:
        value = json.loads(body, object_pairs_hook=_no_duplicates, parse_constant=_reject_constant)
    except json.JSONDecodeError:
        raise OutputFormatError("the reply is not valid JSON; return only one JSON object") from None
    except RecursionError:
        raise OutputFormatError("the JSON is nested too deeply") from None
    if not isinstance(value, dict):
        raise OutputFormatError("the reply must be a JSON object")
    return value
