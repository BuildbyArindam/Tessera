# ruff: noqa: E501  (report-style script: long print lines are the output)
"""Day 8 verification: the simulation sandbox. Run: .venv/bin/python scripts/verify_day8.py

Mirrors the Day 8 verification prompt and exits non-zero on any failure:
  1. same seed => identical report; different seeds agree within a stated tolerance at 5000 paths
  2. flat rate: expected saving == minus the cost, zero variance
  3. proposal identical to its baseline: saving is exactly zero
  4. no observation after as_of is used (and the test can fail)
  5. 5000 paths run in under 2 seconds
  6. worst_case_loss_gate escalates a deliberately risky example (and approves a calm one)
Then informational (not pass/fail): how often realised outcomes fall below the simulated 5th percentile.
FX for sections 1, 5, 6 and the informational part is the Day 2 SYNTHETIC generator (no network).
"""

import hashlib
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests/unit")]

from policy_helpers import full_context, make_proposal  # noqa: E402
from sim_helpers import PAIR, business_days, constant_series, convert, hold, make_series, pay, scaled_after  # noqa: E402
from tessera_core.domain import ActionType, Currency, Money, Verdict  # noqa: E402
from tessera_core.policy import evaluate, load_ruleset  # noqa: E402
from tessera_core.simulate import FxSeries, SimulationConfig, replay, simulate  # noqa: E402
from tessera_evalkit.fx import load_fx  # noqa: E402

failures = 0
N = 5000


def check(name: str, ok: bool, detail: str = "") -> None:
    global failures
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))


def usd(amount: str) -> Money:
    return Money(amount=amount, currency=Currency.USD)


# ---- the Day 2 synthetic FX, as observed-business-day series ---------------------------------------
FX = load_fx(date(2023, 1, 2), date(2026, 6, 30), seed=7, data_dir=Path(tempfile.mkdtemp()), source="synthetic")


def fx_series(pair: str) -> FxSeries:
    sell, buy = pair.split("/")
    days = [d for d in sorted(FX.eur_per) if d.weekday() < 5]  # drop the forward-filled weekends
    return FxSeries.from_observations(pair, [(d, FX.rate(d, sell, buy)) for d in days])


REAL = {p: fx_series(p) for p in ("USD/EUR", "USD/INR")}
AS_OF = date(2026, 3, 2)
DUE = AS_OF + timedelta(days=30)
print(
    f"FX: {FX.source}, {len(REAL['USD/EUR'])} observed business days, {REAL['USD/EUR'].days[0]} to {REAL['USD/EUR'].last_day}"
)
print(f"as_of {AS_OF}, due {DUE} (30 calendar days, 21 business-day steps)\n")

# ---- 1. determinism and cross-seed tolerance -----------------------------------------------------------
print("=== 1. determinism ===")
prop = convert("4000.00")
digests = [
    hashlib.sha256(simulate(prop, REAL, AS_OF, N, s, due_date=DUE).model_dump_json().encode()).hexdigest()[:12]
    for s in (7, 7, 8)
]
print(f"report hashes, seeds 7 / 7 / 8: {digests}")
check("same seed => identical report", digests[0] == digests[1])
check("different seed => different report", digests[0] != digests[2])

reports = [simulate(prop, REAL, AS_OF, N, s, due_date=DUE) for s in range(10)]
means = np.array([float(r.expected_saving.amount) for r in reports])
p05s = np.array([float(r.saving_p05.amount) for r in reports])
sigma = float(np.mean([float(r.saving_std.amount) for r in reports]))
tol_mean, tol_p05 = 4 * np.sqrt(2) * sigma / np.sqrt(N), 0.25 * sigma
print(
    f"10 seeds at n_paths={N}: mean saving {means.min():.2f}..{means.max():.2f}, p05 {p05s.min():.2f}..{p05s.max():.2f}, sigma {sigma:.2f}"
)
print(
    f"stated tolerance: mean spread <= 4 SE of a difference = {tol_mean:.2f}; p05 spread <= 0.25 sigma = {tol_p05:.2f}"
)
check(
    "seeds agree on the mean within tolerance",
    means.max() - means.min() <= tol_mean,
    f"spread {means.max() - means.min():.2f}",
)
check(
    "seeds agree on the 5th percentile within tolerance",
    p05s.max() - p05s.min() <= tol_p05,
    f"spread {p05s.max() - p05s.min():.2f}",
)

# ---- 2. flat rate -------------------------------------------------------------------------------------------
print("\n=== 2. zero volatility (constant rate 0.9, 10,000 USD, spread 50 bps) ===")
flat = {PAIR: constant_series(n=200)}
flat_as_of = business_days(200)[150]
fdue = flat_as_of + timedelta(days=30)
for label, proposal, expected in (("CONVERT", convert(), "-50.00"), ("PAY_SUPPLIER", pay(), "-50.25")):
    r = simulate(proposal, flat, flat_as_of, N, 1, due_date=fdue)
    print(f"{label}: saving {r.expected_saving}, std {r.saving_std}, spread cost {r.spread_cost}, fee {r.fee_cost}")
    check(
        f"{label}: saving == -(spread + fee), zero variance",
        r.expected_saving == usd(expected) and r.saving_std == usd("0.00"),
    )
cfg = SimulationConfig(spread_bps="25", fee="2.50")
r = simulate(convert(), flat, flat_as_of, N, 1, due_date=fdue, config=cfg)
check(
    "with a fee: saving == -(spread + fee) to the cent",
    abs(r.expected_saving.amount + r.spread_cost.amount + r.fee_cost.amount) <= 0.01,
    f"{r.expected_saving} vs -({r.spread_cost} + {r.fee_cost})",
)
check(
    "HOLD: waiting costs nothing at a flat rate",
    simulate(hold(), flat, flat_as_of, N, 1, due_date=fdue).expected_saving == usd("0.00"),
)

