"""Pure unit tests for the two-tier aggregation engine (ADR 0002, D2')."""
from __future__ import annotations

import pandas as pd
import pytest

from data_svc.aggregate import aggregate, base_timeframe

_COLS = ["time", "open", "high", "low", "close", "volume"]


def _df(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


# ---- base_timeframe routing (which native series each target derives from) ----

def test_base_timeframe_routing() -> None:
    assert base_timeframe("5") == "1"
    assert base_timeframe("4h") == "1"
    assert base_timeframe("8h") == "1"
    assert base_timeframe("3D") == "1D"
    assert base_timeframe("1W") == "1D"
    assert base_timeframe("1M") == "1D"
    assert base_timeframe("1") is None      # native
    assert base_timeframe("1D") is None     # native


# ---- native passthrough (1m, 1D are fetched, not aggregated) ----

def test_one_minute_passthrough_sorts_and_dedups() -> None:
    df = _df([
        (200, 2, 2, 2, 2, 1),
        (100, 1, 1, 1, 1, 1),
        (200, 9, 9, 9, 9, 9),   # duplicate ts -> keep last
    ])
    out = aggregate(df, "1")
    assert list(out["time"]) == [100, 200]
    assert int(out.iloc[1]["close"]) == 9


def test_daily_is_native_passthrough() -> None:
    df = _df([(1704810600, 2, 2, 2, 2, 1), (1704724200, 1, 1, 1, 1, 1)])
    out = aggregate(df, "1D")
    assert list(out["time"]) == [1704724200, 1704810600]
    assert int(out.iloc[0]["open"]) == 1


# ---- intraday: derived from the 1-min series, fixed UTC buckets ----

def test_five_minute_bucket_ohlcv() -> None:
    t0 = 1_700_000_100  # 5-min aligned
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 60,  11, 15, 11, 11, 2),
        (t0 + 120, 12, 12,  9, 12, 3),
        (t0 + 180, 13, 13, 13, 13, 4),
        (t0 + 240, 14, 14, 14, 14, 5),
    ])
    out = aggregate(df, "5")
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == t0
    assert row["open"] == 10 and row["close"] == 14
    assert row["high"] == 15 and row["low"] == 9 and row["volume"] == 15


def test_intraday_splits_into_two_buckets() -> None:
    t0 = 1_700_000_100
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 240, 14, 14, 14, 14, 1),
        (t0 + 300, 20, 25, 20, 22, 1),
    ])
    out = aggregate(df, "5")
    assert list(out["time"]) == [t0, t0 + 300]
    assert list(out["close"]) == [14, 22]


# ---- daily-plus: derived from native daily bars, US/Eastern calendar ----
# Daily bars at 09:30 ET (= 14:30 UTC, EST) across the 2024-01-08..12 trading week.
_MON = 1704724200  # 2024-01-08
_TUE = 1704810600  # 2024-01-09
_WED = 1704897000  # 2024-01-10
_THU = 1704983400  # 2024-01-11
_FRI = 1705069800  # 2024-01-12
_FEB = 1706797800  # 2024-02-01


def test_weekly_from_daily_groups_iso_week() -> None:
    df = _df([
        (_MON, 100, 101,  99, 100, 5),
        (_TUE, 100, 110,  95, 105, 7),
        (_WED, 105, 106, 100, 101, 3),
        (_THU, 101, 102,  98, 100, 2),
        (_FRI, 100, 104,  97, 103, 4),
    ])
    out = aggregate(df, "1W")
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == _MON          # earliest session in the week
    assert row["open"] == 100 and row["close"] == 103
    assert row["high"] == 110 and row["low"] == 95 and row["volume"] == 21


def test_monthly_from_daily_splits_by_month() -> None:
    df = _df([(_MON, 1, 1, 1, 1, 1), (_FEB, 2, 2, 2, 2, 1)])
    out = aggregate(df, "1M")
    assert list(out["time"]) == [_MON, _FEB]


def test_three_day_from_daily_fixed_grid() -> None:
    # 2024-01-09/10/11 share the 3-day grid bucket (ordinal 738894); 01-12 starts a new one.
    df = _df([
        (_TUE, 10, 10, 10, 10, 1),
        (_WED, 12, 20, 12, 18, 1),
        (_THU, 18, 19, 15, 15, 1),
        (_FRI, 40, 40, 40, 40, 1),
    ])
    out = aggregate(df, "3D")
    assert list(out["time"]) == [_TUE, _FRI]
    first = out.iloc[0]
    assert first["open"] == 10 and first["close"] == 15 and first["high"] == 20
    assert int(out.iloc[1]["open"]) == 40


# ---- edges ----

def test_empty_input_returns_empty_with_columns() -> None:
    out = aggregate(_df([]), "1h")
    assert list(out.columns) == _COLS
    assert len(out) == 0


def test_unknown_timeframe_raises() -> None:
    with pytest.raises(ValueError):
        aggregate(_df([(0, 1, 1, 1, 1, 1)]), "7m")
