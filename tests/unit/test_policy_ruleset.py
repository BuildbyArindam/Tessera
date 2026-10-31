"""Day 6: YAML loading, validation at load time, and the ruleset hash (verification point (f))."""

from pathlib import Path

import pytest
from tessera_core.policy import (
    DEFAULT_RULES_DIR,
    RulesetError,
    RuleSpec,
    build_ruleset,
    load_ruleset,
    registered_types,
    ruleset_from_yaml_texts,
)

KILL = "id: kill_switch\ntype: kill_switch\nparams: {}\n"
CAP = """\
id: max_amount_per_action
type: max_amount_per_action
params:
  max_amount:
    USD: "5000.00"
    EUR: "5000.00"
"""


def texts(**overrides: str) -> dict[str, str]:
    base = {"kill_switch.yaml": KILL, "max_amount_per_action.yaml": CAP}
    base.update({k.replace("__", "."): v for k, v in overrides.items()})
    return base


def test_shipped_rules_load_and_register() -> None:
    rs = load_ruleset(DEFAULT_RULES_DIR)
    assert [r.rule_id for r in rs.rules] == [
        "beneficiary_allowlist",
        "card_limit_cap",
        "currency_concentration_cap",
        "daily_cumulative_cap",
        "forecast_confidence_gate",
        "kill_switch",
        "max_amount_per_action",
        "quote_slippage_guard",
        "velocity_limit",
        "worst_case_loss_gate",
    ]
    assert len(rs.hash) == 64 and int(rs.hash, 16) >= 0
    assert {r.rule_id for r in rs.rules} <= set(registered_types())


# ---- (f) the hash tracks meaning, not formatting ------------------------------------------
def test_editing_a_limit_changes_the_hash() -> None:
    base = ruleset_from_yaml_texts(texts()).hash
    edited = ruleset_from_yaml_texts(texts(max_amount_per_action__yaml=CAP.replace('"5000.00"', '"5000.01"', 1))).hash
    assert edited != base


def test_adding_removing_or_renaming_a_rule_changes_the_hash() -> None:
    base = ruleset_from_yaml_texts(texts()).hash
    assert ruleset_from_yaml_texts({"kill_switch.yaml": KILL}).hash != base
    renamed = CAP.replace("id: max_amount_per_action", "id: cap_two").replace("type: max", "type: max")
    assert ruleset_from_yaml_texts(texts(cap_two__yaml=renamed)).hash != base


def test_comments_whitespace_key_order_and_number_style_do_not_change_the_hash() -> None:
    base = ruleset_from_yaml_texts(texts()).hash
    reformatted = """\
# a new comment
params:
  max_amount:
    EUR: 5000
    USD: '5000'


type: max_amount_per_action
id: max_amount_per_action
"""
    assert ruleset_from_yaml_texts(texts(max_amount_per_action__yaml=reformatted)).hash == base


def test_hash_is_stable_across_loads_and_listing_order() -> None:
    a = ruleset_from_yaml_texts(texts()).hash
    b = ruleset_from_yaml_texts(dict(reversed(list(texts().items())))).hash
    assert a == b


# ---- configuration mistakes are load-time errors ------------------------------------------
def bad(**overrides: str) -> None:
    with pytest.raises(RulesetError):
        ruleset_from_yaml_texts(texts(**overrides))


def test_duplicate_yaml_key_is_rejected() -> None:
    bad(max_amount_per_action__yaml=CAP.replace('EUR: "5000.00"', 'USD: "99999.00"'))


def test_float_cap_is_rejected() -> None:
    bad(max_amount_per_action__yaml=CAP.replace('"5000.00"', "5000.5", 1))


@pytest.mark.parametrize(
    "value",
    ['"0"', '"-5"', '"abc"', '"NaN"', '"1e30"', "null", "[1]"],
)
def test_bad_cap_values_are_rejected(value: str) -> None:
    bad(max_amount_per_action__yaml=CAP.replace('"5000.00"', value, 1))


def test_unknown_currency_unknown_param_and_empty_caps_are_rejected() -> None:
    bad(max_amount_per_action__yaml=CAP.replace("EUR:", "XXX:"))
    bad(max_amount_per_action__yaml=CAP + "  extra_knob: 1\n")
    head = "id: max_amount_per_action\ntype: max_amount_per_action\n"
    bad(max_amount_per_action__yaml=head + "params:\n  max_amount: {}\n")
    bad(max_amount_per_action__yaml=head + "params: {}\n")


def test_unknown_rule_type_is_rejected() -> None:
    bad(max_amount_per_action__yaml=CAP.replace("type: max_amount_per_action", "type: eval_my_expression"))


def test_kill_switch_with_params_is_rejected() -> None:
    bad(kill_switch__yaml="id: kill_switch\ntype: kill_switch\nparams:\n  engaged: true\n")


def test_missing_kill_switch_is_rejected() -> None:
    with pytest.raises(RulesetError, match="kill_switch"):
        ruleset_from_yaml_texts({"max_amount_per_action.yaml": CAP})


def test_empty_ruleset_is_rejected() -> None:
    with pytest.raises(RulesetError):
        build_ruleset([])
    with pytest.raises(RulesetError):
        ruleset_from_yaml_texts({})


def test_duplicate_rule_ids_are_rejected() -> None:
    spec = RuleSpec(id="kill_switch", type="kill_switch")
    with pytest.raises(RulesetError, match="duplicate"):
        build_ruleset([spec, spec])


@pytest.mark.parametrize("text", ["- a\n- b\n", "just a string", "", "id: [unclosed", "id: x\nid: y\n"])
def test_non_mapping_or_broken_yaml_is_rejected(text: str) -> None:
    bad(kill_switch__yaml=text)


def test_file_name_must_match_rule_id() -> None:
    with pytest.raises(RulesetError, match="file name"):
        ruleset_from_yaml_texts({"other_name.yaml": KILL, "max_amount_per_action.yaml": CAP})


def test_bad_rule_id_pattern_is_rejected() -> None:
    bad(kill_switch__yaml=KILL.replace("id: kill_switch", "id: Kill-Switch"))


def test_python_object_tags_are_not_executed() -> None:
    bad(kill_switch__yaml="id: !!python/object/apply:os.system ['echo pwned']\ntype: kill_switch\n")


# ---- filesystem loader --------------------------------------------------------------------
def test_load_ruleset_from_directory(tmp_path: Path) -> None:
    for name, text in texts().items():
        (tmp_path / name).write_text(text)
    (tmp_path / "notes.txt").write_text("ignored")
    assert load_ruleset(tmp_path).hash == ruleset_from_yaml_texts(texts()).hash


def test_missing_or_empty_directory_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(RulesetError):
        load_ruleset(tmp_path / "nope")
    with pytest.raises(RulesetError):
        load_ruleset(tmp_path)
