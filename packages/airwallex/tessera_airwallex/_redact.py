"""Redaction for anything that may be logged or put in an error message.

Two layers: sensitive KEYS are blanked wherever they appear in a structure, and known secret
VALUES (the API key, the current token) are scrubbed from any string, so a secret echoed back in
an unexpected place is still removed.
"""

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = frozenset(
    {
        "x-api-key",
        "x-client-id",
        "x-login-as",
        "authorization",
        "token",
        "api_key",
        "client_secret",
        "account_number",
        "account_identifier",
        "iban",
        "routing_number",
        "sort_code",
        "swift_code",
        "card_number",
        "cvv",
    }
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def redact(value: Any, secrets: Iterable[str] = ()) -> Any:
    """Return a copy of `value` that is safe to log."""
    known = [s for s in secrets if s]
    if isinstance(value, dict):
        return {k: (REDACTED if str(k).lower() in _SENSITIVE_KEYS else redact(v, known)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v, known) for v in value]
    if isinstance(value, str):
        return scrub(value, known)
    return value


def scrub(text: str, secrets: Iterable[str] = ()) -> str:
    """Remove known secret values and bearer tokens from a string."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return _BEARER.sub("Bearer " + REDACTED, text)
