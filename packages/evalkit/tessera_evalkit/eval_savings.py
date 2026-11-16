"""Savings and ablation evaluation: turn replay results into costs, savings, intervals and ablation rows.

Units. Every saving is first computed in the SELL currency of its obligation (positive = Tessera paid less than the
baseline) and then converted to EUR at the due-date rate so obligations in different currencies can be added up.
"Per 10,000 units converted" is 10,000 x (sum of saving) / (sum of the due-date baseline's sell units), both in EUR:
a ratio of sums, so a large obligation counts for more than a small one.

Intervals (95%): a paired bootstrap over OBLIGATIONS, as specified, plus a more conservative one over DUE-WEEK clusters.
The obligation bootstrap treats obligations as independent, which they are not: every business faces the same FX path.
The week-cluster interval keeps obligations that share a week together; the held-out path is still ONE realisation of
the FX market, which no resampling can fix. The report shows both.
"""

from __future__ import annotations

from decimal import Decimal

import numpy as np
import pandas as pd
import yaml
from tessera_core.policy import Ruleset, ruleset_from_yaml_texts
from tessera_core.policy.loader import DEFAULT_RULES_DIR

from tessera_evalkit.eval_config import DEV_START, PLANNER, PLANNER_DEV_QUANTILE, PlannerConstants
from tessera_evalkit.eval_replay import dev_p_adverse
from tessera_evalkit.eval_stats import bootstrap_ratio, cluster_weights, group_sums, paired_difference, verdict
from tessera_evalkit.eval_types import Result
from tessera_evalkit.eval_world import FxWorld, Obligation

VARIANTS: dict[str, tuple[str, ...]] = {
    "full policy": (),
    "without forecast_confidence_gate": ("forecast_confidence_gate",),
    "without worst_case_loss_gate": ("worst_case_loss_gate",),
    "without both gates": ("forecast_confidence_gate", "worst_case_loss_gate"),
}


def ruleset_without(*rule_ids: str, loss_ceiling_scale: float = 1.0) -> Ruleset:
    """The shipped ruleset minus the named rules. `loss_ceiling_scale` != 1 rescales the worst_case_loss_gate ceilings
    (a labelled policy SENSITIVITY, never used for the headline numbers)."""
    texts = {p.name: p.read_text(encoding="utf-8") for p in sorted(DEFAULT_RULES_DIR.glob("*.yaml"))}
    missing = [r for r in rule_ids if f"{r}.yaml" not in texts]
    if missing:
        raise ValueError(f"no such rule file: {missing}")
    if loss_ceiling_scale != 1.0:
        spec = yaml.safe_load(texts["worst_case_loss_gate.yaml"])
        ceilings = spec["params"]["max_worst_case_loss"]
        spec["params"]["max_worst_case_loss"] = {
            k: str((Decimal(v) * Decimal(repr(loss_ceiling_scale))).quantize(Decimal("0.01")))
            for k, v in ceilings.items()
        }
        texts["worst_case_loss_gate.yaml"] = yaml.safe_dump(spec, sort_keys=True)
    return ruleset_from_yaml_texts({n: t for n, t in texts.items() if n[: -len(".yaml")] not in rule_ids})


def calibrate_planner(
    dev_obligations: list[Obligation],
    world: FxWorld,
    seed: int,
    base: PlannerConstants = PLANNER,
) -> tuple[PlannerConstants, np.ndarray]:
    """Set the planner's threshold from the DEVELOPMENT period only (eval_config). Returns (constants, dev inputs)."""
    ps = np.array(dev_p_adverse(dev_obligations, world, base.drop_threshold_pct, seed, DEV_START))
    threshold = float(np.quantile(ps, PLANNER_DEV_QUANTILE))
    return PlannerConstants(base.drop_threshold_pct, threshold, base.fraction), ps


