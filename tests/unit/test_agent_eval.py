"""Day 19: agreement metrics, the scenario files, the scripted planners and the policy-containment claim."""

from __future__ import annotations

import csv
import importlib.util
import io
from decimal import Decimal
from pathlib import Path

import pytest
from tessera_core.policy import load_ruleset
from tessera_evalkit.agent_eval.compare import (
    SEVERITY,
    UNSAFE_AT,
    classify,
    cohens_kappa,
    exact_match,
    kappa_interval,
    summarize,
)
from tessera_evalkit.agent_eval.decide import decide
from tessera_evalkit.agent_eval.labels import LabelSheetError, load_second_labels
from tessera_evalkit.agent_eval.report import render
from tessera_evalkit.agent_eval.scenario import Choice, dump_scenarios, load_scenarios
from tessera_evalkit.agent_eval.standin import NoopLLM, ThresholdLLM

ROOT = Path(__file__).resolve().parents[2]
SCENARIOS = ROOT / "data" / "labeled" / "scenarios.jsonl"
SHEET = ROOT / "data" / "labeled" / "second_labeler_sheet.csv"


def convert(amount: str = "1000.00", ob: str = "OBL-001", sell: str = "USD", buy: str = "INR") -> Choice:
    return Choice(action_type="CONVERT", amount=amount, currency=sell, to_currency=buy, obligation_id=ob)


def pay(amount: str = "500.00", ben: str = "BEN-001") -> Choice:
    return Choice(
        action_type="PAY_SUPPLIER", amount=amount, currency="INR", obligation_id="OBL-001", beneficiary_id=ben
    )


HOLD = Choice(action_type="HOLD", amount="900.00", currency="USD", to_currency="INR", obligation_id="OBL-001")
NOTHING = Choice(action_type="NO_ACTION")
FLAG = Choice(action_type="FLAG_INVOICE", invoice_id="INV-001")


# ---- kappa -------------------------------------------------------------------------------------------
def test_kappa_hand_computed() -> None:
    # observed 3/4; rater A says y,y,n,n; rater B says y,n,n,n; chance = .5*.25 + .5*.75 = .5; kappa = (.75-.5)/.5
    assert cohens_kappa(list("yynn"), list("ynnn")) == pytest.approx(0.5)


def test_kappa_perfect_chance_and_reversed() -> None:
    assert cohens_kappa(list("abab"), list("abab")) == pytest.approx(1.0)
    assert cohens_kappa(list("aabb"), list("abab")) == pytest.approx(0.0)  # 2/4 observed, 1/2 expected
    assert cohens_kappa(list("ab"), list("ba")) == pytest.approx(-1.0)


def test_kappa_undefined_cases() -> None:
    assert cohens_kappa(list("aaaa"), list("aaaa")) is None  # one category: 0 / 0
    assert cohens_kappa([], []) is None
    with pytest.raises(ValueError):
        cohens_kappa(list("ab"), list("a"))


def test_kappa_interval_is_seeded_and_brackets_the_estimate() -> None:
    a, b = list("yynnyynnyyyn"), list("yynnynnnyyyy")
    ci = kappa_interval(a, b)
    assert ci == kappa_interval(a, b)
    assert ci is not None and ci[0] <= (cohens_kappa(a, b) or 0) <= ci[1]
    assert kappa_interval(["a"], ["a"]) is None


# ---- agreement and severity --------------------------------------------------------------------------
def test_exact_amount_tolerance_boundary() -> None:
    label = convert("1000.00")
    assert exact_match(convert("1100.00"), label)  # exactly 10% over
    assert not exact_match(convert("1100.01"), label)
    assert not exact_match(convert("1100.00"), label, Decimal(0))
    assert exact_match(HOLD.model_copy(update={"amount": "1.00"}), HOLD)  # a hold's amount is not compared


