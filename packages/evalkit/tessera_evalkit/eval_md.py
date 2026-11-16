"""Small markdown helpers for the generated report: fixed number formats, intervals, tables."""

from __future__ import annotations

import math
from collections.abc import Sequence


def num(x: float, nd: int = 3) -> str:
    return "n/a" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:,.{nd}f}"


def ci(x: float, lo: float, hi: float, nd: int = 3) -> str:
    return f"{num(x, nd)} [{num(lo, nd)}, {num(hi, nd)}]"


def pct(x: float, nd: int = 1) -> str:
    return "n/a" if x is None or math.isnan(x) else f"{100.0 * x:.{nd}f}%"


def table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


MODEL_NAME = {
    "lightgbm_conformal": "LightGBM + conformal",
    "naive_conformal": "naive + conformal",
    "seasonal_naive_conformal": "seasonal-naive + conformal",
}
