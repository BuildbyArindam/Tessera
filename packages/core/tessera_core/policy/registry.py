"""Registry mapping a YAML `type` string to its Rule class.

Registration happens at import time (a decorator on each rule class). There is no dynamic
lookup by path or name, no eval, no plugin loading: a type the registry has never seen is a
RulesetError at load time.
"""

from typing import Any, TypeVar

from tessera_core.policy.rule import Rule

_REGISTRY: dict[str, type[Rule[Any]]] = {}

R = TypeVar("R", bound=type[Rule[Any]])


class UnknownRuleTypeError(KeyError):
    pass


def register_rule(cls: R) -> R:
    name = cls.type_name
    if name in _REGISTRY and _REGISTRY[name] is not cls:
        raise ValueError(f"rule type {name!r} is already registered")
    _REGISTRY[name] = cls
    return cls


def get_rule_class(type_name: str) -> type[Rule[Any]]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "(none)"
        raise UnknownRuleTypeError(f"unknown rule type {type_name!r}; registered: {known}") from None


def registered_types() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))
