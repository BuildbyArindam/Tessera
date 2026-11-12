"""Map an Airwallex HTTP error response to a typed port error.

Only status codes and error codes written down in docs/airwallex-notes.md are matched. The
error body shape itself is UNVERIFIED there, so parsing is defensive: a top-level string `code`
and `message` are used if present, and the HTTP status alone decides otherwise.
"""

from collections.abc import Iterable

import httpx

from tessera_airwallex import _json
from tessera_airwallex._redact import scrub
from tessera_airwallex.errors import (
    AirwallexError,
    AuthError,
    InsufficientFundsError,
    InvalidRequestError,
    NotFoundError,
    QuoteExpiredError,
    RateLimitedError,
    UnknownError,
)

QUOTE_EXPIRED_CODES = frozenset({"expired_quote"})
INSUFFICIENT_FUNDS_CODES = frozenset({"insufficient_funds", "credit_limit_exceeded"})
NOT_FOUND_CODES = frozenset({"not_found", "invalid_quote_id", "does_not_exist"})
# Documented 400 codes that mean "this request is wrong", as opposed to an unrecognised failure
# (which could be the vendor rejecting a duplicate request_id).
KNOWN_INVALID_CODES = frozenset(
    {
        "amount_below_limit",
        "amount_above_limit",
        "conflict_original_ccy",
        "field_required",
        "invalid_argument",
        "invalid_conversion_date",
        "invalid_settlement_date",
        "invalid_endpoint",
        "unsupported_currency",
        "validation_error",
        "invalid_parameter",
        "operation_failed",
        "term_agreement_is_required",
        "incorrect_version",
    }
)
_MAX_MESSAGE = 200


def error_fields(response: httpx.Response, secrets: Iterable[str] = ()) -> tuple[str | None, str]:
    """(vendor code, short scrubbed message) from an error response, tolerating any body."""
    try:
        data = _json.loads(response.text)
    except ValueError:
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    code = data.get("code")
    message = data.get("message")
    return (
        code if isinstance(code, str) else None,
        scrub(message, secrets)[:_MAX_MESSAGE] if isinstance(message, str) else "",
    )


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    try:
        return max(0.0, float(raw)) if raw is not None else None
    except ValueError:
        return None


def map_error(response: httpx.Response, *, what: str, secrets: Iterable[str] = ()) -> AirwallexError:
    """Build the port error for a non-2xx response. `what` is e.g. 'POST /api/v1/...'."""
    status = response.status_code
    code, message = error_fields(response, secrets)
    text = f"{what} -> HTTP {status}" + (f" {code}" if code else "") + (f": {message}" if message else "")
    if status in (401, 403):
        return AuthError(text, status=status, code=code)
    if status == 429:
        return RateLimitedError(text, code=code, retry_after=_retry_after(response))
    if code in QUOTE_EXPIRED_CODES:
        return QuoteExpiredError(text, status=status, code=code)
    if code in INSUFFICIENT_FUNDS_CODES:
        return InsufficientFundsError(text, status=status, code=code)
    if status == 404 or code in NOT_FOUND_CODES:
        return NotFoundError(text, status=status, code=code)
    if status in (400, 409, 422):
        return InvalidRequestError(text, status=status, code=code)
    return UnknownError(text, status=status, code=code)


def needs_reconcile(exc: AirwallexError) -> bool:
    """True when a failed create call might still have created something.

    An unknown outcome, or a 4xx we cannot classify (the documented behaviour for a duplicate
    request_id is only "rejected"), means: look the conversion up before giving up.
    """
    if isinstance(exc, UnknownError):
        return True
    return isinstance(exc, InvalidRequestError) and exc.code not in KNOWN_INVALID_CODES
