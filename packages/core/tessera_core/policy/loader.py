"""Loading rules from YAML.

`ruleset_from_yaml_texts` is pure (text in, Ruleset out). `load_ruleset` is the one function in
the policy package that touches the filesystem, and it only reads the rule files.

The YAML loader is stricter than stock `yaml.safe_load`, because a silently-misread safety limit
is worse than a crash:
- duplicate mapping keys are an error (stock YAML keeps the last one and says nothing);
- amounts must be quoted strings or integers; a float such as 5000.5 is rejected by Money;
- each file holds exactly one rule, and the file name must equal the rule id.
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from yaml.constructor import ConstructorError

from tessera_core.policy.ruleset import Ruleset, RulesetError, RuleSpec, build_ruleset

DEFAULT_RULES_DIR = Path(__file__).parent / "rules"
_SUFFIXES = (".yaml", ".yml")


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate keys instead of letting the last one win."""


def _construct_unique_mapping(loader: _StrictLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in seen
        except TypeError:
            raise ConstructorError(None, None, "mapping keys must be simple scalars", key_node.start_mark) from None
        if duplicate:
            raise ConstructorError(None, None, f"duplicate key {key!r}", key_node.start_mark)
        seen.add(key)
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def parse_rule_yaml(text: str, source: str) -> RuleSpec:
    try:
        data = yaml.load(text, Loader=_StrictLoader)  # noqa: S506 - _StrictLoader is a SafeLoader
    except yaml.YAMLError as exc:
        raise RulesetError(f"{source}: invalid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise RulesetError(f"{source}: a rule file must contain a single mapping")
    try:
        spec = RuleSpec.model_validate(data)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors())
        raise RulesetError(f"{source}: {problems}") from None
    stem = source.rsplit("/", 1)[-1].rsplit(".", 1)[0]
    if stem != spec.id:
        raise RulesetError(f"{source}: file name must match the rule id {spec.id!r}")
    return spec


def ruleset_from_yaml_texts(texts: Mapping[str, str]) -> Ruleset:
    """Build a Ruleset from {file name: YAML text}. Pure; used by tests and by load_ruleset."""
    return build_ruleset([parse_rule_yaml(text, name) for name, text in sorted(texts.items())])


def load_ruleset(directory: Path = DEFAULT_RULES_DIR) -> Ruleset:
    if not directory.is_dir():
        raise RulesetError(f"rules directory not found: {directory}")
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.suffix in _SUFFIXES)
    if not files:
        raise RulesetError(f"no rule files (*.yaml) found in {directory}")
    return ruleset_from_yaml_texts({p.name: p.read_text(encoding="utf-8") for p in files})
