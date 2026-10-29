"""Weekly net cash flow: the forecasting target.

net = inflows - outflows, bucketed by the week in which the cash actually
moved (``cash_date``), per business per currency. Weeks run Monday to Sunday.
Weeks with no movement are zero-filled between a series' first flow and the
last complete week, so lags are positional and honest.
"""

from __future__ import annotations

import pandas as pd

_ONE_WEEK = pd.Timedelta(days=7)


def week_start(ts: pd.Timestamp) -> pd.Timestamp:
    """Monday of the week containing ``ts``."""
    day = pd.Timestamp(ts).normalize()
    return day - pd.Timedelta(days=day.weekday())


def last_complete_week_start(as_of: pd.Timestamp) -> pd.Timestamp:
    """Start of the latest Monday-Sunday week fully elapsed at end of ``as_of``."""
    day = pd.Timestamp(as_of).normalize()
    start = week_start(day)
    return start if day.weekday() == 6 else start - _ONE_WEEK


def weekly_net_cashflow(cashflows: pd.DataFrame, as_of: pd.Timestamp | None = None) -> pd.DataFrame:
    """Long frame: business_id, currency, week_start, net.

    With ``as_of`` set, rows after that day are dropped and the incomplete
    current week is excluded (a partial week would bias the last lag low).
    Without it, every series ends at the same last week present in the data.
    """
    cols = ["business_id", "currency", "week_start", "net"]
    df = cashflows
    last_start: pd.Timestamp | None = None
    if as_of is not None:
        cutoff = pd.Timestamp(as_of).normalize()
        df = df[df["cash_date"] < cutoff + pd.Timedelta(days=1)]
        last_start = last_complete_week_start(cutoff)
    if df.empty:
        return pd.DataFrame(columns=cols)

    starts = df["cash_date"].dt.normalize() - pd.to_timedelta(df["cash_date"].dt.weekday, unit="D")
    df = df.assign(week_start=starts)
    if last_start is not None:
        df = df[df["week_start"] <= last_start]
    end_all = df["week_start"].max() if last_start is None else last_start

    frames: list[pd.DataFrame] = []
    for key, grp in df.groupby(["business_id", "currency"], sort=True):
        business_id, currency = key  # type: ignore[misc]
        weekly = grp.groupby("week_start")["amount"].sum()
        idx = pd.date_range(weekly.index.min(), end_all, freq="7D")
        weekly = weekly.reindex(idx, fill_value=0.0)
        frames.append(
            pd.DataFrame(
                {
                    "business_id": business_id,
                    "currency": currency,
                    "week_start": idx,
                    "net": weekly.to_numpy(dtype=float),
                }
            )
        )
    if not frames:
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)[cols]


def series_by_key(weekly: pd.DataFrame) -> dict[tuple[str, str], pd.Series]:
    """Split the long frame into one Series (index=week_start) per key."""
    out: dict[tuple[str, str], pd.Series] = {}
    for key, grp in weekly.groupby(["business_id", "currency"], sort=True):
        business_id, currency = key  # type: ignore[misc]
        out[(str(business_id), str(currency))] = pd.Series(
            grp["net"].to_numpy(dtype=float), index=pd.DatetimeIndex(grp["week_start"]), name="net"
        )
    return out
