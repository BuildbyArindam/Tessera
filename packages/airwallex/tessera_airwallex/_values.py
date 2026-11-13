"""Small value helpers shared by the sandbox adapter's FX, payout and card code."""

import hashlib
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from tessera_core.domain import Currency, Money

from tessera_airwallex.errors import InvalidRequestError

PLAIN_KEY = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
VENDOR_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")  # what a vendor id may look like before it goes into a URL
TOLERANCE = Decimal("0.01")  # amounts are 2 dp, so sell * rate may differ from buy by a cent
MAX_LIST_PAGES = 10


def request_id_for(idempotency_key: str) -> str:
    """Deterministic Airwallex `request_id` (max 64 chars) for a port idempotency key."""
    if not idempotency_key or not idempotency_key.strip():
        raise InvalidRequestError("idempotency_key must be non-empty")
    if PLAIN_KEY.match(idempotency_key):
        return idempotency_key
    return hashlib.sha256(idempotency_key.encode()).hexdigest()


def dec(value: object) -> Decimal:
    """Decimal from a parsed JSON number or string. Floats never reach here (see _json)."""
    if isinstance(value, bool | float) or value is None:
        raise ValueError("expected a Decimal, int or str amount")
    try:
        return Decimal(value) if isinstance(value, int | str) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal") from exc


def close(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) <= TOLERANCE + abs(b) * Decimal("1e-7")


def money(data: dict[str, Any], amount_key: str, currency_key: str) -> Money:
    return Money(amount=dec(data[amount_key]), currency=Currency(data[currency_key]))