def results_frame(results: list[Result]) -> pd.DataFrame:
    rows = []
    for r in results:
        k = r.eur_per_sell
        rows.append(
            {
                "obligation": r.ob.id,
                "business": r.ob.business,
                "sell": r.ob.funding,
                "buy": r.ob.buy,
                "issue": r.ob.issue,
                "due": r.ob.due,
                "due_week": r.ob.due.to_period("W").start_time,
                "sell_due": r.sell_due,
                "sell_invoice": r.sell_invoice,
                "sell_tessera": r.sell_tessera,
                "notional_eur": r.sell_due * k,
                "save_vs_due_eur": (r.sell_due - r.sell_tessera) * k,
                "save_vs_invoice_eur": (r.sell_invoice - r.sell_tessera) * k,
                "invoice_vs_due_eur": (r.sell_due - r.sell_invoice) * k,
                "spread_due_eur": r.spread_due * k,
                "spread_invoice_eur": r.spread_invoice * k,
                "spread_tessera_eur": r.spread_tessera * k,
                "n_conversions": r.n_conversions_tessera,
                "acted": r.n_early > 0,
            }
        )
    return pd.DataFrame(rows)


def decisions_frame(results: list[Result]) -> pd.DataFrame:
    rows = [
        {
            "obligation": d.obligation,
            "day": d.day,
            "sell": d.sell,
            "p_adverse": d.p_adverse,
            "amount": d.amount,
            "verdict": d.verdict,
            "rule_ids": ",".join(d.rule_ids),
            "worst_case_loss_eur": d.worst_case_loss * d.eur_per_sell,
            "amount_eur": d.amount * d.eur_per_sell,
            "tranche_saving_eur": None if d.tranche_saving is None else d.tranche_saving * d.eur_per_sell,
            "rel_width": d.rel_width,
        }
        for r in results
        for d in r.decisions
    ]
    cols = [
        "obligation",
        "day",
        "sell",
        "p_adverse",
        "amount",
        "verdict",
        "rule_ids",
        "worst_case_loss_eur",
        "amount_eur",
        "tranche_saving_eur",
        "rel_width",
    ]
    return pd.DataFrame(rows, columns=cols)


def _per_10k(
    df: pd.DataFrame, col: str, n_boot: int, rng: np.random.Generator, cluster: str | None
) -> dict[str, float]:
    key = df[cluster].astype(str).to_numpy() if cluster else df["obligation"].to_numpy()
    _, s = group_sums(key, df[col].to_numpy(dtype=float), df["notional_eur"].to_numpy(dtype=float))
    w = cluster_weights(len(s), n_boot, rng)
    point, (lo, hi) = bootstrap_ratio(s[:, 0], s[:, 1], w, 10_000.0)
    return {"value": point, "lo": lo, "hi": hi}


def headline(df: pd.DataFrame, n_boot: int, seed: int) -> pd.DataFrame:
    """Saving per 10,000 units converted against each baseline, two intervals, and the share of decisions that win."""
    out = []
    for name, col in (("convert on due date", "save_vs_due_eur"), ("convert on invoice date", "save_vs_invoice_eur")):
        # the same seed for obligation and cluster draws of both baselines keeps the comparison paired
        by_obl = _per_10k(df, col, n_boot, np.random.default_rng(seed), None)
        by_week = _per_10k(df, col, n_boot, np.random.default_rng(seed + 1), "due_week")
        s = df[col].to_numpy()
        acted = df["acted"].to_numpy()
        out.append(
            {
                "baseline": name,
                "n_obligations": len(df),
                "n_acted": int(acted.sum()),
                "saving_per_10k": by_obl["value"],
                "lo": by_obl["lo"],
                "hi": by_obl["hi"],
                "week_lo": by_week["lo"],
                "week_hi": by_week["hi"],
                "share_beat": float((s > 1e-9).mean()),
                "share_tie": float((np.abs(s) <= 1e-9).mean()),
                "share_lose": float((s < -1e-9).mean()),
                "share_beat_when_acted": float((s[acted] > 1e-9).mean()) if acted.any() else float("nan"),
                "verdict": verdict((by_obl["lo"], by_obl["hi"])),
                "verdict_week_clusters": verdict((by_week["lo"], by_week["hi"])),
            }
        )
    return pd.DataFrame(out)


