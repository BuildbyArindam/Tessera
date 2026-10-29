"""Currency import shim.

evalkit may depend on tessera_core (never the reverse). When tessera_core is
installed we use its Currency enum so there is exactly one definition. The
fallback below is value-identical and only exists so evalkit can be developed
and tested standalone.
"""

from __future__ import annotations

try:
    from tessera_core.domain import Currency
except ImportError:  # pragma: no cover - standalone fallback
    from enum import Enum

    class Currency(str, Enum):  # type: ignore[no-redef]
        USD = "USD"
        EUR = "EUR"
        GBP = "GBP"
        INR = "INR"
        SGD = "SGD"
        AUD = "AUD"


__all__ = ["Currency"]
