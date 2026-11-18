"""Build data/labeled/scenarios.jsonl (Day 19) and the blank sheet for the second labeller.

    python scripts/build_scenarios.py

Each spec below is a situation plus the action INTENDED for it. The script applies the rubric (docs/labeling-rubric.md,
section "Decision rules") to the computed numbers and refuses to write if the rubric and the intention disagree, so a
label cannot silently contradict the rubric. The labels are a DRAFT written by Claude, not by a treasurer: see the
rubric's "Provenance" section. FX ranges are computed from seeded synthetic series (agent_eval/world.py).

Rerunning rewrites both files byte for byte (every seed is fixed).
"""

from __future__ import annotations

import csv
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / p) for p in ("core", "agent", "evalkit")]

from tessera_evalkit.agent_eval.scenario import (  # noqa: E402
    Ben,
    Fc,
    Fx,
    Label,
    Obl,
    Scenario,
    describe,
    dump_scenarios,
)  # fmt: skip
from tessera_evalkit.agent_eval.world import compute_ranges  # noqa: E402

OUT = ROOT / "data" / "labeled"
SPOT = {"USD/INR": 83.2, "EUR/INR": 90.5, "GBP/INR": 105.1, "EUR/USD": 1.087}
LABELER = "claude-draft (rubric v1)"
CALM, NORMAL, ELEVATED, STRESSED = 0.12, 0.25, 0.40, 0.70  # daily volatility, percent


def up10(x: float) -> str:
    return f"{math.ceil(x / 10) * 10:.2f}"


def forecast(ccy: str, point: float, wide: bool) -> Fc:
    lo, hi = (0.1, 1.9) if wide else (0.8, 1.2)
    return Fc(currency=ccy, point=f"{point:.2f}", lower=f"{point * lo:.2f}", upper=f"{point * hi:.2f}")


def horizon_row(fx: Fx, due: int):
    rows = {r.horizon_days: r for r in fx.ranges}
    return rows[next((h for h in (3, 7, 14, 30) if h >= due), 30)]


def suggest(due: int, p05: float, wide: bool) -> tuple[str, float, str]:
    """Rubric R4 for a funding gap: (action, fraction of the gap to sell now, rule id)."""
    if due <= 3:
        return "CONVERT", 1.0, "R4a"
    if due > 14:
        return ("CONVERT", 0.5, "R4d") if p05 <= -3.0 else ("HOLD", 0.0, "R4c")
    full, half = (p05 <= -1.5), (-1.5 < p05 <= -0.8)
    if wide:
        return ("CONVERT", 0.5, "R4e") if full else ("HOLD", 0.0, "R4e")
    return ("CONVERT", 1.0, "R4b") if full else ("CONVERT", 0.5, "R4b") if half else ("HOLD", 0.0, "R4c")


def gap_scenario(i, title, pair, vol, due, amount, bal_p, bal_s, intended, *, wide=False, distractor=True):
    sell, buy = pair.split("/")
    fx = Fx(pair=pair, spot=SPOT[pair], daily_vol_pct=vol, seed=1000 + i)
    fx = fx.model_copy(update={"ranges": compute_ranges(fx)})
    row = horizon_row(fx, due)
    action, frac, rule = suggest(due, row.p05_pct, wide)
    assert action == intended, f"S{i:02d}: rubric says {action}, intended {intended} (p05 {row.p05_pct})"
    gap = amount - bal_p
    sell_units = gap / fx.spot * (frac if action == "CONVERT" else 1.0)
    reasons = {
        "R4a": f"Due in {due} days with a {buy} {gap:,.0f} shortfall: no time left to wait, buy the whole gap now.",
        "R4b": f"5th-percentile move to the due date is {row.p05_pct:+.2f}% for {pair}: "
        + ("lock in the whole gap." if frac == 1.0 else "hedge half the gap and keep the rest open."),
        "R4c": f"5th-percentile move to the due date is only {row.p05_pct:+.2f}% for {pair}: not worth locking in yet, hold and revisit.",
        "R4d": f"Due late but the 5th-percentile move is {row.p05_pct:+.2f}%: hedge half now, revisit weekly.",
        "R4e": f"The {sell} inflow forecast is too wide to commit fully: "
        + ("hedge only half the gap." if action == "CONVERT" else "hold and revisit."),
    }
    obl = [
        Obl(
            id="OBL-001",
            kind="PAYABLE",
            amount=f"{amount:.2f}",
            currency=buy,
            due_in_days=due,
            beneficiary_id="BEN-001",
        )
    ]
    if distractor:
        obl.append(
            Obl(
                id="OBL-002",
                kind="RECEIVABLE",
                amount="2500.00",
                currency=sell,
                due_in_days=20 + i,
                invoice_id=f"INV-{i:03d}",
            )
        )
    label = Label(
        action_type=action, amount=up10(sell_units) if action != "NO_ACTION" else None, currency=sell, to_currency=buy,
        obligation_id="OBL-001", reason=reasons[rule],
    )  # fmt: skip
    return Scenario(
        id=f"S{i:02d}", title=title, focus="OBL-001", balances={buy: f"{bal_p:.2f}", sell: f"{bal_s:.2f}"},
        forecasts=(forecast(sell, bal_s * 0.2, wide), forecast(buy, amount * 0.5, False)), obligations=tuple(obl),
        beneficiaries=(Ben(id="BEN-001", currency=buy, status="approved"),), fx=(fx,), label=label, labeler=LABELER,
    )  # fmt: skip


