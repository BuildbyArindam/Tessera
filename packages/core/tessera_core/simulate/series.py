"""FxSeries: observed daily mid rates for ONE pair, with a strict as-of view.

Convention (same as the policy rules and FxQuote): the pair is "SELL/BUY" and a value is the number
of BUY units per 1 SELL unit. The series must hold OBSERVED business days only. Forward-filled
weekend rows (Day 2's `is_observed == False`) must be dropped by the caller: a filled weekend is a
fake zero return and would understate volatility.

`up_to(as_of)` is the single choke point that keeps the future out of a simulation, in the same
spirit as `History.as_of` in the forecaster: everything downstream gets the sliced series only.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import numpy as np
import numpy.typing as npt

FloatArray = npt.NDArray[np.float64]
DayArray = npt.NDArray[np.datetime64]

# The newest observation may be at most this many calendar days older than the date asked about
# (covers a weekend plus a holiday). Older than that and the series has a hole or has not been
# refreshed, and simulating from it would quietly use a stale spot.
MAX_STALE_DAYS = 7
_PAIR = re.compile(r"[A-Z]{3}/[A-Z]{3}")


class SimulationInputError(ValueError):
    """The inputs cannot be simulated (bad proposal params, short or stale history, bad dates)."""


@dataclass(frozen=True, eq=False)
class FxSeries:
    pair: str
    days: DayArray  # datetime64[D], strictly increasing
    values: FloatArray  # mid rate, buy units per 1 sell unit; finite and > 0

    def __post_init__(self) -> None:
        if _PAIR.fullmatch(self.pair) is None or self.pair[:3] == self.pair[4:]:
            raise SimulationInputError(f"pair must look like 'USD/EUR' with two different codes, got {self.pair!r}")
        if self.days.ndim != 1 or self.values.ndim != 1 or len(self.days) != len(self.values):
            raise SimulationInputError("days and values must be 1-D and the same length")
        if len(self.days) == 0:
            raise SimulationInputError("an FX series needs at least one observation")
        if not np.isfinite(self.values).all() or (self.values <= 0).any():
            raise SimulationInputError("rates must be finite and strictly positive")
        if (np.diff(self.days.astype("int64")) <= 0).any():
            raise SimulationInputError("observation days must be strictly increasing (no duplicates)")
        self.days.setflags(write=False)
        self.values.setflags(write=False)

    @classmethod
    def from_observations(cls, pair: str, observations: Sequence[tuple[date, float]]) -> "FxSeries":
        days = np.array([np.datetime64(d, "D") for d, _ in observations], dtype="datetime64[D]")
        values = np.array([v for _, v in observations], dtype=np.float64)
        return cls(pair=pair, days=days, values=values)

    def __len__(self) -> int:
        return len(self.days)

    @property
    def last_day(self) -> date:
        return self.days[-1].astype(date)  # type: ignore[no-any-return]

    def up_to(self, as_of: date) -> "FxSeries":
        """The observations dated on or before `as_of`. Nothing later survives."""
        cut = int(np.searchsorted(self.days, np.datetime64(as_of, "D"), side="right"))
        if cut == 0:
            raise SimulationInputError(f"no {self.pair} observation on or before {as_of}")
        return FxSeries(self.pair, self.days[:cut], self.values[:cut])

    def latest_on_or_before(self, day: date) -> tuple[date, float]:
        """(date, mid rate) of the newest observation on or before `day`, refusing a stale one."""
        view = self.up_to(day)
        seen = view.last_day
        if (day - seen).days > MAX_STALE_DAYS:
            raise SimulationInputError(
                f"newest {self.pair} observation before {day} is {seen}, more than {MAX_STALE_DAYS} days older"
            )
        return seen, float(view.values[-1])

    def log_returns(self) -> FloatArray:
        out: FloatArray = np.diff(np.log(self.values))
        return out
