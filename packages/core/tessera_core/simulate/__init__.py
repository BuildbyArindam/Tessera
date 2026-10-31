"""tessera_core.simulate: replay a proposal over historical FX paths before anything runs.

Pure NumPy and Pydantic: no network, database or LLM, no clock, no global state. `simulate` is the
Monte-Carlo used by the policy path; `replay` runs one realised path for backtests (Day 17).
"""

from tessera_core.simulate.bootstrap import block_bootstrap_log_returns
from tessera_core.simulate.config import SimulationConfig
from tessera_core.simulate.engine import MAX_HORIZON_DAYS, MIN_PATHS, MIN_RETURNS, replay, simulate
from tessera_core.simulate.exposure import Exposure, exposure_from_proposal
from tessera_core.simulate.report import ReplayReport, SimulationReport
from tessera_core.simulate.series import MAX_STALE_DAYS, FxSeries, SimulationInputError

__all__ = [
    "MAX_HORIZON_DAYS",
    "MAX_STALE_DAYS",
    "MIN_PATHS",
    "MIN_RETURNS",
    "Exposure",
    "FxSeries",
    "ReplayReport",
    "SimulationConfig",
    "SimulationInputError",
    "SimulationReport",
    "block_bootstrap_log_returns",
    "exposure_from_proposal",
    "replay",
    "simulate",
]