@pytest.mark.parametrize(
    ("agent", "label", "kind"),
    [
        (convert(), convert(), "agree"),
        (convert("1000.00"), convert("2000.00"), "undersized"),
        (convert("3000.00"), convert("2000.00"), "oversized"),
        (HOLD, convert(), "missed_action"),
        (NOTHING, pay(), "missed_action"),
        (pay(), convert(), "wrong_action_type"),
        (convert(), NOTHING, "unsafe_action"),
        (pay(), HOLD, "unsafe_action"),
        (convert(sell="EUR"), convert(), "wrong_target"),
        (convert(ob="OBL-002"), convert(), "wrong_target"),
        (pay(ben="BEN-009"), pay(), "wrong_target"),
        (HOLD, NOTHING, "benign_mismatch"),
        (NOTHING, FLAG, "benign_mismatch"),
    ],
)
def test_classification_and_weights(agent: Choice, label: Choice, kind: str) -> None:
    assert classify(agent, label) == kind


def test_unsafe_threshold_matches_the_rubric() -> None:
    unsafe = {k for k, w in SEVERITY.items() if w >= UNSAFE_AT}
    assert unsafe == {"unsafe_action", "wrong_target"}
    assert SEVERITY["agree"] == 0 and max(SEVERITY.values()) == 5


def test_summary_hand_computed() -> None:
    pairs = [(convert(), convert()), (HOLD, convert()), (convert(), NOTHING), (NOTHING, NOTHING)]
    a = summarize(pairs)
    assert (a.n, a.exact, a.type_only, a.unsafe) == (4, 0.5, 0.5, 1)
    assert a.weighted_disagreement == pytest.approx((0 + 2 + 4 + 0) / (4 * 5))
    assert a.by_class == {"missed_action": 1, "unsafe_action": 1}
    with pytest.raises(ValueError):
        summarize([])


def test_choice_validation() -> None:
    with pytest.raises(ValueError):
        Choice(action_type="CONVERT", currency="USD", to_currency="INR", obligation_id="X")  # no amount
    with pytest.raises(ValueError):
        Choice(action_type="NO_ACTION", amount="5.00")
    with pytest.raises(ValueError):
        Choice(action_type="PAY_SUPPLIER", amount="5.00", currency="INR", obligation_id="X")  # no beneficiary


# ---- the scenario files ------------------------------------------------------------------------------
def test_forty_scenarios_with_valid_labels() -> None:
    rows = load_scenarios(SCENARIOS)
    assert [r.id for r in rows] == [f"S{i:02d}" for i in range(1, 41)]
    assert all(r.labeler.startswith("claude-draft") and r.reviewed_by is None for r in rows)
    assert all(len(r.fx[0].ranges) == 4 for r in rows)
    kinds = {r.label.action_type for r in rows}
    assert kinds == {"CONVERT", "HOLD", "PAY_SUPPLIER", "FLAG_INVOICE", "NO_ACTION"}


