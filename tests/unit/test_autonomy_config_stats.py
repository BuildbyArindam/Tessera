"""Autonomy config (strict YAML, hashed), the bootstrap bound, shadow detection, and "thresholds live in YAML"."""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from policy_helpers import make_context, make_proposal, only
from tessera_core.autonomy import (
    DEFAULT_CONFIG_PATH,
    AutonomyConfigError,
    AutonomyLevel,
    autonomy_config_from_text,
    bootstrap_lower_bound,
    is_shadow_decision,
    load_autonomy_config,
)
from tessera_core.domain import ActionType, PolicyDecision, Verdict
from tessera_core.policy import evaluate

YAML = DEFAULT_CONFIG_PATH.read_text(encoding="utf-8")


# ---- config --------------------------------------------------------------------------------
def test_the_shipped_config_loads() -> None:
    cfg = load_autonomy_config()
    assert cfg.max_level is AutonomyLevel.L3 and cfg.promotion.min_agreement == Decimal("0.8000")


def test_the_hash_tracks_meaning_not_formatting() -> None:
    base = load_autonomy_config().hash
    assert autonomy_config_from_text("# a comment\n" + YAML).hash == base
    assert autonomy_config_from_text(YAML.replace('min_agreement: "0.8000"', 'min_agreement: "0.8500"', 1)).hash != base
    assert autonomy_config_from_text(YAML.replace("seed: 14", "seed: 15", 1)).hash != base


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('min_agreement: "0.8000"', "min_agreement: 0.8", "float"),
        ('min_agreement: "0.8000"', 'min_agreement: "1.5000"', "at most 1"),
        ('min_agreement: "0.8000"', 'min_agreement: "0.00001"', "decimal places"),
        ("evidence_days: 30", "evidence_days: 0", "greater than or equal to 1"),
        ("evidence_days: 30", 'evidence_days: "30"', "evidence_days"),
        ("max_level: L3", "max_level: L4", "max_level"),
        ("levels_down: 3\n  on_kill_switch", "levels_down: 4\n  on_kill_switch", "levels_down"),
        ("resamples: 2000", "resamples: 5", "resamples"),
        ("seed: 14", "seed: 14\n  extra: 1", "extra"),
        ("window:\n", "window:\n  evidence_days: 31\n", "duplicate key"),
    ],
)
def test_a_bad_config_does_not_load(old: str, new: str, message: str) -> None:
    with pytest.raises(AutonomyConfigError, match=message):
        autonomy_config_from_text(YAML.replace(old, new, 1))


def test_a_missing_section_does_not_load() -> None:
    with pytest.raises(AutonomyConfigError, match="demotion"):
        autonomy_config_from_text(YAML[: YAML.index("# Automatic demotion")])


def test_a_missing_file_does_not_load(tmp_path: Path) -> None:
    with pytest.raises(AutonomyConfigError, match="not found"):
        load_autonomy_config(tmp_path / "nope.yaml")


# ---- the bootstrap bound -------------------------------------------------------------------
D = Decimal
SPREAD = [D("10.00"), D("15.00"), D("11.00"), D("14.00"), D("12.00"), D("13.00")] * 4


def test_the_bound_is_repeatable_for_a_seed_and_differs_across_seeds() -> None:
    a = bootstrap_lower_bound(SPREAD, resamples=1000, confidence=D("0.95"), seed=1)
    assert a == bootstrap_lower_bound(SPREAD, resamples=1000, confidence=D("0.95"), seed=1)
    assert a != bootstrap_lower_bound(SPREAD, resamples=1000, confidence=D("0.95"), seed=2)


def test_identical_samples_give_that_value_as_the_bound() -> None:
    assert bootstrap_lower_bound([D("7.25")] * 10, resamples=500, confidence=D("0.95"), seed=3) == D("7.25")


