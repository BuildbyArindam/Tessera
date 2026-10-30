"""Importing this package registers the built-in rule types. Day 7 adds the rest here."""

from tessera_core.policy.builtin_rules import kill_switch, max_amount

__all__ = ["kill_switch", "max_amount"]