def pay_scenario(i, title, ccy, amount, bal, due, ben_status, intended):
    fx = Fx(pair="USD/INR", spot=SPOT["USD/INR"], daily_vol_pct=NORMAL, seed=1000 + i)
    fx = fx.model_copy(update={"ranges": compute_ranges(fx)})
    if ben_status != "approved":
        action, reason = (
            "NO_ACTION",
            "Beneficiary is not fully established (new or unapproved): do not pay until it is verified.",
        )
    elif due <= 2:
        action, reason = (
            "PAY_SUPPLIER",
            f"Due in {due} days, funds are in the {ccy} balance and the beneficiary is approved: pay in full.",
        )
    else:
        action, reason = (
            "NO_ACTION",
            f"Funds are in place and it is not due for {due} days: pay on the due date, not early.",
        )
    assert action == intended, f"S{i:02d}: rubric says {action}, intended {intended}"
    paying = action == "PAY_SUPPLIER"
    label = Label(
        action_type=action, amount=f"{amount:.2f}" if paying else None, currency=ccy if paying else None,
        obligation_id="OBL-001" if paying else None, beneficiary_id="BEN-001" if paying else None, reason=reason,
    )  # fmt: skip
    return Scenario(
        id=f"S{i:02d}", title=title, focus="OBL-001", balances={ccy: f"{bal:.2f}"}, forecasts=(forecast(ccy, bal * 0.2, False),),
        obligations=(Obl(id="OBL-001", kind="PAYABLE", amount=f"{amount:.2f}", currency=ccy, due_in_days=due, beneficiary_id="BEN-001"),),
        beneficiaries=(Ben(id="BEN-001", currency=ccy, status=ben_status),), fx=(fx,), label=label, labeler=LABELER,
    )  # fmt: skip


def receivable_scenario(i, title, ccy, amount, overdue_days, intended):
    fx = Fx(pair="USD/INR", spot=SPOT["USD/INR"], daily_vol_pct=NORMAL, seed=1000 + i)
    fx = fx.model_copy(update={"ranges": compute_ranges(fx)})
    flag = overdue_days >= 7
    action = "FLAG_INVOICE" if flag else "NO_ACTION"
    assert action == intended, f"S{i:02d}: rubric says {action}, intended {intended}"
    reason = (
        f"Overdue by {overdue_days} days (7 or more): flag it as likely late and chase the customer."
        if flag else f"Only {overdue_days} days overdue: normal slippage, keep watching."
    )  # fmt: skip
    label = Label(action_type=action, invoice_id=f"INV-{i:03d}" if flag else None, reason=reason)
    return Scenario(
        id=f"S{i:02d}", title=title, focus="OBL-001", balances={ccy: "20000.00", "INR": "300000.00"},
        forecasts=(forecast(ccy, 4000.0, False),),
        obligations=(Obl(id="OBL-001", kind="RECEIVABLE", amount=f"{amount:.2f}", currency=ccy, due_in_days=-overdue_days, invoice_id=f"INV-{i:03d}"),),
        beneficiaries=(), fx=(fx,), label=label, labeler=LABELER,
    )  # fmt: skip


