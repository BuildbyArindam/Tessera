"""Importing this package registers the built-in rule types."""

from tessera_core.policy.builtin_rules import (
    autonomy_level_gate,
    beneficiary_allowlist,
    card_limit_cap,
    currency_concentration_cap,
    daily_cumulative_cap,
    early_payment_gate,
    forecast_confidence_gate,
    kill_switch,
    max_amount,
    quote_slippage_guard,
    untrusted_input_gate,
    velocity_limit,
    worst_case_loss_gate,
)

__all__ = [
    "autonomy_level_gate",
    "beneficiary_allowlist",
    "card_limit_cap",
    "currency_concentration_cap",
    "daily_cumulative_cap",
    "early_payment_gate",
    "forecast_confidence_gate",
    "kill_switch",
    "max_amount",
    "quote_slippage_guard",
    "untrusted_input_gate",
    "velocity_limit",
    "worst_case_loss_gate",
]
