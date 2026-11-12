"""Typed errors raised by every AirwallexPort implementation.

Callers catch these, never vendor-specific exceptions. The mock and the sandbox client raise
the same classes for the same situations; tests/contract checks that.

Names: the brief lists AuthError, ValidationError, RateLimited, QuoteExpired, InsufficientFunds
and Unknown. Day 1 already had `InvalidRequestError`, `QuoteExpiredError` and
`InsufficientFundsError`, so those names stay (`ValidationError` is an alias of
`InvalidRequestError`) and the new ones follow the same `...Error` pattern.
"""


class AirwallexError(Exception):
    """Base class for all port errors.

    `status` (HTTP status) and `code` (vendor error code) are set by the sandbox client when
    known. They are never secrets; the message never contains a request body or credentials.
    """

    def __init__(self, message: str = "", *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


class InvalidRequestError(AirwallexError):
    """The request was malformed or violates a business rule (wrong currency, bad amount)."""


ValidationError = InvalidRequestError


class NotFoundError(AirwallexError):
    """A referenced quote, beneficiary or card does not exist."""


class QuoteExpiredError(AirwallexError):
    """The FX quote is past its expiry."""


class InsufficientFundsError(AirwallexError):
    """The account balance cannot cover the request."""


class IdempotencyConflictError(AirwallexError):
    """An idempotency key was reused with a different request."""


class AuthError(AirwallexError):
    """Credentials were rejected or lack permission (HTTP 401 or 403). Never retried."""


class RateLimitedError(AirwallexError):
    """HTTP 429. Never retried by the client; `retry_after` is seconds if the vendor sent it."""

    def __init__(
        self,
        message: str = "",
        *,
        status: int | None = 429,
        code: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, status=status, code=code)
        self.retry_after = retry_after


class UnknownError(AirwallexError):
    """Anything else: 5xx after retries, network failure, an unparseable response.

    For a state-changing call this means the outcome is not known. The caller must reconcile
    (look the action up by its idempotency key) before assuming it did or did not happen.
    """
