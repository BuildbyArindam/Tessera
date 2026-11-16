# ruff: noqa: E501  (report prose lines)
"""Savings, ablation and null-result sections of the generated report."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
import yaml
from tessera_core.policy.loader import DEFAULT_RULES_DIR

from tessera_evalkit import eval_config as C
from tessera_evalkit.eval_md import ci, num, pct, table

if TYPE_CHECKING:
    from tessera_evalkit.evaluate import EvalResults


def _escalated_by(d: object) -> str:
    return ", ".join(f"{k} {v}" for k, v in dict(d).items()) or "none"  # type: ignore[call-overload]


def savings_section(r: EvalResults) -> str:
    h, c, p = r.headline, r.costs, r.planner
    rows_h = [
        [
            x.baseline,
            ci(x.saving_per_10k, x.lo, x.hi, 2),
            f"[{num(x.week_lo, 2)}, {num(x.week_hi, 2)}]",
            f"{x.n_acted}/{x.n_obligations}",
            pct(x.share_beat),
            pct(x.share_tie),
            pct(x.share_lose),
            pct(x.share_beat_when_acted, 0),
            x.verdict,
        ]
        for x in h.itertuples()
    ]
    due = h.set_index("baseline").loc["convert on due date"]
    rows_c = [
        [x.strategy, num(x.total_cost_eur, 0), num(x.spread_eur, 0), num(x.conversions, 0)] for x in c.itertuples()
    ]
    t = r.tranche
    extra = int(c.set_index("strategy").loc["Tessera (full policy)", "conversions"] - len(r.per_obligation))
    rows_s = [
        [x.spread_bps, x.fee, ci(x.saving_per_10k, x.lo, x.hi, 2), int(x.n_acted)] for x in r.sensitivity.itertuples()
    ]
    df = r.per_obligation
    drift = float(
        (df["sell_invoice"] - df["sell_due"]).mul(df["notional_eur"] / df["sell_due"]).sum()
        / df["notional_eur"].sum()
        * 10_000
    )
    return f"""## 3. FX savings (replay over the held-out FX path)

**What is replayed.** Each held-out supplier invoice (AUD or SGD, 30-day terms) must be paid in the payout currency on its
due date. The business funds it from the USD/EUR/GBP currency it invoiced most in before {C.HELDOUT_START.date()}. Each Sunday
from the invoice to the day before it is due, the scripted planner (section 1) may propose converting part of the remaining need;
the Day 8 simulator scores the proposal using FX history up to that day only; the **real policy engine with the shipped 13-rule
YAML ruleset at autonomy L{C.AUTONOMY_LEVEL}** judges it with a full context (balance stand-in, caps, daily spend, velocity,
forecast interval from the held-out-safe model, simulation, obligation due date). APPROVE executes at the last observed mid
rate with spread and fee; ESCALATE and BLOCK do nothing, because nobody is available to approve in a replay (this measures the
**autonomous** path). What is left converts on the due date, as the baseline does. The two baselines buy the whole payout in one
conversion, on the due date or on the invoice date. The quote slippage guard cannot fire here (the executable quote is the
reference mid by construction; no intraday data exists).

**Units.** Saving is in EUR (each obligation converted at its due-date rate) per 10,000 units converted, as a ratio of sums
(baseline due-date notional). Positive means Tessera paid less. Intervals are paired bootstrap over obligations
({C.N_BOOT:,} replicates, seed {C.SEED}); the second interval resamples whole due-weeks, which is the more conservative view
because every business faces the same FX path.

![Savings](eval/savings_overview.png)

{table(["baseline", "saving per 10,000 [95% CI, obligations]", "95% CI, due-week clusters", "acted", "beat", "tied", "lost", "beat, when acted", "verdict"], rows_h)}

"Beat / tied / lost" is the share of the {int(due["n_obligations"])} obligations on which Tessera's total FX cost was lower than, equal to or higher than the
baseline. A tie means Tessera made no early conversion and so paid exactly the baseline's cost (spread and fee included).

