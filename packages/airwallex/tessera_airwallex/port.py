"""The contract between Tessera and Airwallex.

Only the worker may call an implementation of this port, and only after tessera_core.policy
has approved the action. Two implementations exist: mock.py (tests, demo) and sandbox.py (Day 12).

Conventions:
- Every method that changes state takes a keyword-only `idempotency_key`. Replaying the same
  key with the same request returns the original result and has no further effect; reusing a
  key for a different request raises IdempotencyConflictError.
- create_fx_quote and get_balances are read-like and take no key. (A quote reserves nothing.)
- Account details are never passed as plain str: `SecretStr` keeps them out of logs and reprs.
"""

from abc import ABC, abstractmethod

from pydantic import SecretStr

from tessera_airwallex.models import BeneficiaryRef, Card, ConversionResult, Transfer
from tessera_core.domain import Balance, Currency, FxQuote, Money


class AirwallexPort(ABC):
    @abstractmethod
    def get_balances(self) -> list[Balance]:
        """Current available balance per currency."""

    @abstractmethod
    def create_fx_quote(self, *, sell: Money, buy_currency: Currency) -> FxQuote:
        """Quote converting `sell` into `buy_currency`. Executable until `expires_at`."""

    @abstractmethod
    def execute_conversion(self, *, quote_id: str, idempotency_key: str) -> ConversionResult:
        """Execute a previously issued quote exactly once."""

    @abstractmethod
    def create_beneficiary(
        self,
        *,
        name: str,
        currency: Currency,
        account_identifier: SecretStr,
        idempotency_key: str,
    ) -> BeneficiaryRef:
        """Register a payout recipient. `account_identifier` is sensitive bank detail."""

    @abstractmethod
    def create_transfer(
        self,
        *,
        beneficiary_id: str,
        amount: Money,
        reference: str,
        idempotency_key: str,
    ) -> Transfer:
        """Pay a registered beneficiary. `amount` must be in the beneficiary's currency."""

    @abstractmethod
    def create_card(self, *, label: str, monthly_limit: Money, idempotency_key: str) -> Card:
        """Issue a virtual card with a monthly spend ceiling."""

    @abstractmethod
    def set_card_limits(self, *, card_id: str, monthly_limit: Money, idempotency_key: str) -> Card:
        """Change the monthly spend ceiling of an existing card."""
