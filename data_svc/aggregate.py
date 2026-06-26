"""Aggregate native OHLCV bars into derived timeframes (ADR 0002, D2').

Two-tier model: the source (Yahoo) provides **1m and 1d natively**. Intraday
targets (5m..8h) are derived from the **1-minute** series by fixed UTC
second-windows; daily-plus targets (3d/1w/1mo) are derived from the **native
daily** series by US/Eastern calendar grouping. `1m` and `1D` are native —
`aggregate()` passes them through unchanged.

Pure function of its input: no clock, no DB, no network. The most-recent bucket
may be partial — the writer re-runs this each cycle and upserts idempotently, so
the forming bar updates until it closes.
"""
from __future__ import annotations

import datetime as _dt

import pandas as pd

_COLS = ["time", "open", "high", "low", "close", "volume"]
_NY = "America/New_York"

# Native feeds — fetched from the source, never aggregated.
NATIVE_TFS: frozenset[str] = frozenset({"1", "1D"})

# Intraday targets -> bucket width (seconds); derived from the 1-min series (UTC).
INTRADAY_SECONDS: dict[str, int] = {
    "5": 300, "15": 900, "30": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800,
}

# Daily-plus targets; derived from the native daily (1D) series.
DAILY_DERIVED: frozenset[str] = frozenset({"3D", "1W", "1M"})

# Which base series each derived target resamples from ("1" or "1D").
BASE_TF: dict[str, str] = (
    {tf: "1" for tf in INTRADAY_SECONDS} | {tf: "1D" for tf in DAILY_DERIVED}
)

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def base_timeframe(timeframe: str) -> str | None:
    """The base series a derived `timeframe` resamples from (`"1"` or `"1D"`),
    or `None` for native timeframes (`"1"`, `"1D"`)."""
    return BASE_TF.get(timeframe)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Sort ascending by time and collapse duplicate timestamps (last wins)."""
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    out = df[_COLS].sort_values("time", kind="stable")
    return out.drop_duplicates(subset="time", keep="last").reset_index(drop=True)


def _bucket_intraday(df: pd.DataFrame, width: int) -> pd.DataFrame:
    """Fixed UTC second-window buckets; bucket-open time = floor(ts/width)*width."""
    key = (df["time"].astype("int64") // width) * width
    grouped = (
        df.assign(time=key.values)
        .groupby("time", sort=True, as_index=False)
        .agg(_AGG)
    )
    return grouped[_COLS].reset_index(drop=True)


def _bucket_daily(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Group native daily bars into 3D/1W/1M by US/Eastern calendar; the bucket's
    `time` is the earliest constituent daily bar's ts (a real session open)."""
    et_dates = (
        pd.to_datetime(df["time"].astype("int64"), unit="s", utc=True)
        .dt.tz_convert(_NY)
        .dt.date
    )

    def group_key(d: _dt.date):
        if timeframe == "1W":
            iso = d.isocalendar()
            return (iso[0], iso[1])            # ISO (year, week)
        if timeframe == "1M":
            return (d.year, d.month)
        if timeframe == "3D":                  # fixed grid: dates with ordinal%3==0 start
            o = d.toordinal()
            return o - (o % 3)
        raise ValueError(f"unknown daily-derived timeframe {timeframe!r}")

    keys = [group_key(d) for d in et_dates]
    grouped = (
        df.assign(_k=keys)
        .groupby("_k", sort=False, as_index=False)
        .agg(time=("time", "min"), open=("open", "first"), high=("high", "max"),
             low=("low", "min"), close=("close", "last"), volume=("volume", "sum"))
    )
    return grouped[_COLS].sort_values("time").reset_index(drop=True)


def aggregate(df_base: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate a base series into `timeframe`.

    The caller must pass the correct base series for `timeframe` (see
    `base_timeframe`): the 1-minute series for intraday targets, the native daily
    series for 3D/1W/1M. Native timeframes (`"1"`, `"1D"`) pass through normalized.
    """
    df = _normalize(df_base)
    if timeframe in NATIVE_TFS:
        return df
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    if timeframe in INTRADAY_SECONDS:
        return _bucket_intraday(df, INTRADAY_SECONDS[timeframe])
    if timeframe in DAILY_DERIVED:
        return _bucket_daily(df, timeframe)
    raise ValueError(f"unknown timeframe {timeframe!r}")
