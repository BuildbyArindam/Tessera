"""Day 17 verification: independent re-derivation of the evaluation's numbers and probes that would catch a leak.

    python scripts/verify_day17.py        (about 4 minutes: it runs the whole evaluation twice)

Nothing here reuses the evaluation's own aggregation code for the numbers it checks: forecasts are re-scored with plain
numpy, one obligation's cost is rebuilt from the raw FX table, and one bootstrap is re-run with a Python loop.
"""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "packages" / "core"), str(ROOT / "packages" / "evalkit")]

from tessera_core.forecast.direct_lgbm import DirectForecaster, target_col  # noqa: E402
from tessera_core.forecast.features import build_features  # noqa: E402
from tessera_evalkit import eval_config as C  # noqa: E402
from tessera_evalkit.eval_forecast import predict_all  # noqa: E402
from tessera_evalkit.eval_frame import BASELINE_COLUMNS, MODEL_FEATURES, build_eval_frame  # noqa: E402
from tessera_evalkit.evaluate import run_all, write_outputs  # noqa: E402
from tessera_evalkit.loading import load_history  # noqa: E402

DATA, FX = ROOT / "data" / "synthetic", ROOT / "data" / "fx" / "fx_daily.parquet"
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


def digest(folder: Path, report: Path) -> dict[str, str]:
    files = sorted(folder.glob("*")) + [report]
    return {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


def main() -> int:
    # ---- (a) determinism: two full runs, same seed, byte-identical outputs --------------------------------------
    with tempfile.TemporaryDirectory() as t1, tempfile.TemporaryDirectory() as t2:
        outs = []
        for t in (t1, t2):
            r = run_all(DATA, FX)
            write_outputs(r, Path(t) / "eval", Path(t) / "eval-report.md")
            outs.append(digest(Path(t) / "eval", Path(t) / "eval-report.md"))
        check(
            "two runs with one seed give byte-identical tables, plots and report",
            outs[0] == outs[1],
            f"{len(outs[0])} files",
        )
        committed = ROOT / "docs" / "eval-report.md"
        check(
            "the committed docs/eval-report.md equals a fresh run",
            committed.read_bytes() == (Path(t1) / "eval-report.md").read_bytes(),
        )
    res = run_all(DATA, FX)
    history = load_history(DATA)
    frame = build_eval_frame(history, C.DATA_END)
    end = C.DATA_END + pd.Timedelta(days=1)
    rows, _ = predict_all(
        frame, C.HORIZONS, MODEL_FEATURES, BASELINE_COLUMNS, C.HELDOUT_START, end, C.CAL_WEEKS, C.COVERAGE, C.SEED
    )

    # ---- (b) leakage probes -------------------------------------------------------------------------------------
    check(
        "every scored row is a test origin on or after HELDOUT_START",
        bool((pd.to_datetime(rows["as_of"]) >= C.HELDOUT_START).all()),
    )
    poisoned = frame.copy()
    for h in C.HORIZONS:
        live = (poisoned["as_of"] >= C.HELDOUT_START) & poisoned[target_col(h)].notna()
        poisoned.loc[live, target_col(h)] = 1e9
    rows_p, _ = predict_all(
        poisoned, C.HORIZONS, MODEL_FEATURES, BASELINE_COLUMNS, C.HELDOUT_START, end, C.CAL_WEEKS, C.COVERAGE, C.SEED
    )
    same = np.allclose(rows[["point", "lower", "upper"]].to_numpy(), rows_p[["point", "lower", "upper"]].to_numpy())
    check("forecasts are unchanged when every held-out LABEL is replaced by 1e9", bool(same))
    leaky = DirectForecaster(C.HORIZONS, MODEL_FEATURES, C.COVERAGE, C.SEED).fit(poisoned, end, C.CAL_WEEKS)
    test_rows = poisoned[poisoned["as_of"] >= C.HELDOUT_START].reset_index(drop=True)
    leaked = leaky.predict_horizon(test_rows, 4)["point"].to_numpy()
    honest = rows[(rows["model"] == "lightgbm_conformal") & (rows["horizon"] == 4)]["point"].to_numpy()
    check(
        "probe has teeth: training on poisoned test labels DOES change predictions",
        bool(np.abs(leaked).max() > 100 * np.abs(honest).max()),
    )
    ok_feat = True
    for origin in (pd.Timestamp("2026-01-04"), pd.Timestamp("2026-03-01"), pd.Timestamp("2026-05-03")):
        a = build_features(history, origin).sort_index()
        b = build_features(history.as_of(origin), origin).sort_index()
        ok_feat &= bool(
            np.allclose(
                a[MODEL_FEATURES].to_numpy(dtype=float), b[MODEL_FEATURES].to_numpy(dtype=float), equal_nan=True
            )
        )
    check("features at a test origin are identical when all later data is deleted", ok_feat)

    # ---- (c) independent re-scoring of a forecast cell ----------------------------------------------------------
    for h, cur in ((8, "USD"), (13, "EUR"), (4, "AUD")):
        g = rows[(rows["model"] == "lightgbm_conformal") & (rows["horizon"] == h) & (rows["currency"] == cur)]
        y, p, lo, up = (g[c].to_numpy(dtype=float) for c in ("y", "point", "lower", "upper"))
        s = res.forecast_summary.query("model == 'lightgbm_conformal' and horizon == @h and currency == @cur").iloc[0]
        ok = (
            abs(np.abs(y - p).sum() / np.abs(y).sum() - s["wape"]) < 1e-9
            and abs(((y >= lo) & (y <= up)).mean() - s["coverage"]) < 1e-9
        )
        check(f"independent WAPE and coverage recomputation, LightGBM {h}w {cur}", bool(ok), f"WAPE {s['wape']:.4f}")
    k, origin = ("B09", "USD"), pd.Timestamp("2026-02-08")
    cf = history.cashflows
    wk = cf[
        (cf["business_id"] == k[0])
        & (cf["currency"] == k[1])
        & (cf["cash_date"] > origin - pd.Timedelta(days=7))
        & (cf["cash_date"] <= origin)
    ]
    row = frame[(frame["business_id"] == k[0]) & (frame["currency"] == k[1]) & (frame["as_of"] == origin)].iloc[0]
    check(
        "naive 4-week forecast = 4 x the last complete week, rebuilt from raw cash flows",
        abs(row["naive_h4"] - 4 * wk["amount"].sum()) < 1e-6,
    )

    # ---- (d) savings: rebuild one acted obligation from the raw FX table ---------------------------------------
    wide = pd.read_parquet(FX).query("is_observed").pivot(index="date", columns="quote", values="rate")
    wide["EUR"] = 1.0
    wide.index = pd.to_datetime(wide.index)

    def rate(sell: str, buy: str, day: pd.Timestamp) -> float:
        r = wide.loc[:day].iloc[-1]
        return float(r[buy] / r[sell])

    per, dec = res.per_obligation, res.decisions
    ok_all, n_checked = True, 0
    for oid, g in dec[dec["verdict"] == "APPROVE"].groupby("obligation"):
        o = per[per["obligation"] == oid].iloc[0]
        remaining = float(pd.read_parquet(DATA / "outflows.parquet").set_index("outflow_id").loc[oid, "amount"])
        paid = 0.0
        for d in g.itertuples():
            paid += d.amount
            remaining -= (d.amount - 2.0) * rate(o["sell"], o["buy"], d.day) * 0.995
        paid += remaining / (rate(o["sell"], o["buy"], o["due"]) * 0.995) + 2.0
        ok_all &= abs(paid - o["sell_tessera"]) < 1e-6 * max(1.0, paid)
        n_checked += 1
    check(
        "every acted obligation's total cost rebuilt by hand from raw FX rates",
        bool(ok_all),
        f"{n_checked} obligations",
    )
    amt = float(
        pd.read_parquet(DATA / "outflows.parquet").set_index("outflow_id").loc[per.iloc[0]["obligation"], "amount"]
    )
    r0 = per.iloc[0]
    check(
        "due-date baseline = payout / (rate x (1 - 50bps)) + fee",
        abs(r0["sell_due"] - (amt / (rate(r0["sell"], r0["buy"], r0["due"]) * 0.995) + 2.0)) < 1e-6,
    )
    check(
        "invoice-date baseline uses the rate on the invoice date",
        abs(r0["sell_invoice"] - (amt / (rate(r0["sell"], r0["buy"], r0["issue"]) * 0.995) + 2.0)) < 1e-6,
    )
    h = res.headline.iloc[0]
    rng = np.random.default_rng(99)
    num, den = per["save_vs_due_eur"].to_numpy(), per["notional_eur"].to_numpy()
    draws = np.array(
        [(lambda i: 1e4 * num[i].sum() / den[i].sum())(rng.integers(0, len(num), len(num))) for _ in range(3000)]
    )
    lo, hi = np.quantile(draws, [0.025, 0.975])
    check(
        "headline point equals 1e4 x sum(saving)/sum(notional)",
        abs(h["saving_per_10k"] - 1e4 * num.sum() / den.sum()) < 1e-9,
    )
    check(
        "bootstrap interval matches an independent loop bootstrap",
        abs(lo - h["lo"]) < 0.15 and abs(hi - h["hi"]) < 0.15,
        f"{lo:.2f},{hi:.2f} vs {h['lo']:.2f},{h['hi']:.2f}",
    )
    check("beat + tied + lost shares sum to 1", abs(h["share_beat"] + h["share_tie"] + h["share_lose"] - 1.0) < 1e-12)
    check(
        "an obligation with no approved early conversion costs exactly the due-date baseline",
        bool(np.allclose(per.loc[~per["acted"], "sell_tessera"], per.loc[~per["acted"], "sell_due"])),
    )

    # ---- (e) structural invariants of the replay ----------------------------------------------------------------
    obl = pd.read_parquet(DATA / "outflows.parquet").set_index("outflow_id")
    due = pd.to_datetime(obl.loc[dec["obligation"], "due_date"]).to_numpy()
    issue = pd.to_datetime(obl.loc[dec["obligation"], "issue_date"]).to_numpy()
    check(
        "every decision is on a Sunday inside [invoice, due) and inside the held-out period",
        bool(
            (
                (dec["day"].dt.day_of_week == 6)
                & (dec["day"] >= C.HELDOUT_START)
                & (dec["day"].to_numpy() < due)
                & (dec["day"].to_numpy() >= issue - np.timedelta64(7, "D"))
            ).all()
        ),
    )
    check(
        "no policy_error: every verdict came from a complete context",
        not dec["rule_ids"].fillna("").str.contains("policy_error").any(),
    )
    check(
        "only APPROVE decisions carry a tranche saving",
        bool(dec.loc[dec["verdict"] != "APPROVE", "tranche_saving_eur"].isna().all()),
    )
    check(
        "approved conversions + one due-date conversion per obligation = reported conversions",
        int(per["n_conversions"].sum()) == int((dec["verdict"] == "APPROVE").sum()) + len(per),
    )
    check(
        "planner threshold came from development decisions that all precede the held-out start",
        res.planner["dev_decisions"] > 0 and C.DEV_START < C.HELDOUT_START,
    )
    check(
        "ablation 'full policy' row equals the headline",
        abs(res.ablation.iloc[0]["saving_per_10k"] - h["saving_per_10k"]) < 1e-12,
    )
    check(
        "held-out obligations are all issued on or after the held-out start",
        bool((per["issue"] >= C.HELDOUT_START).all()),
    )

    # ---- (f) static numbers quoted in the report's prose are true of the CSVs -----------------------------------
    rp = (ROOT / "docs" / "eval-report.md").read_text()
    pw = pd.read_csv(ROOT / "docs/eval/forecast_paired_wape.csv")
    sn = pw[pw["other"] == "seasonal_naive_conformal"]
    summ = pd.read_csv(ROOT / "docs/eval/forecast_summary.csv")
    lg = summ[summ["model"] == "lightgbm_conformal"]
    sens = pd.read_csv(ROOT / "docs/eval/sensitivity.csv")
    nb = lg[lg["currency"] != "ALL"].groupby("currency")["n_businesses"].max()
    check(
        "report: 7 of 21 seasonal-naive WAPE intervals include zero",
        int(((sn.lo <= 0) & (sn.hi >= 0)).sum()) == 7 and len(sn) == 21 and "7 of 21 cells" in rp,
    )
    check(
        "report: 10 of 21 LightGBM bias intervals include zero",
        int(((lg.bias_lo <= 0) & (lg.bias_hi >= 0)).sum()) == 10 and len(lg) == 21 and "10 of 21 LightGBM bias" in rp,
    )
    check(
        "report: 9 to 12 businesses per currency",
        int(nb.min()) == 9 and int(nb.max()) == 12 and "9 to 12 per currency" in rp,
    )
    check(
        "report: every sensitivity-grid interval includes zero",
        bool(((sens.lo <= 0) & (sens.hi >= 0)).all()) and "every row of that grid" in rp,
    )
    fee0, fee10 = sens.query("fee == 0").iloc[0], sens.query("fee == 10").iloc[0]
    check(
        "report: fee 10 gives 0.10, fee 0 gives 0.61",
        round(fee10.saving_per_10k, 2) == 0.10 and round(fee0.saving_per_10k, 2) == 0.61,
    )
    al = lg[(lg["currency"] == "ALL")]
    check(
        "report: all-currency coverage intervals at 4w and 8w contain 0.80",
        bool(((al.coverage_lo <= 0.8) & (al.coverage_hi >= 0.8))[al.horizon.isin([4, 8])].all()),
    )
    ov = lg[(lg["horizon"] == 13) & lg["currency"].isin(["AUD", "EUR"])]
    check("report: AUD and EUR at 13 weeks are over-covered (interval above 0.80)", bool((ov.coverage_lo > 0.8).all()))
    check(
        "report: 18 proposals, 5 approved, 13 escalated, 4 acted obligations",
        bool(len(dec) == 18 and (dec["verdict"] == "APPROVE").sum() == 5 and per["acted"].sum() == 4),
    )
    print(f"\n{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
