"""Day 17: run the held-out evaluation and write the tables, plots and docs/eval-report.md.

    python -m tessera_evalkit.evaluate --data data/synthetic --fx data/fx/fx_daily.parquet \\
        --out docs/eval --report docs/eval-report.md --seed 7

One command, one seed, no network and no model API: the same inputs give byte-identical tables and report. Nothing here
fits or selects anything on the held-out period (see eval_config for the declared split and constants).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
from tessera_core.forecast.direct_lgbm import target_col
from tessera_core.forecast.splits import make_split

from tessera_evalkit import eval_config as C
from tessera_evalkit.eval_forecast import forecast_table, paired_wape, predict_all, summarise
from tessera_evalkit.eval_frame import BASELINE_COLUMNS, MODEL_FEATURES, build_eval_frame
from tessera_evalkit.eval_plots import ablation_overview, forecast_by_currency, forecast_overview, savings_overview
from tessera_evalkit.eval_replay import replay_policy
from tessera_evalkit.eval_report import write_report
from tessera_evalkit.eval_savings import (
    VARIANTS,
    ablation_row,
    calibrate_planner,
    cost_table,
    decisions_frame,
    diff_vs_full,
    headline,
    results_frame,
    ruleset_without,
    tranche_summary,
)
from tessera_evalkit.eval_types import Costs, Result
from tessera_evalkit.eval_world import FxWorld, Obligation, load_obligations
from tessera_evalkit.loading import load_history

STRESS_CEILING_SCALE = 0.1  # sensitivity: the worst_case_loss_gate ceilings divided by 10


@dataclass
class EvalResults:
    forecast_rows: pd.DataFrame
    forecast_summary: pd.DataFrame
    forecast_paired: pd.DataFrame
    splits: pd.DataFrame
    manifest: dict[str, object]
    planner: dict[str, object]
    headline: pd.DataFrame
    costs: pd.DataFrame
    tranche: dict[str, float]
    per_obligation: pd.DataFrame
    decisions: pd.DataFrame
    ablation: pd.DataFrame
    stress_ablation: pd.DataFrame
    sensitivity: pd.DataFrame


def split_table(frame: pd.DataFrame) -> pd.DataFrame:
    rows = []
    end = C.DATA_END + pd.Timedelta(days=1)
    for h in C.HORIZONS:
        s = make_split(frame["as_of"], h, C.HELDOUT_START, end, C.CAL_WEEKS)
        ok = np.isfinite(frame[target_col(h)].to_numpy(dtype="float64"))
        for stage, idx in (("train", s.train_idx), ("calibration", s.cal_idx), ("test", s.test_idx)):
            sel = idx[ok[idx]]
            d = frame["as_of"].iloc[sel]
            rows.append(
                {
                    "horizon": h,
                    "stage": stage,
                    "rows": len(sel),
                    "first_origin": d.min().date(),
                    "last_origin": d.max().date(),
                }
            )
    return pd.DataFrame(rows)


def _ablation(frames: dict[str, tuple[pd.DataFrame, pd.DataFrame]], n_boot: int, seed: int) -> pd.DataFrame:
    full = frames["full policy"][0]
    rows = []
    for name, (d, e) in frames.items():
        row = ablation_row(name, d, e, n_boot, seed)
        row.update(diff_vs_full(d, full, n_boot, seed))
        rows.append(row)
    return pd.DataFrame(rows)


def run_all(data_dir: Path, fx_path: Path, seed: int = C.SEED, n_boot: int = C.N_BOOT) -> EvalResults:
    history = load_history(data_dir)
    frame = build_eval_frame(history, C.DATA_END)
    end = C.DATA_END + pd.Timedelta(days=1)
    rows, model = predict_all(
        frame, C.HORIZONS, MODEL_FEATURES, BASELINE_COLUMNS, C.HELDOUT_START, end, C.CAL_WEEKS, C.COVERAGE, seed
    )
    f_summary = summarise(rows, C.COVERAGE, n_boot, seed)
    f_paired = paired_wape(rows, C.COVERAGE, n_boot, seed)
    forecasts = forecast_table(model, frame, C.HORIZONS, C.HELDOUT_START)

    world = FxWorld(fx_path)
    dev = load_obligations(data_dir, history, C.DEV_START, C.HELDOUT_START - pd.Timedelta(days=1))
    planner, dev_p = calibrate_planner(dev, world, seed)
    held = load_obligations(data_dir, history, C.HELDOUT_START, C.DATA_END)
    costs = Costs(C.SPREAD_BPS, C.FEE)

    def run(
        obs: list[Obligation],
        rules: tuple[str, ...],
        cost: Costs = costs,
        pl: C.PlannerConstants = planner,
        scale: float = 1.0,
    ) -> list[Result]:
        rs = ruleset_without(*rules, loss_ceiling_scale=scale)
        return replay_policy(obs, world, rs, cost, history.cashflows, forecasts, C.HELDOUT_START, seed, planner=pl)

    apriori = replace(planner, min_p_adverse=C.A_PRIORI_MIN_P_ADVERSE)
    n_apriori = sum(len(r.decisions) for r in run(held, (), pl=apriori))

    runs = {name: run(held, rules) for name, rules in VARIANTS.items()}
    frames = {n: (results_frame(r), decisions_frame(r)) for n, r in runs.items()}
    df, dec = frames["full policy"]
    ablation = _ablation(frames, n_boot, seed)
    tight = {n: run(held, rules, scale=STRESS_CEILING_SCALE) for n, rules in VARIANTS.items()}
    stress_ablation = _ablation({n: (results_frame(r), decisions_frame(r)) for n, r in tight.items()}, n_boot, seed)

    sens = []
    for spread, fee in ((25, "2.00"), (50, "2.00"), (100, "2.00"), (50, "0.00"), (50, "10.00")):
        c = Costs(Decimal(spread), Decimal(fee))
        d2 = results_frame(run(held, (), cost=c))
        h = headline(d2, n_boot, seed).iloc[0]
        sens.append(
            {
                "spread_bps": spread,
                "fee": fee,
                "saving_per_10k": h["saving_per_10k"],
                "lo": h["lo"],
                "hi": h["hi"],
                "n_acted": h["n_acted"],
            }
        )

    man = json.loads((data_dir / "manifest.json").read_text(encoding="utf-8"))
    manifest: dict[str, object] = {
        "heldout_start": str(C.HELDOUT_START.date()),
        "data_end": str(C.DATA_END.date()),
        "dev_start": str(C.DEV_START.date()),
        "horizons": list(C.HORIZONS),
        "calibration_weeks": C.CAL_WEEKS,
        "coverage": C.COVERAGE,
        "seed": seed,
        "bootstrap_replicates": n_boot,
        "spread_bps": str(C.SPREAD_BPS),
        "fee": str(C.FEE),
        "autonomy_level": C.AUTONOMY_LEVEL,
        "sim_paths": C.SIM_PATHS,
        "range_sims": C.RANGE_SIMS,
        "synthetic_data_sha256": man["sha256"],
        "fx_source": man.get("fx_source"),
        "ruleset_hashes": {n: ruleset_without(*r).hash for n, r in VARIANTS.items()},
    }
    planner_info: dict[str, object] = {
        "drop_threshold_pct": planner.drop_threshold_pct,
        "fraction": planner.fraction,
        "min_p_adverse": planner.min_p_adverse,
        "dev_decisions": len(dev_p),
        "dev_obligations": len(dev),
        "dev_quantile": C.PLANNER_DEV_QUANTILE,
        "dev_p_max": float(dev_p.max()),
        "a_priori": C.A_PRIORI_MIN_P_ADVERSE,
        "a_priori_heldout_proposals": n_apriori,
        "heldout_obligations": len(held),
        "stress_ceiling_scale": STRESS_CEILING_SCALE,
    }
    return EvalResults(
        forecast_rows=rows,
        forecast_summary=f_summary,
        forecast_paired=f_paired,
        splits=split_table(frame),
        manifest=manifest,
        planner=planner_info,
        headline=headline(df, n_boot, seed),
        costs=cost_table(df),
        tranche=tranche_summary(dec),
        per_obligation=df,
        decisions=dec,
        ablation=ablation,
        stress_ablation=stress_ablation,
        sensitivity=pd.DataFrame(sens),
    )


def write_outputs(r: EvalResults, out: Path, report: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tables = {
        "forecast_summary": r.forecast_summary,
        "forecast_paired_wape": r.forecast_paired,
        "splits": r.splits,
        "savings_headline": r.headline,
        "savings_costs": r.costs,
        "savings_per_obligation": r.per_obligation,
        "savings_decisions": r.decisions,
        "ablation": r.ablation.assign(escalated_by=r.ablation["escalated_by"].astype(str)),
        "ablation_stress": r.stress_ablation.assign(escalated_by=r.stress_ablation["escalated_by"].astype(str)),
        "sensitivity": r.sensitivity,
    }
    for name, t in tables.items():
        t.to_csv(out / f"{name}.csv", index=False, float_format="%.6f")
    (out / "heldout_freeze.json").write_text(
        json.dumps({"manifest": r.manifest, "planner": r.planner}, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    forecast_overview(r.forecast_summary, out / "forecast_overview.png", C.COVERAGE)
    forecast_by_currency(r.forecast_summary, out / "forecast_by_currency.png", C.COVERAGE)
    savings_overview(r.headline, r.per_obligation, out / "savings_overview.png")
    ablation_overview(r.ablation, out / "ablation_overview.png")
    ablation_overview(r.stress_ablation, out / "ablation_stress.png", title_suffix=" (ceilings / 10)")
    write_report(r, report, out)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data/synthetic")
    ap.add_argument("--fx", default="data/fx/fx_daily.parquet")
    ap.add_argument("--out", default="docs/eval")
    ap.add_argument("--report", default="docs/eval-report.md")
    ap.add_argument("--seed", type=int, default=C.SEED)
    ap.add_argument("--n-boot", type=int, default=C.N_BOOT)
    a = ap.parse_args()
    write_outputs(run_all(Path(a.data), Path(a.fx), a.seed, a.n_boot), Path(a.out), Path(a.report))
    print(f"wrote {a.report} and {a.out}/")


if __name__ == "__main__":
    main()