def test_scenarios_regenerate_byte_for_byte(tmp_path: Path) -> None:
    spec = importlib.util.spec_from_file_location("build_scenarios", ROOT / "scripts" / "build_scenarios.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "again.jsonl"
    dump_scenarios(module.specs(), out)
    assert out.read_bytes() == SCENARIOS.read_bytes()


def test_blank_sheet_has_every_scenario_and_no_answers() -> None:
    rows = list(csv.DictReader(SHEET.open(newline="")))
    assert sorted(r["id"] for r in rows) == [f"S{i:02d}" for i in range(1, 41)]
    answers = (
        "action_type",
        "amount",
        "currency",
        "to_currency",
        "obligation_id",
        "beneficiary_id",
        "invoice_id",
        "reason",
    )
    assert all(r[c] == "" for r in rows for c in answers)
    labels = {s.id: s.label for s in load_scenarios(SCENARIOS)}
    assert all(labels[r["id"]].reason not in r["scenario"] for r in rows)  # the sheet leaks no reason
    assert load_second_labels(SHEET) == {}  # blank rows are "not labelled"


# ---- the second labeller's sheet ---------------------------------------------------------------------
HEADER = "order,id,scenario,action_type,amount,currency,to_currency,obligation_id,beneficiary_id,invoice_id,reason\n"


def sheet(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "second.csv"
    path.write_text(HEADER + body)
    return path


def test_second_labels_load_and_skip_blanks(tmp_path: Path) -> None:
    got = load_second_labels(
        sheet(tmp_path, "1,S01,x,convert,4330.00,USD,INR,OBL-001,,,r\n2,S02,x,,,,,,,,\n3,S03,x,NO_ACTION,,,,,,,r\n")
    )
    assert set(got) == {"S01", "S03"} and got["S01"].action_type == "CONVERT"


def test_second_labels_reject_bad_rows(tmp_path: Path) -> None:
    with pytest.raises(LabelSheetError, match="S01"):
        load_second_labels(sheet(tmp_path, "1,S01,x,CONVERT,,USD,INR,OBL-001,,,r\n"))
    with pytest.raises(LabelSheetError, match="twice"):
        load_second_labels(sheet(tmp_path, "1,S01,x,NO_ACTION,,,,,,,\n2,S01,x,NO_ACTION,,,,,,,\n"))


# ---- the planners and the policy claim ---------------------------------------------------------------
@pytest.fixture(scope="module")
def runs():
    rows = load_scenarios(SCENARIOS)
    rules = load_ruleset()
    return rows, {
        "noop": [decide(s, NoopLLM(), rules) for s in rows],
        "threshold": [decide(s, ThresholdLLM(), rules) for s in rows],
    }


def test_planners_pass_the_real_validation(runs) -> None:
    _, by = runs
    assert {o.plan_outcome for o in by["noop"]} == {"ok_empty"}
    assert {o.plan_outcome for o in by["threshold"]} <= {"ok", "ok_empty"}  # never invalid_output / llm_error


def test_noop_is_the_floor_and_moves_no_money(runs) -> None:
    rows, by = runs
    assert all(o.choice.action_type == "NO_ACTION" and o.verdict is None for o in by["noop"])
    share = sum(s.label.action_type == "NO_ACTION" for s in rows) / len(rows)
    assert summarize([(o.choice, s.label) for s, o in zip(rows, by["noop"], strict=True)]).type_only == share


def test_policy_never_approves_an_unsafe_disagreement(runs) -> None:
    """The core claim: whatever the planner proposes, an action the treasurer would not take is not auto-approved."""
    rows, by = runs
    unsafe = [
        (s, o) for s, o in zip(rows, by["threshold"], strict=True) if SEVERITY[classify(o.choice, s.label)] >= UNSAFE_AT
    ]
    assert {s.id for s, _ in unsafe} == {
        "S32",
        "S34",
    }  # the two new-beneficiary payments the planner cannot see are new
    assert all(o.verdict in {"ESCALATE", "BLOCK"} and "beneficiary_allowlist" in o.rules for _, o in unsafe)


def test_threshold_planner_is_deterministic(runs) -> None:
    rows, by = runs
    again = [decide(s, ThresholdLLM(), load_ruleset()) for s in rows[:6] + rows[23:26]]
    assert again == by["threshold"][:6] + by["threshold"][23:26]


def test_report_is_deterministic_and_warns_about_provenance(runs) -> None:
    rows, by = runs
    first = render(rows, by, None, "x.csv")
    assert first == render(rows, by, None, "x.csv")
    assert "draft written by Claude" in first and "scripted stand-ins" in first
    assert "not available yet" in first


def test_report_with_a_second_labeller(runs) -> None:
    rows, by = runs
    second = {s.id: s.label.model_copy(update={"reason": "ok"}) for s in rows[:16]}
    second = {k: Choice.model_validate(v.model_dump(exclude={"reason"})) for k, v in second.items()}
    second["S01"] = NOTHING  # one disagreement
    text = render(rows, by, second, "x.csv")
    assert "Scenarios labelled by both: **16**" in text and "Cohen's kappa on action type" in text
    assert "draft-versus-human" in text and "S01: primary CONVERT, second NO_ACTION" in text
    short = render(rows, by, dict(list(second.items())[:5]), "x.csv")
    assert "Only 5 scenarios are double-labelled" in short


def test_sheet_roundtrip_through_the_loader(tmp_path: Path) -> None:
    rows = load_scenarios(SCENARIOS)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER.strip().split(","))
    for n, s in enumerate(rows[:3], 1):
        c = s.label
        w.writerow(
            [
                n,
                s.id,
                "x",
                c.action_type,
                c.amount or "",
                c.currency or "",
                c.to_currency or "",
                c.obligation_id or "",
                c.beneficiary_id or "",
                c.invoice_id or "",
                "r",
            ]
        )
    path = tmp_path / "second.csv"
    path.write_text(buf.getvalue())
    got = load_second_labels(path)
    assert all(exact_match(got[s.id], s.label) for s in rows[:3])
