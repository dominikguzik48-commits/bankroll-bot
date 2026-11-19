"""NYSE trading calendar (pandas_market_calendars): holidays, early closes, settlement days."""
from __future__ import annotations

import datetime as dt
from functools import lru_cache

import numpy as np
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

try:
    import pandas_market_calendars as _pmc
except ImportError:  # the simulator works without it
    _pmc = None


@lru_cache(maxsize=1)
def _nyse():
    return _pmc.get_calendar("NYSE") if _pmc else None


@lru_cache(maxsize=1)
def holidays() -> tuple[np.datetime64, ...]:
    """Full-day NYSE holidays (weekdays with no session), 2020-2035."""
    cal = _nyse()
    if cal is None:
        return ()
    valid = set(cal.valid_days("2020-01-01", "2035-12-31").date)
    days = np.arange(np.datetime64("2020-01-01"), np.datetime64("2036-01-01"))
    return tuple(d for d in days if np.is_busday(d) and d.astype(dt.date) not in valid)


def add_trading_days(d: dt.date, n: int) -> dt.date:
    return np.busday_offset(np.datetime64(d, "D"), n, roll="forward",
                            holidays=list(holidays())).astype(dt.date)


def session(d: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
    """(open, close) as timezone-aware UTC datetimes, or None if the market is closed."""
    cal = _nyse()
    if cal is None:
        raise RuntimeError("pandas_market_calendars is not installed")
    s = cal.schedule(d.isoformat(), d.isoformat())
    if s.empty:
        return None
    row = s.iloc[0]
    return row["market_open"].to_pydatetime(), row["market_close"].to_pydatetime()


def run_window(now_utc: dt.datetime, before_close_min: tuple[int, int] = (40, 5)):
    """Is `now` inside the daily run window (40 to 5 minutes before today's close)?
    Returns (ok, message). Early-close days (1 PM ET) are handled automatically."""
    today = now_utc.astimezone(ET).date()
    s = session(today)
    if s is None:
        return False, f"Market closed on {today} (holiday or weekend); nothing done."
    close = s[1]
    lo = close - dt.timedelta(minutes=before_close_min[0])
    hi = close - dt.timedelta(minutes=before_close_min[1])
    if lo <= now_utc <= hi:
        return True, ""
    return False, (f"Outside the run window: today's close is {close.astimezone(ET):%H:%M} ET; the bot "
                   f"runs between {before_close_min[0]} and {before_close_min[1]} minutes before it.")
