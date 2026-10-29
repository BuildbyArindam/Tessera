"""Currencies Tessera understands.

All six are 2-decimal currencies, which is why Money quantizes to 2 dp.
Adding a 0- or 3-decimal currency (JPY, KWD) means making minor units a property of
Currency first; do not just widen the quantum.
"""

from enum import StrEnum


class Currency(StrEnum):
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    INR = "INR"
    SGD = "SGD"
    AUD = "AUD"
