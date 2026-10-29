"""Historical daily FX: ECB reference rates (via Frankfurter) or a synthetic fallback.

Source selection (env var ``TESSERA_FX_SOURCE``):

* ``auto`` (default) - use raw ECB files in ``data/fx/raw`` if present, else try
  the Frankfurter API; if the network is unavailable, fall back to the
  synthetic series and log a loud warning.
* ``ecb``       - ECB only. Raises if neither cache nor network works.
* ``synthetic`` - never touch the network; always generate the fallback.

Layout::

    data/fx/raw/frankfurter_EUR_<start>_<end>.json   raw API response, untouched
    data/fx/fx_daily.parquet                          cleaned, forward-filled
    data/fx/fx_meta.json                              source, seed, window, notes

Cleaned parquet is LONG format with EUR as base (``EURUSD`` ... ``EURAUD``).
``is_observed`` is False for weekends/holidays that were forward-filled.
Cross rates (e.g. USD->INR) come from :class:`FxTable`.

SYNTHETIC FALLBACK (clearly labelled ``source == "synthetic"``): correlated
geometric Brownian motion on business days with a 3-state Markov volatility
regime (calm / normal / stress). Anchors, drifts and vols are round numbers
chosen to be plausible, NOT fitted to real data. Do not quote results from it
as real FX performance.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from ._compat import Currency
from .calendars import business_days
from .records import FxRateRec
from .rng import rng_for

log = logging.getLogger(__name__)

ENV_VAR = "TESSERA_FX_SOURCE"
FRANKFURTER_URL = "https://api.frankfurter.app/{start}..{end}?from=EUR&to={quotes}"
QUOTES = ("USD", "GBP", "INR", "SGD", "AUD")
SYNTH_NOTE = (
    "SYNTHETIC: correlated GBM with 3-state volatility regimes; not real market data."
)

# --- synthetic model parameters (documented, not fitted) -----------------------
_ANCHOR = {"USD": 1.08, "GBP": 0.85, "INR": 90.0, "SGD": 1.45, "AUD": 1.62}
_DRIFT = {"USD": 0.0, "GBP": 0.0, "INR": 0.03, "SGD": 0.0, "AUD": 0.0}  # per year
_BASE_VOL = {"USD": 0.05, "GBP": 0.045, "INR": 0.035, "SGD": 0.04, "AUD": 0.065}
_CORR = np.array(  # order: USD, GBP, INR, SGD, AUD
    [
        [1.00, 0.55, 0.65, 0.75, 0.60],
        [0.55, 1.00, 0.35, 0.45, 0.45],
        [0.65, 0.35, 1.00, 0.55, 0.40],
        [0.75, 0.45, 0.55, 1.00, 0.60],
        [0.60, 0.45, 0.40, 0.60, 1.00],
    ]
)
_REGIME_MULT = np.array([0.6, 1.0, 1.8])  # calm, normal, stress
_REGIME_P = np.array(  # daily transition matrix (rows sum to 1)
    [
        [0.985, 0.015, 0.000],
        [0.008, 0.985, 0.007],
        [0.000, 0.035, 0.965],
    ]
)


class FxUnavailable(RuntimeError):
    pass


# ------------------------------------------------------------------ ECB path ---
def parse_frankfurter(payload: dict) -> dict[date, dict[str, float]]:
    """Frankfurter time-series JSON -> {date: {quote: rate}} (EUR base)."""
    if payload.get("base") != "EUR" or "rates" not in payload:
        raise ValueError("unexpected Frankfurter payload (need base=EUR and rates)")
    out: dict[date, dict[str, float]] = {}
    for day, quotes in payload["rates"].items():
        out[date.fromisoformat(day)] = {k: float(v) for k, v in quotes.items()}
    if not out:
        raise ValueError("Frankfurter payload has no rates")
    return out


def _fetch_frankfurter(start: date, end: date, raw_dir: Path, timeout: float = 20.0) -> Path:
    url = FRANKFURTER_URL.format(start=start, end=end, quotes=",".join(QUOTES))
    req = urllib.request.Request(url, headers={"User-Agent": "tessera-evalkit/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            body = resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FxUnavailable(f"Frankfurter unreachable: {exc}") from exc
    parse_frankfurter(json.loads(body))  # validate before persisting
    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"frankfurter_EUR_{start}_{end}.json"
    path.write_bytes(body)
    return path


def _find_cached_raw(raw_dir: Path, start: date, end: date) -> Path | None:
    for p in sorted(raw_dir.glob("frankfurter_EUR_*.json")):
        _, _, s, e = p.stem.split("_")
        if date.fromisoformat(s) <= start and date.fromisoformat(e) >= end:
            return p
    return None


# ------------------------------------------------------------ synthetic path ---
def synthetic_observations(seed: int, start: date, end: date) -> dict[date, dict[str, float]]:
    days = business_days(start, end)
    n, k = len(days), len(QUOTES)
    rng = rng_for(seed, "fx", "synthetic")

    # Markov volatility regime path (shared across pairs).
    regime = np.empty(n, dtype=int)
    state = 1
    u = rng.random(n)
    for i in range(n):
        state = int(np.searchsorted(np.cumsum(_REGIME_P[state]), u[i]))
        state = min(state, 2)
        regime[i] = state

    chol = np.linalg.cholesky(_CORR)
    z = rng.standard_normal((n, k)) @ chol.T
    dt = 1.0 / 252.0
    vol = np.array([_BASE_VOL[q] for q in QUOTES])[None, :] * _REGIME_MULT[regime][:, None]
    mu = np.array([_DRIFT[q] for q in QUOTES])[None, :]
    log_ret = (mu - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * z
    levels = np.array([_ANCHOR[q] for q in QUOTES])[None, :] * np.exp(np.cumsum(log_ret, axis=0))

    return {d: {q: float(levels[i, j]) for j, q in enumerate(QUOTES)} for i, d in enumerate(days)}


# ----------------------------------------------------------------- cleaning ---
def _forward_fill(obs: dict[date, dict[str, float]], start: date, end: date):
    rows: list[dict] = []
    last: dict[str, float] | None = None
    d = start
    while d <= end:
        if d in obs:
            last, observed = obs[d], True
        else:
            observed = False
        if last is not None:  # leading days before first observation stay absent
            for q in QUOTES:
                rows.append((d, q, round(last[q], 6), observed))
        d += timedelta(days=1)
    return rows


def _write_parquet(rows, source: str, path: Path) -> list[FxRateRec]:
    recs = [
        FxRateRec(
            date=d, base=Currency.EUR, quote=Currency(q), pair=f"EUR{q}",
            rate=r, is_observed=obs, source=source,  # type: ignore[arg-type]
        )
        for d, q, r, obs in rows
    ]
    table = pa.table(
        {
            "date": pa.array([r.date for r in recs], pa.date32()),
            "base": [r.base for r in recs],
            "quote": [r.quote for r in recs],
            "pair": [r.pair for r in recs],
            "rate": pa.array([r.rate for r in recs], pa.float64()),
            "is_observed": pa.array([r.is_observed for r in recs], pa.bool_()),
            "source": [r.source for r in recs],
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd")
    return recs


@dataclass(frozen=True)
class FxTable:
    """In-memory daily rate lookup with cross-rate conversion."""

    eur_per: dict[date, dict[str, float]]  # date -> {"USD": EURUSD, ..., "EUR": 1.0}
    source: str
    first: date
    last: date

    def rate(self, d: date, frm: str, to: str) -> float:
        """Units of ``to`` per 1 unit of ``frm`` on date ``d`` (clamped into range)."""
        if frm == to:
            return 1.0
        d = min(max(d, self.first), self.last)
        row = self.eur_per[d]
        return row[to] / row[frm]


def table_from_parquet(path: Path) -> FxTable:
    t = pq.read_table(path).to_pydict()
    eur_per: dict[date, dict[str, float]] = {}
    for d, quote, rate in zip(t["date"], t["quote"], t["rate"]):
        eur_per.setdefault(d, {"EUR": 1.0})[quote] = rate
    days = sorted(eur_per)
    return FxTable(eur_per, t["source"][0], days[0], days[-1])


# --------------------------------------------------------------------- main ---
def load_fx(
    start: date,
    end: date,
    *,
    seed: int,
    data_dir: Path = Path("data"),
    source: str | None = None,
) -> FxTable:
    """Build (or rebuild) data/fx/fx_daily.parquet and return a lookup table."""
    mode = (source or os.environ.get(ENV_VAR, "auto")).lower()
    if mode not in {"auto", "ecb", "synthetic"}:
        raise ValueError(f"{ENV_VAR} must be auto|ecb|synthetic, got {mode!r}")

    fx_dir = data_dir / "fx"
    raw_dir = fx_dir / "raw"
    # Pull in a lead-in so forward-fill covers the first day of the window.
    fetch_start = start - timedelta(days=7)
    obs: dict[date, dict[str, float]] | None = None
    used = "synthetic"
    raw_name: str | None = None

    if mode in {"auto", "ecb"}:
        try:
            raw = _find_cached_raw(raw_dir, fetch_start, end) or _fetch_frankfurter(
                fetch_start, end, raw_dir
            )
            obs = parse_frankfurter(json.loads(raw.read_text()))
            used, raw_name = "ecb", raw.name
        except (FxUnavailable, ValueError, json.JSONDecodeError) as exc:
            if mode == "ecb":
                raise FxUnavailable(f"{ENV_VAR}=ecb but ECB data unavailable: {exc}") from exc
            log.warning("ECB FX unavailable (%s). Falling back to SYNTHETIC series.", exc)

    if obs is None:
        obs = synthetic_observations(seed, fetch_start, end)

    rows = _forward_fill(obs, fetch_start, end)
    _write_parquet(rows, used, fx_dir / "fx_daily.parquet")
    meta = {
        "source": used,
        "requested_mode": mode,
        "seed": seed if used == "synthetic" else None,
        "window": [str(fetch_start), str(end)],
        "raw_file": raw_name,
        "pairs": [f"EUR{q}" for q in QUOTES],
        "note": SYNTH_NOTE if used == "synthetic" else "ECB euro foreign exchange reference rates via Frankfurter; weekends forward-filled.",
    }
    (fx_dir / "fx_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    return table_from_parquet(fx_dir / "fx_daily.parquet")
