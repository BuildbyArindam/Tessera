"""Small deterministic calendar helpers (no holidays; Mon-Fri business days)."""

from __future__ import annotations

import calendar
from datetime import date, timedelta


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def roll_forward(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def roll_back(d: date) -> date:
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def add_months(d: date, n: int) -> date:
    idx = d.year * 12 + (d.month - 1) + n
    year, month0 = divmod(idx, 12)
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(d.day, last))


def month_end(d: date) -> date:
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])


def month_starts(start: date, n_months: int) -> list[date]:
    first = start.replace(day=1)
    return [add_months(first, i) for i in range(n_months)]


def window_end(start: date, n_months: int) -> date:
    """Last calendar day of the window that begins in start's month."""
    return month_end(add_months(start.replace(day=1), n_months - 1))


def clamp_day(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def business_days(lo: date, hi: date) -> list[date]:
    out: list[date] = []
    d = lo
    while d <= hi:
        if is_business_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out
