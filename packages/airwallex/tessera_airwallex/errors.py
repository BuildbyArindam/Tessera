"""Typed errors raised by every AirwallexPort implementation.

Day 12 extends this list (AuthError, RateLimited, Unknown) when the real sandbox client lands.
Callers catch these, never vendor-specific exceptions.
"""


class AirwallexError(Exception):
    """Base class for all port errors."""


class InvalidRequestError(AirwallexError):
    """The request was malformed or violates a business rule (wrong currency, bad amount)."""


class NotFoundError(AirwallexError):
    """A referenced quote, beneficiary or card does not exist."""


class QuoteExpiredError(AirwallexError):
    """The FX quote is past its expiry."""


class InsufficientFundsError(AirwallexError):
    """The account balance cannot cover the request."""


class IdempotencyConflictError(AirwallexError):
    """An idempotency key was reused with a different request."""