def idle_scenario(i):
    fx = Fx(pair="USD/INR", spot=SPOT["USD/INR"], daily_vol_pct=NORMAL, seed=1000 + i)
    fx = fx.model_copy(update={"ranges": compute_ranges(fx)})
    label = Label(
        action_type="NO_ACTION", reason="Nothing is due and nothing is overdue: leave the balances as they are."
    )
    return Scenario(
        id=f"S{i:02d}", title="Surplus USD, nothing due", focus="OBL-001", balances={"USD": "40000.00", "INR": "500000.00"},
        forecasts=(forecast("USD", 8000.0, False),),
        obligations=(Obl(id="OBL-001", kind="RECEIVABLE", amount="6000.00", currency="USD", due_in_days=20, invoice_id=f"INV-{i:03d}"),),
        beneficiaries=(), fx=(fx,), label=label, labeler=LABELER,
    )  # fmt: skip


def specs() -> list[Scenario]:
    g, p, r = gap_scenario, pay_scenario, receivable_scenario
    return [
        g(1, "INR payable in 2 days, USD to sell", "USD/INR", NORMAL, 2, 420_000, 60_000, 30_000, "CONVERT"),
        g(2, "INR payable in 3 days, EUR to sell", "EUR/INR", NORMAL, 3, 650_000, 100_000, 15_000, "CONVERT"),
        g(3, "INR payable tomorrow, no INR at all, calm market", "USD/INR", CALM, 1, 250_000, 0, 20_000, "CONVERT"),
        g(
            4,
            "INR payable in 3 days, GBP to sell, elevated vol",
            "GBP/INR",
            ELEVATED,
            3,
            800_000,
            200_000,
            9_000,
            "CONVERT",
        ),
        g(5, "USD payable in 2 days, funded from EUR", "EUR/USD", NORMAL, 2, 9_000, 1_500, 12_000, "CONVERT"),
        g(6, "INR payable in 3 days, stressed market", "USD/INR", STRESSED, 3, 300_000, 50_000, 12_000, "CONVERT"),
        g(7, "INR payable in 2 days, EUR to sell, calm", "EUR/INR", CALM, 2, 500_000, 80_000, 10_000, "CONVERT"),
        g(8, "INR payable in 10 days, stressed market", "USD/INR", STRESSED, 10, 500_000, 50_000, 15_000, "CONVERT"),
        g(
            9,
            "INR payable in 6 days, stressed market, EUR",
            "EUR/INR",
            STRESSED,
            6,
            700_000,
            100_000,
            12_000,
            "CONVERT",
        ),
        g(10, "INR payable in 12 days, elevated vol", "USD/INR", ELEVATED, 12, 480_000, 60_000, 12_000, "CONVERT"),
        g(11, "INR payable in 5 days, elevated vol, GBP", "GBP/INR", ELEVATED, 5, 600_000, 100_000, 8_000, "CONVERT"),
        g(12, "INR payable in 14 days, normal vol", "USD/INR", NORMAL, 14, 400_000, 40_000, 10_000, "CONVERT"),
        g(
            13,
            "USD payable in 9 days, wide EUR inflow forecast",
            "EUR/USD",
            ELEVATED,
            9,
            8_000,
            1_000,
            9_000,
            "CONVERT",
            wide=True,
        ),
        g(14, "INR payable in 8 days, calm market", "USD/INR", CALM, 8, 300_000, 50_000, 10_000, "HOLD"),
        g(15, "INR payable in 12 days, calm, EUR", "EUR/INR", CALM, 12, 450_000, 50_000, 8_000, "HOLD"),
        g(16, "INR payable in 6 days, calm market", "USD/INR", CALM, 6, 350_000, 70_000, 9_000, "HOLD"),
        g(17, "INR payable in 13 days, calm, GBP", "GBP/INR", CALM, 13, 520_000, 120_000, 6_000, "HOLD"),
        g(
            18,
            "INR payable in 7 days, elevated vol, wide USD forecast",
            "USD/INR",
            ELEVATED,
            7,
            400_000,
            40_000,
            9_000,
            "HOLD",
            wide=True,
        ),
        g(
            19,
            "INR payable in 10 days, normal vol, wide USD forecast",
            "USD/INR",
            NORMAL,
            10,
            380_000,
            30_000,
            8_000,
            "HOLD",
            wide=True,
        ),
        g(20, "INR payable in 25 days, normal vol", "USD/INR", NORMAL, 25, 500_000, 50_000, 12_000, "HOLD"),
        g(21, "INR payable in 40 days, elevated vol", "EUR/INR", ELEVATED, 40, 600_000, 100_000, 9_000, "HOLD"),
        g(22, "INR payable in 21 days, stressed market", "USD/INR", STRESSED, 21, 560_000, 60_000, 12_000, "CONVERT"),
        g(23, "INR payable in 30 days, calm, GBP", "GBP/INR", CALM, 30, 640_000, 140_000, 7_000, "HOLD"),
        p(24, "INR payable tomorrow, funds in place", "INR", 180_000, 400_000, 1, "approved", "PAY_SUPPLIER"),
        p(25, "INR payable in 2 days, funds exactly cover it", "INR", 90_000, 90_000, 2, "approved", "PAY_SUPPLIER"),
        p(26, "USD payable due today, funds in place", "USD", 4_500, 10_000, 0, "approved", "PAY_SUPPLIER"),
        p(27, "INR payable in 2 days, large balance", "INR", 350_000, 900_000, 2, "approved", "PAY_SUPPLIER"),
        p(28, "EUR payable tomorrow, funds in place", "EUR", 2_800, 6_000, 1, "approved", "PAY_SUPPLIER"),
        p(29, "INR payable in 7 days, funds in place", "INR", 200_000, 500_000, 7, "approved", "NO_ACTION"),
        p(30, "INR payable in 5 days, funds in place", "INR", 450_000, 800_000, 5, "approved", "NO_ACTION"),
        p(31, "USD payable in 10 days, funds in place", "USD", 6_000, 15_000, 10, "approved", "NO_ACTION"),
        p(32, "INR payable in 2 days, beneficiary added 3 hours ago", "INR", 220_000, 500_000, 2, "new", "NO_ACTION"),
        p(33, "INR payable tomorrow, beneficiary not approved", "INR", 130_000, 400_000, 1, "unapproved", "NO_ACTION"),
        p(34, "USD payable in 2 days, beneficiary added 3 hours ago", "USD", 3_000, 9_000, 2, "new", "NO_ACTION"),
        r(35, "USD invoice overdue 21 days", "USD", 8_000, 21, "FLAG_INVOICE"),
        r(36, "INR invoice overdue 9 days", "INR", 150_000, 9, "FLAG_INVOICE"),
        r(37, "EUR invoice overdue 45 days", "EUR", 5_000, 45, "FLAG_INVOICE"),
        r(38, "USD invoice overdue 3 days", "USD", 2_500, 3, "NO_ACTION"),
        r(39, "GBP invoice overdue 30 days", "GBP", 4_200, 30, "FLAG_INVOICE"),
        idle_scenario(40),
    ]


def second_sheet(rows: list[Scenario]) -> None:
    """One row per scenario in a fixed random order, with the scenario text and blank answer columns. No labels."""
    order = list(rows)
    random.Random(19).shuffle(order)
    with (OUT / "second_labeler_sheet.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(
            [
                "order",
                "id",
                "scenario",
                "action_type",
                "amount",
                "currency",
                "to_currency",
                "obligation_id",
                "beneficiary_id",
                "invoice_id",
                "reason",
            ]
        )
        for n, s in enumerate(order, 1):
            w.writerow([n, s.id, describe(s), "", "", "", "", "", "", "", ""])


def main() -> int:
    rows = specs()
    assert len(rows) == 40 and len({s.id for s in rows}) == 40
    OUT.mkdir(parents=True, exist_ok=True)
    dump_scenarios(rows, OUT / "scenarios.jsonl")
    second_sheet(rows)
    counts: dict[str, int] = {}
    for s in rows:
        counts[s.label.action_type] = counts.get(s.label.action_type, 0) + 1
    print("wrote", len(rows), "scenarios;", dict(sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