Total cost of each strategy (EUR; spread is the part of the cost paid to the converter; each conversion also pays a fixed fee,
and Tessera made {extra} more conversions than the baselines):

{table(["strategy", "total cost (EUR)", "of which spread (EUR)", "conversions"], rows_c)}

**Decision level.** Of {int(t["executed"])} early conversions the policy approved, {int(t["beat_due_date"])} cost less than buying the same
units on the due date; their net effect was {num(t["total_eur"], 2)} EUR in total.

**Sensitivity to the cost assumptions** (full policy against convert-on-due-date; the planner threshold is not re-fitted):

{table(["spread (bps)", "fee per conversion", "saving per 10,000 [95% CI]", "obligations acted on"], rows_s)}

**Reading it.** The due-date comparison is the skill test: {due["verdict"]}, on {int(due["n_acted"])} obligations where Tessera
acted at all ({p["heldout_obligations"]} total), so this sample cannot separate a small real effect from luck. Convert-on-invoice-date
paid {num(drift, 1)} EUR per 10,000 {"more" if drift > 0 else "less"} than convert-on-due-date on this path
(buying early lost when the funding currency strengthened and gained when it weakened); a policy that mostly waits inherits that
difference without any forecasting skill, and the week-cluster interval for that row includes zero, so even this gap is only about one path's worth of evidence.
"""


def _row(x: object) -> list[object]:
    return [
        x.variant,
        ci(x.saving_per_10k, x.lo, x.hi, 2),
        ci(x.diff_vs_full, x.diff_lo, x.diff_hi, 2),  # type: ignore[attr-defined]
        num(x.worst_obligation_saving_per_10k, 1),
        num(x.p05_obligation_saving_per_10k, 1),  # type: ignore[attr-defined]
        num(x.max_simulated_worst_case_loss_approved_eur, 1),
        x.approved,
        x.escalated,
        x.blocked,
        _escalated_by(x.escalated_by),  # type: ignore[attr-defined]
    ]


_ABL_HEAD = [
    "variant",
    "saving per 10,000 [95% CI]",
    "difference vs full [95% CI]",
    "worst single obligation (per 10,000)",
    "5th pct obligation (per 10,000)",
    "max simulated loss, approved (EUR)",
    "approved",
    "escalated",
    "blocked",
    "escalated by",
]


def _effect(a: pd.DataFrame, variant: str, gate: str) -> str:
    f, v = a.set_index("variant").loc["full policy"], a.set_index("variant").loc[variant]
    metrics = ("saving_per_10k", "worst_obligation_saving_per_10k", "approved", "escalated", "blocked")
    if all(abs(float(f[m]) - float(v[m])) < 1e-9 for m in metrics):
        return (
            f"- Removing `{gate}` changed **nothing** (same approvals, escalations, savings and worst case): "
            "on this data the gate never bound, so this ablation shows no effect to measure."
        )
    return (
        f"- Removing `{gate}`: approved {int(f['approved'])} to {int(v['approved'])}, escalated {int(f['escalated'])} to {int(v['escalated'])}, "
        f"saving {num(f['saving_per_10k'], 2)} to {num(v['saving_per_10k'], 2)} per 10,000 (paired difference "
        f"{ci(v['diff_vs_full'], v['diff_lo'], v['diff_hi'], 2)}), worst single obligation "
        f"{num(f['worst_obligation_saving_per_10k'], 1)} to {num(v['worst_obligation_saving_per_10k'], 1)} per 10,000."
    )


def ablation_section(r: EvalResults) -> str:
    spec = yaml.safe_load((DEFAULT_RULES_DIR / "worst_case_loss_gate.yaml").read_text(encoding="utf-8"))
    ceilings = [float(v) for k, v in spec["params"]["max_worst_case_loss"].items() if k in C.FUNDING_CURRENCIES]
    biggest = float(r.decisions["worst_case_loss_eur"].max())
    a, s = r.ablation, r.stress_ablation
    return f"""## 4. Ablation of the two gates

