"""Day 17 evaluation constants. Every number a result depends on is declared HERE, before any result is seen.

Nothing in this file may be changed in response to a held-out result. If a constant must change, the report's
"what changed after seeing results" section has to say so (there is none today). The held-out period is final:
no model, threshold or planner constant was chosen with it in view.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

SEED = 7

# ---- held-out period -----------------------------------------------------------------------------------------
# Forecast origins on or after HELDOUT_START are test origins. The forecaster is trained on origins whose labels were
# realised before the calibration window, calibrated on the 16 weeks before HELDOUT_START (see forecast/splits.py),
# and never sees a row from HELDOUT_START on. FX savings are replayed on obligations ISSUED on or after it.
HELDOUT_START = pd.Timestamp("2026-01-04")  # a Sunday, so it is also a forecast origin
DATA_END = pd.Timestamp("2026-06-30")
HORIZONS = (4, 8, 13)  # weeks: the 30 / 60 / 90 day marks
CAL_WEEKS = 16
COVERAGE = 0.8
N_BOOT = 4000  # bootstrap replicates

# ---- cost model (assumptions, NOT measured Airwallex prices) ---------------------------------------------------
SPREAD_BPS = Decimal(50)  # the Day 1 mock's figure, also SimulationConfig's default
FEE = Decimal("2.00")  # fixed fee per conversion, in the sell currency

# ---- replay -----------------------------------------------------------------------------------------------------
AUTONOMY_LEVEL = 2  # L2 standard: CONVERT and PAY_SUPPLIER within the L2 caps
SIM_PATHS = 5000
RANGE_SIMS = 2000
FUNDING_CURRENCIES = (
    "USD",
    "EUR",
    "GBP",
)  # a business funds supplier payouts from the receivable currency it earns most in


@dataclass(frozen=True)
class PlannerConstants:
    """The scripted planner that stands in for the LLM. Fixed a priori; never tuned on the held-out period."""

    drop_threshold_pct: float = 2.0  # "adverse move": the sell currency loses 2% against the buy currency
    # Convert only when P(adverse move) is at least this. None until `calibrate_planner` sets it from the DEV period.
    # The a-priori value 0.20 produced ZERO proposals in the held-out period, so it was replaced (disclosed in the
    # report) by the PLANNER_DEV_QUANTILE of the planner's own input on the development period (ends before the held-out
    # start).
    min_p_adverse: float | None = None
    fraction: float = 0.40  # of the remaining need ("convert 40% now, wait on the rest")


PLANNER = PlannerConstants()
A_PRIORI_MIN_P_ADVERSE = 0.20  # the first constant tried; kept here so the report can say what happened to it
DEV_START = pd.Timestamp(
    "2025-02-01"
)  # first development-period decision day; the period ends the day before HELDOUT_START
PLANNER_DEV_QUANTILE = 0.75  # act on roughly the top quarter of development-period decisions by P(adverse move)

SENSITIVITY_SPREADS_BPS = (25, 50, 100)
SENSITIVITY_FEES = ("0.00", "2.00", "10.00")
