"""A Ruleset is the validated, ordered, hashed set of rules the engine runs.

Building one is where configuration mistakes are caught, loudly, at load time rather than as a
surprise at execution time:
- unknown rule `type`, duplicate ids, or params that fail the rule's own config model;
- an empty ruleset (which would APPROVE everything);
- a ruleset with no `kill_switch` rule (an operator must always be able to stop everything).

`ruleset_hash` covers the *meaning* of the rules (id, type, type version, validated params in a
canonical form), not the bytes of the YAML, so a comment edit does not change it but a limit does.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import Field, JsonValue, ValidationError

from tessera_core.domain.base import DomainModel
from tessera_core.policy.registry import UnknownRuleTypeError, get_rule_class
from tessera_core.policy.rule import Rule

HASH_FORMAT_VERSION = 1
REQUIRED_RULE_TYPES: tuple[str, ...] = ("kill_switch",)
RULE_ID_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"


class RulesetError(ValueError):
    """The rule configuration is invalid. Raised at load time, never during evaluate()."""


class RuleSpec(DomainModel):
    id: str = Field(pattern=RULE_ID_PATTERN)
    type: str = Field(pattern=RULE_ID_PATTERN)
    params: dict[str, JsonValue] = Field(default_factory=dict)


@dataclass(frozen=True)
class Ruleset:
    rules: tuple[Rule[Any], ...]  # sorted by rule_id
    hash: str


def _canonical_hash(rules: Sequence[Rule[Any]]) -> str:
    payload = {
        "format": HASH_FORMAT_VERSION,
        "rules": [
            {
                "id": r.rule_id,
                "type": r.type_name,
                "version": r.type_version,
                "params": r.config.model_dump(mode="json"),
            }
            for r in rules
        ],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def _build_rule(spec: RuleSpec) -> Rule[Any]:
    try:
        cls = get_rule_class(spec.type)
    except UnknownRuleTypeError as exc:
        raise RulesetError(f"rule {spec.id!r}: {exc.args[0]}") from None
    try:
        config = cls.config_model.model_validate(spec.params)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise RulesetError(f"rule {spec.id!r} has invalid params: {problems}") from None
    return cls(spec.id, config)


def build_ruleset(specs: Sequence[RuleSpec]) -> Ruleset:
    if not specs:
        raise RulesetError("a ruleset must contain at least one rule")
    ids = [s.id for s in specs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise RulesetError(f"duplicate rule ids: {', '.join(duplicates)}")

    rules = tuple(sorted((_build_rule(s) for s in specs), key=lambda r: r.rule_id))
    present = {r.type_name for r in rules}
    missing = [t for t in REQUIRED_RULE_TYPES if t not in present]
    if missing:
        raise RulesetError(f"ruleset is missing required rule type(s): {', '.join(missing)}")
    return Ruleset(rules=rules, hash=_canonical_hash(rules))