Same replay, same seeds, same obligations; only the named rule is removed from the ruleset (so a proposal it escalated can
now be approved and executed, and later decisions follow from that). "Worst case" is shown two ways: the realised worst
single obligation (per 10,000, against convert-on-due-date) and the largest simulated 5th-percentile loss among proposals that were approved.

![Ablation](eval/ablation_overview.png)

{table(_ABL_HEAD, [_row(x) for x in a.itertuples()])}

{_effect(a, "without forecast_confidence_gate", "forecast_confidence_gate")}
{_effect(a, "without worst_case_loss_gate", "worst_case_loss_gate")}
- Largest simulated worst-case loss across all {len(r.decisions)} proposals: {num(biggest, 1)} EUR, against shipped ceilings of {num(min(ceilings), 0)} to {num(max(ceilings), 0)} in the sell currencies (USD, EUR, GBP), while the autonomy caps already limit a conversion to about 3,000 units. The loss gate sits behind tighter limits and was never the binding one.

**Sensitivity, not a result: the loss ceilings divided by {round(1 / float(r.planner["stress_ceiling_scale"]))}.** To see what the gate does when it does bind, the same replay with every `worst_case_loss_gate` ceiling cut tenfold
(illustrative only; the shipped values are used everywhere else):

![Ablation with tighter ceilings](eval/ablation_stress.png)

{table(_ABL_HEAD, [_row(x) for x in s.itertuples()])}

{_effect(s, "without worst_case_loss_gate", "worst_case_loss_gate")}
{_effect(s, "without forecast_confidence_gate", "forecast_confidence_gate")}
"""


def null_results_section(r: EvalResults) -> str:
    h = r.headline.set_index("baseline")
    d = h.loc["convert on due date"]
    a = r.ablation.set_index("variant")
    n_prop = len(r.decisions)
    fu, fc = a.loc["full policy"], a.loc["without forecast_confidence_gate"]
    dec = r.decisions
    gw = dec[dec["rule_ids"].fillna("").str.contains("forecast_confidence_gate")]["rel_width"].dropna()
    gate_item = (
        f"4. **The forecast_confidence_gate bought caution at a price.** With it: saving {num(fu['saving_per_10k'], 2)}, worst single obligation "
        f"{num(fu['worst_obligation_saving_per_10k'], 1)} per 10,000, {int(fu['escalated'])} escalations. Without it: saving {num(fc['saving_per_10k'], 2)}, "
        f"worst {num(fc['worst_obligation_saving_per_10k'], 1)}, {int(fc['escalated'])} escalations. Removing it is "
        f"{'better (interval above zero)' if fc['diff_lo'] > 0 else 'not distinguishable' if fc['diff_hi'] >= 0 else 'worse (interval below zero)'} on saving "
        f"(paired difference {ci(fc['diff_vs_full'], fc['diff_lo'], fc['diff_hi'], 2)}), so on this path the gate cost savings and limited the worst case. That interval comes from the obligation bootstrap alone, rests on a handful of early conversions on one FX path, and no due-week-cluster interval was computed for the ablation, so treat it as suggestive, not established. "
        f"The {len(gw)} forecast-gate escalations had relative widths from {num(gw.min(), 2)} to {num(gw.max(), 2)} (median {num(gw.median(), 2)}) against a limit of 0.5."
    )
    items = [
        f"1. **No demonstrated saving against convert-on-due-date.** {ci(d['saving_per_10k'], d['lo'], d['hi'], 2)} EUR per 10,000, "
        f"{d['verdict']}; the planner acted on {int(d['n_acted'])} of {int(d['n_obligations'])} obligations and made {n_prop} proposals in total. "
        "If the planner has no real edge over waiting, an early conversion costs an extra fixed fee and gains nothing in expectation, so a "
        "positive mean here would be luck, not edge; this sample cannot say which.",
        "2. **The invoice-date result is the path, not the policy** (section 3).",
        _effect(r.ablation, "without worst_case_loss_gate", "worst_case_loss_gate")
        .replace("- ", "3. **", 1)
        .replace(" changed", "** changed", 1)
        if abs(a.loc["full policy", "saving_per_10k"] - a.loc["without worst_case_loss_gate", "saving_per_10k"]) < 1e-9
        else "3. The worst_case_loss_gate changed outcomes (section 4).",
        gate_item,
        "5. **Interval coverage is not shown to be at the nominal 80%.** At the all-currency level the intervals still contain 0.80 (4 weeks 0.72, 8 weeks 0.75), so under-coverage is not established there; it is established for INR at all three horizons, and AUD and EUR at 13 weeks are over-covered (about 0.99 and 0.94, intervals above 0.80). INR seasonal-naive intervals are degenerate (section 2).",
        "6. **Most per-currency comparisons are weaker than the headline.** LightGBM's WAPE gain over seasonal-naive has an interval that includes zero in 7 of 21 cells (AUD 4w, INR at every horizon, SGD at every horizon); 10 of 21 LightGBM bias intervals include zero, so no bias claim is made.",
    ]
    return f"""## 5. Negative and null results

