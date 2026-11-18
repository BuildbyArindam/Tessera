"""The README number check (scripts/check_readme_numbers.py): it must fail on a made-up number, not only pass."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_readme_numbers.py"
_spec = importlib.util.spec_from_file_location("check_readme_numbers", SCRIPT)
assert _spec is not None and _spec.loader is not None
mod = importlib.util.module_from_spec(_spec)
sys.modules["check_readme_numbers"] = mod
_spec.loader.exec_module(mod)

CORPUS = """
LightGBM WAPE 0.413 [0.352, 0.474], coverage 0.724 [0.559, 0.876] over 1,452 rows.
Tessera saved 0.50 [-0.02, 1.35] EUR per 10,000 units; block rate 91.7%.
"""


def problems(readme: str, allowed: dict[str, str] | None = None) -> list[str]:
    found, _ = mod.check(readme, CORPUS, allowed or {})
    return found


def test_numbers_that_are_in_the_report_pass() -> None:
    assert problems("WAPE 0.413 and coverage 0.724 over 1,452 rows, block rate 91.7%.") == []


def test_a_number_that_is_not_in_the_report_fails() -> None:
    found = problems("WAPE 0.419.")
    assert len(found) == 1 and "0.419" in found[0]


def test_the_same_value_written_differently_fails() -> None:
    # 0.5 is not 0.50: the README must quote the report as written, so nobody rounds a result in their favour.
    assert problems("saved 0.5 EUR") != []


def test_an_interval_must_match_its_point_estimate() -> None:
    assert problems("saved 0.50 [-0.02, 1.35] EUR") == []
    found = problems("saved 0.50 [-0.02, 1.50] EUR")
    assert any("interval" in f for f in found)


def test_a_percentage_is_checked() -> None:
    assert problems("block rate 92.0%") != []


def test_small_integers_are_not_checked_but_large_ones_are() -> None:
    assert problems("13 rules and 5 commands") == []
    assert problems("1,219 tests") != []


def test_the_allowlist_needs_a_reason_and_works(tmp_path: Path) -> None:
    assert problems("1,219 tests", {"1219": "pytest count, 2026-11-18"}) == []
    bad = tmp_path / "allow.txt"
    bad.write_text("1219\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        mod.load_allowlist(bad)


def test_code_dates_and_links_are_ignored() -> None:
    readme = (
        "```\nmake test --seed 99999\n```\n"
        "See `port 5433`, 2026-11-18 and [the report](docs/x-123456.md) <!-- 777.7 -->"
    )
    assert problems(readme) == []


def test_the_shipped_readme_passes_against_the_shipped_reports() -> None:
    assert mod.main([]) == 0