def cost_table(df: pd.DataFrame, fee_eur_per_conversion: float | None = None) -> pd.DataFrame:
    """Total cost of each strategy in EUR, split into the rate-driven part, spread and fees."""
    out = []
    for name, sell_col, spread_col, n in (
        ("convert on due date", "sell_due", "spread_due_eur", None),
        ("convert on invoice date", "sell_invoice", "spread_invoice_eur", None),
        ("Tessera (full policy)", "sell_tessera", "spread_tessera_eur", "n_conversions"),
    ):
        eur = (df[sell_col] * df["notional_eur"] / df["sell_due"]).sum()
        conversions = float(df[n].sum()) if n else float(len(df))
        out.append(
            {"strategy": name, "total_cost_eur": eur, "spread_eur": df[spread_col].sum(), "conversions": conversions}
        )
    t = pd.DataFrame(out)
    return t


def ablation_row(name: str, df: pd.DataFrame, dec: pd.DataFrame, n_boot: int, seed: int) -> dict[str, object]:
    h = headline(df, n_boot, seed).iloc[0]  # vs convert on due date
    s = df["save_vs_due_eur"].to_numpy() / df["notional_eur"].to_numpy() * 10_000.0  # per-obligation, per 10k
    escalated = dec[dec["verdict"] == "ESCALATE"]
    approved = dec[dec["verdict"] == "APPROVE"]
    by_rule = escalated["rule_ids"].str.split(",").explode().value_counts().to_dict() if len(escalated) else {}
    return {
        "variant": name,
        "saving_per_10k": h["saving_per_10k"],
        "lo": h["lo"],
        "hi": h["hi"],
        "p05_obligation_saving_per_10k": float(np.quantile(s, 0.05)),
        "worst_obligation_saving_per_10k": float(s.min()),
        "worst_single_loss_eur": float(max(0.0, -df["save_vs_due_eur"].min())),
        "max_simulated_worst_case_loss_approved_eur": float(approved["worst_case_loss_eur"].max())
        if len(approved)
        else 0.0,
        "proposals": len(dec),
        "approved": len(approved),
        "escalated": len(escalated),
        "blocked": int((dec["verdict"] == "BLOCK").sum()),
        "escalated_by": by_rule,
    }


def tranche_summary(dec: pd.DataFrame) -> dict[str, float]:
    """Decision-level view: each EXECUTED early conversion against buying the same units on the due date."""
    ex = dec[dec["verdict"] == "APPROVE"]
    s = ex["tranche_saving_eur"].to_numpy(dtype=float)
    return {
        "executed": float(len(ex)),
        "beat_due_date": float((s > 0).sum()),
        "total_eur": float(s.sum()) if len(s) else 0.0,
    }


def diff_vs_full(variant: pd.DataFrame, full: pd.DataFrame, n_boot: int, seed: int) -> dict[str, float]:
    """Saving per 10,000 (vs due date) of a variant MINUS the full policy's, paired on shared resamples."""
    assert list(variant["obligation"]) == list(full["obligation"]), "variants must cover the same obligations in order"
    n = len(full)
    w = cluster_weights(n, n_boot, np.random.default_rng(seed + 2))
    den = full["notional_eur"].to_numpy(dtype=float)
    d, (lo, hi) = paired_difference(
        variant["save_vs_due_eur"].to_numpy(dtype=float), den, full["save_vs_due_eur"].to_numpy(dtype=float), den, w
    )
    return {"diff_vs_full": d * 10_000.0, "diff_lo": lo * 10_000.0, "diff_hi": hi * 10_000.0}