{chr(10).join(items)}

## 6. Limitations

- Synthetic data and a synthetic FX path (generator hard-codes some drift). One path, one 26-week period: the week-cluster interval
  is the more honest one and still cannot represent other market regimes.
- The planner is a rule, not the LLM. This evaluates the forecast, simulation and policy layers; it says nothing about whether the
  model would propose better or worse actions, or resist injection (Days 9 and 10 cover that, also without a live model).
- Replay executes at the last observed daily mid with the assumed spread and fee: no intraday quotes, slippage, partial fills or
  settlement lag. Balances are the Day 15 stand-in. Escalations are not approved by a human here; with a human in the loop the
  outcome depends on that person.
- Forecast sample is small: 12 businesses (9 to 12 per currency), about 18 months of history, 16 calibration weeks, and the labels overlap. Business-cluster bootstrap intervals with so few clusters tend to be too narrow; read them as optimistic.
- 42 paired comparisons and many other intervals are reported without a multiplicity correction; a few intervals excluding zero are expected by chance.
- The savings result rests on 4 acted obligations (5 approved conversions out of 18 proposals); the ablation differences come from 10 flipped decisions. They say little about how the policy behaves at scale.
- The held-out run was seen once before the planner threshold was replaced (section 1), so the held-out period is not pristine for that one constant. Training deliberately stops at the labels realised before calibration, so the forecaster also ignores the most recent months before the test period, which probably makes it worse than a production refit would be.
- Costs are assumptions (50 bps spread, 2.00 fee). Sensitivity to both is in section 3: a fee of 10 turns the mean saving to 0.10 and a fee of 0 raises it to 0.61, and in every row of that grid the interval includes zero.
- **Not done:** Day 8 deferred a real coverage test of the simulated tail to Day 17; this evaluation does not include it (it was not in the Day 17 specification), so the simulator's tail calibration remains undemonstrated.
- The planner's probability comes from the zero-drift Day 5 estimator, which Day 5 found biased on this path; a better-specified planner could do better or worse.
- Only supplier payouts are replayed: no customer-invoice conversions, card top-ups or payroll (INR is the base currency).
- Not independent of the author: the same agent wrote the evaluation, the planner stand-in and the code under test.

## 7. Reproduce

```
python -m tessera_evalkit.evaluate --data data/synthetic --fx data/fx/fx_daily.parquet --out docs/eval --report docs/eval-report.md --seed {C.SEED}
```

(`make eval`.) No network, no model API. Tables are written to `docs/eval/*.csv`, plots to `docs/eval/*.png`, the split, data
hashes and ruleset hashes to `docs/eval/heldout_freeze.json`. The same seed gives byte-identical tables and report.
"""