def test_the_bound_sits_below_the_mean_and_rises_as_the_confidence_falls() -> None:
    mean = sum(SPREAD, D(0)) / len(SPREAD)
    strict = bootstrap_lower_bound(SPREAD, resamples=2000, confidence=D("0.99"), seed=5)
    loose = bootstrap_lower_bound(SPREAD, resamples=2000, confidence=D("0.80"), seed=5)
    assert strict <= loose <= mean


def test_more_data_tightens_the_bound() -> None:
    few = bootstrap_lower_bound(SPREAD[:6], resamples=2000, confidence=D("0.95"), seed=5)
    many = bootstrap_lower_bound(SPREAD * 4, resamples=2000, confidence=D("0.95"), seed=5)
    assert few < many


@pytest.mark.parametrize(
    "kwargs",
    [
        {"resamples": 0, "confidence": D("0.95")},
        {"resamples": 10, "confidence": D("1")},
        {"resamples": 10, "confidence": D("0")},
    ],
)
def test_a_bad_bootstrap_setting_is_an_error(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        bootstrap_lower_bound(SPREAD, seed=1, **kwargs)  # type: ignore[arg-type]


def test_one_sample_has_no_spread_to_resample() -> None:
    with pytest.raises(ValueError, match="at least two"):
        bootstrap_lower_bound([D("1.00")], resamples=100, confidence=D("0.95"), seed=1)


# ---- shadow decisions ----------------------------------------------------------------------
def test_a_proposal_stopped_only_by_the_level_is_a_shadow_decision() -> None:
    rs = only("autonomy_level_gate.yaml")
    d = evaluate(make_proposal(ActionType.CONVERT, "100.00", "USD"), make_context(autonomy_level=AutonomyLevel.L0), rs)
    assert d.verdict is Verdict.BLOCK and is_shadow_decision(d)


def test_the_same_proposal_at_a_level_that_allows_it_is_not() -> None:
    rs = only("autonomy_level_gate.yaml")
    d = evaluate(make_proposal(ActionType.CONVERT, "100.00", "USD"), make_context(autonomy_level=AutonomyLevel.L1), rs)
    assert d.verdict is Verdict.APPROVE and not is_shadow_decision(d)


def test_a_block_by_another_rule_too_is_not_a_shadow_decision() -> None:
    d = PolicyDecision(
        verdict=Verdict.BLOCK, reasons=("a: x", "b: y"), rule_ids=("autonomy_level_gate", "max_amount_per_action")
    )
    assert not is_shadow_decision(d)


def test_a_policy_error_is_never_a_shadow_decision() -> None:
    d = PolicyDecision(
        verdict=Verdict.BLOCK,
        reasons=("policy_error", "autonomy_level_gate: missing context"),
        rule_ids=("autonomy_level_gate",),
    )
    assert not is_shadow_decision(d)


def test_an_escalation_is_not_a_shadow_decision() -> None:
    d = PolicyDecision(
        verdict=Verdict.ESCALATE, reasons=("autonomy_level_gate: over cap",), rule_ids=("autonomy_level_gate",)
    )
    assert not is_shadow_decision(d)


# ---- thresholds live in YAML ---------------------------------------------------------------
LOGIC = ("controller.py", "stats.py", "shadow.py", "evidence.py")
ALLOWED = {0, 1}  # an empty count, a single step; anything else belongs in autonomy.yaml


@pytest.mark.parametrize("name", LOGIC)
def test_no_threshold_is_hard_coded_in_the_autonomy_logic(name: str) -> None:
    tree = ast.parse((DEFAULT_CONFIG_PATH.parent / name).read_text(encoding="utf-8"))
    numbers = [
        (n.value, n.lineno)
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, int | float) and not isinstance(n.value, bool)
    ]
    # Two structural allowances, neither a decision threshold: evidence.py's sanity bound on an observation (10 ** 9)
    # and stats.py's "a bootstrap needs two samples" (there is no spread to resample from one).
    allowed = ALLOWED | {"evidence.py": {9, 10}, "stats.py": {2}}.get(name, set())
    assert [(v, line) for v, line in numbers if v not in allowed] == []
