"""Settings the port does not carry but Airwallex requires for payouts and cards.

The port (port.py) deliberately has no `reason`, `bank_country_code`, `cardholder_id` or `created_by`: they are
operator configuration, not something a proposal (and so the model) may choose. They come from the environment.
Every default here is an ASSUMPTION marked UNVERIFIED in docs/airwallex-notes.md; there is no default for the
transfer reason or the cardholder, so a missing setting stops the call with InvalidRequestError instead of guessing.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from tessera_core.domain import Currency

# The country of the bank for each currency Tessera trades. EUR has no single country, so it has no default.
DEFAULT_BANK_COUNTRY: dict[Currency, str] = {
    Currency.USD: "US",
    Currency.GBP: "GB",
    Currency.INR: "IN",
    Currency.SGD: "SG",
    Currency.AUD: "AU",
}


@dataclass(frozen=True)
class PayoutConfig:
    transfer_reason: str | None = None  # AIRWALLEX_TRANSFER_REASON: required by the transfer endpoint
    transfer_methods: tuple[str, ...] = ("LOCAL",)  # AIRWALLEX_TRANSFER_METHODS: "SWIFT" and/or "LOCAL"
    bank_country: Mapping[Currency, str] = field(default_factory=lambda: dict(DEFAULT_BANK_COUNTRY))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "PayoutConfig":
        import os

        source = env if env is not None else os.environ
        countries = dict(DEFAULT_BANK_COUNTRY)
        for currency in Currency:
            value = source.get(f"AIRWALLEX_BANK_COUNTRY_{currency.value}")
            if value:
                countries[currency] = value.strip().upper()
        methods = tuple(m.strip().upper() for m in source.get("AIRWALLEX_TRANSFER_METHODS", "").split(",") if m.strip())
        return cls(
            transfer_reason=source.get("AIRWALLEX_TRANSFER_REASON") or None,
            transfer_methods=methods or ("LOCAL",),
            bank_country=countries,
        )


@dataclass(frozen=True)
class CardConfig:
    cardholder_id: str | None = None  # AIRWALLEX_CARDHOLDER_ID: required by the card endpoint
    created_by: str | None = None  # AIRWALLEX_CARD_CREATED_BY: required by the card endpoint
    purpose: str = "COMMERCIAL"  # program.purpose: COMMERCIAL or CONSUMER
    is_personalized: bool = False  # required by the card endpoint; its meaning is not explained on the page read

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CardConfig":
        import os

        source = env if env is not None else os.environ
        return cls(
            cardholder_id=source.get("AIRWALLEX_CARDHOLDER_ID") or None,
            created_by=source.get("AIRWALLEX_CARD_CREATED_BY") or None,
            purpose=(source.get("AIRWALLEX_CARD_PURPOSE") or "COMMERCIAL").upper(),
        )
