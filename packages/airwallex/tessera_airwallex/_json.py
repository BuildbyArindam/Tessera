"""JSON helpers that keep money away from floats (Master Context rule 5).

Airwallex sends amounts and rates as JSON numbers, and `response.json()` would turn them into
floats. `loads` parses every non-integer number straight to Decimal instead. Quote creation
wants a JSON *number* for the amount; `dumps` writes a Decimal as a bare number without ever
passing through a float.

How `dumps` does it: `json.dumps` cannot emit a raw number for a custom type, so a Decimal is
first written as a string carrying a marker, and the quotes around marked strings are removed
afterwards. The marker includes a random per-process nonce, so text in the payload (an invoice
note, say) can never be mistaken for one: it cannot know the nonce.
"""

import json
import re
import secrets
from decimal import Decimal
from typing import Any

_MARK = f"dec-{secrets.token_hex(12)}:"
_MARKED = re.compile(r'"' + re.escape(_MARK) + r'(-?[0-9]+(?:\.[0-9]+)?)"')


def loads(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal)


def _default(value: object) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("cannot serialise a non-finite Decimal")
        return _MARK + format(value, "f")
    raise TypeError(f"cannot serialise {type(value).__name__}")


def dumps(payload: Any) -> str:
    """Serialise `payload`; Decimal values become bare JSON numbers (plain notation, no exponent)."""
    return _MARKED.sub(lambda m: m.group(1), json.dumps(payload, default=_default, separators=(",", ":")))
