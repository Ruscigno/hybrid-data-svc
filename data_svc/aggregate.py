"""Aggregate 1-minute OHLCV bars into higher timeframes (ADR 0001, D2).

Pure function of its input: no clock, no DB, no network. Intraday timeframes
bucket by fixed UTC second-windows; daily-and-up bucket by the US/Eastern
trading-session calendar (added in a later task). The most-recent bucket may be
partial — the writer re-runs this each cycle and upserts idempotently.
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

_COLS = ["time", "open", "high", "low", "close", "volume"]

# Storage code -> bucket width in seconds (UTC-anchored).
INTRADAY_SECONDS: dict[str, int] = {
    "5": 300, "15": 900, "30": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800,
}

# Daily-and-up timeframes, bucketed on the America/New_York calendar.
CALENDAR_TFS: frozenset[str] = frozenset({"1D", "3D", "1W", "1M"})

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}

_NY = "America/New_York"


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Sort ascending by time and collapse duplicate minutes (last write wins)."""
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    out = df[_COLS].sort_values("time", kind="stable")
    out = out.drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    return out


def _bucket(df: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    """Group `df` by the integer bucket-open `key` and apply the OHLCV rule."""
    grouped = (
        df.assign(time=key.values)
        .groupby("time", sort=True, as_index=False)
        .agg(_AGG)
    )
    return grouped[_COLS].reset_index(drop=True)


def _et_midnight_epoch(d: _dt.date) -> int:
    """Epoch seconds of `d` at 00:00 America/New_York (DST-safe)."""
    return int(pd.Timestamp(d).tz_localize(_NY).timestamp())


def _calendar_bucket_open(times: pd.Series, timeframe: str) -> pd.Series:
    """Per-row bucket-open epoch (int seconds) for a calendar timeframe."""
    et_dates = (
        pd.to_datetime(times.astype("int64"), unit="s", utc=True)
        .dt.tz_convert(_NY)
        .dt.date
    )

    def start_date(d: _dt.date) -> _dt.date:
        if timeframe == "1D":
            return d
        if timeframe == "1W":
            return d - _dt.timedelta(days=d.weekday())      # back to Monday
        if timeframe == "1M":
            return d.replace(day=1)
        if timeframe == "3D":                               # fixed 3-day grid
            o = d.toordinal()
            return _dt.date.fromordinal(o - (o % 3))
        raise ValueError(f"unknown calendar timeframe {timeframe!r}")

    # Cache per distinct date so we localize each calendar date only once.
    opens = {d: _et_midnight_epoch(start_date(d)) for d in set(et_dates)}
    return et_dates.map(opens).astype("int64")


def aggregate(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 1-minute bars (columns _COLS, `time`=epoch seconds UTC) into
    `timeframe` (a storage code: "1","5",...,"8h","1D","3D","1W","1M")."""
    df = _normalize(df_1m)
    if timeframe == "1":
        return df
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    if timeframe in INTRADAY_SECONDS:
        width = INTRADAY_SECONDS[timeframe]
        key = (df["time"].astype("int64") // width) * width
        return _bucket(df, key)
    if timeframe in CALENDAR_TFS:
        key = _calendar_bucket_open(df["time"], timeframe)
        return _bucket(df, key)
    raise ValueError(f"unknown timeframe {timeframe!r}")