# ---- 3. identical to the baseline ---------------------------------------------------------------------------
print("\n=== 3. proposal identical to its baseline ===")
r = simulate(convert(), REAL, AS_OF, N, 1, due_date=AS_OF, config=SimulationConfig(spread_bps="0", fee="0"))
print(
    f"CONVERT due today, no costs, on the volatile series: mean {r.expected_saving}, std {r.saving_std}, p05 {r.saving_p05}"
)
check(
    "saving is exactly zero on every path",
    r.expected_saving == usd("0.00") and r.saving_std == usd("0.00") and r.saving_p05 == usd("0.00"),
)
check(
    "HOLD until today == converting now",
    simulate(hold(), REAL, AS_OF, N, 1, due_date=AS_OF).expected_saving == usd("0.00"),
)

# ---- 4. no look-ahead ---------------------------------------------------------------------------------------------
print("\n=== 4. no data after as_of is used ===")
sa_series = make_series()
sa_as_of = business_days(800)[600]
sa_due = sa_as_of + timedelta(days=30)
base = simulate(prop, {PAIR: sa_series}, sa_as_of, N, 9, due_date=sa_due)
same = [
    simulate(prop, {PAIR: scaled_after(sa_series, sa_as_of, f)}, sa_as_of, N, 9, due_date=sa_due) == base
    for f in (0.1, 3.0, 25.0)
]
check("future observations scaled x0.1, x3, x25: report unchanged", all(same))
check(
    "history cut at as_of: report unchanged",
    simulate(prop, {PAIR: sa_series.up_to(sa_as_of)}, sa_as_of, N, 9, due_date=sa_due) == base,
)
a, b = (replay(prop, {PAIR: s}, sa_as_of, due_date=sa_due) for s in (sa_series, scaled_after(sa_series, sa_as_of, 1.2)))
check(
    "control: replay DOES read the future (so the test above can fail)",
    a.realised_saving != b.realised_saving,
    f"{a.realised_saving} vs {b.realised_saving}",
)

# ---- 5. speed ---------------------------------------------------------------------------------------------------------
print("\n=== 5. timing, 5000 paths ===")
worst = 0.0
for days in (30, 90, 365):
    started = time.perf_counter()
    simulate(prop, REAL, AS_OF, N, 1, due_date=AS_OF + timedelta(days=days))
    took = time.perf_counter() - started
    worst = max(worst, took)
    print(f"{days:>3}-day horizon: {took * 1000:6.1f} ms")
check("5000 paths under 2 seconds at every horizon", worst < 2.0, f"worst {worst * 1000:.1f} ms")

# ---- 6. the gate ---------------------------------------------------------------------------------------------------------
print("\n=== 6. worst_case_loss_gate (full shipped ruleset, clean context otherwise) ===")
rules = load_ruleset()
for label, vol in (("calm market (0.1% a day)", 0.001), ("wild market (3% a day)", 0.03)):
    series = make_series(daily_vol=vol)
    p = make_proposal(ActionType.CONVERT, "4000.00", "USD", to_currency="EUR")
    rep = simulate(p, {PAIR: series}, sa_as_of, N, 1, due_date=sa_due)
    d = evaluate(p, full_context(simulation=rep.summary()), rules)
    print(
        f"{label}: expected {rep.expected_saving}, p05 {rep.saving_p05}, worst-case loss {rep.worst_case_loss} -> {d.verdict.value} {d.rule_ids}"
    )
    if vol > 0.01:
        check(
            "risky conversion is ESCALATEd by the gate alone",
            d.verdict is Verdict.ESCALATE and d.rule_ids == ("worst_case_loss_gate",),
        )
        print(f"    reason: {d.reasons[0]}")
    else:
        check("calm conversion is APPROVEd", d.verdict is Verdict.APPROVE)
no_sim = evaluate(make_proposal(ActionType.CONVERT, "4000.00", "USD", to_currency="EUR"), full_context(), rules)
check(
    "no simulation supplied => ESCALATE, never APPROVE",
    no_sim.verdict is Verdict.ESCALATE and no_sim.rule_ids == ("worst_case_loss_gate",),
)

# ---- informational: does the realised outcome respect the simulated tail? -----------------------------------------
print("\n=== informational (not pass/fail): realised saving vs simulated 5th percentile, 30-day CONVERT ===")
for pair in ("USD/EUR", "USD/INR"):
    sell, buy = pair.split("/")
    p = make_proposal(ActionType.CONVERT, "10000.00", sell, to_currency=buy)
    days = [d for d in REAL[pair].days.astype("datetime64[D]").astype(object)]
    hits, total = 0, 0
    for i in range(520, len(days) - 25, 21):
        as_of_i = days[i]
        sim = simulate(p, REAL, as_of_i, 2000, 1, due_date=as_of_i + timedelta(days=30), config=SimulationConfig())
        real = replay(p, REAL, as_of_i, due_date=as_of_i + timedelta(days=30))
        hits += real.realised_saving < sim.saving_p05
        total += 1
    print(
        f"{pair}: realised below simulated p05 in {hits}/{total} origins ({100 * hits / total:.0f}%, nominal 5%; {total} origins, indicative only)"
    )
print(
    "(18 non-overlapping-ish monthly origins cannot distinguish 0% from 5%; a real coverage test needs Day 17's longer backtest."
)
print(" USD/INR drifts +3%/yr in the synthetic generator, which only helps a USD seller, so its tail is rarely hit.)")

print(f"\n{'ALL CHECKS PASSED' if not failures else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
