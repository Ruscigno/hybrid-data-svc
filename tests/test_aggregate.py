"""Pure unit tests for the 1-minute -> N-timeframe aggregation engine."""
from __future__ import annotations

import pandas as pd
import pytest

from data_svc.aggregate import aggregate

_COLS = ["time", "open", "high", "low", "close", "volume"]


def _df(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


def test_five_minute_bucket_ohlcv():
    # Five 1-min bars all inside the 5-min bucket starting at t0 (t0 % 300 == 0).
    t0 = 1_700_000_100
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 60,  11, 15, 11, 11, 2),
        (t0 + 120, 12, 12,  9, 12, 3),
        (t0 + 180, 13, 13, 13, 13, 4),
        (t0 + 240, 14, 14, 14, 14, 5),
    ])
    out = aggregate(df, "5")
    assert list(out.columns) == _COLS
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == t0
    assert row["open"] == 10 and row["close"] == 14
    assert row["high"] == 15 and row["low"] == 9
    assert row["volume"] == 15


def test_intraday_splits_into_two_buckets():
    t0 = 1_700_000_100  # 5-min aligned
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 240, 14, 14, 14, 14, 1),
        (t0 + 300, 20, 25, 20, 22, 1),  # next 5-min bucket
    ])
    out = aggregate(df, "5")
    assert list(out["time"]) == [t0, t0 + 300]
    assert list(out["close"]) == [14, 22]


def test_one_minute_passthrough_sorts_and_dedups():
    df = _df([
        (200, 2, 2, 2, 2, 1),
        (100, 1, 1, 1, 1, 1),
        (200, 9, 9, 9, 9, 9),  # duplicate minute -> keep last
    ])
    out = aggregate(df, "1")
    assert list(out["time"]) == [100, 200]
    assert int(out.iloc[1]["close"]) == 9  # last write wins for the dup


def test_empty_input_returns_empty_with_columns():
    out = aggregate(_df([]), "1h")
    assert list(out.columns) == _COLS
    assert len(out) == 0


def test_unknown_timeframe_raises():
    with pytest.raises(ValueError):
        aggregate(_df([(0, 1, 1, 1, 1, 1)]), "7m")


# America/New_York is UTC-5 in January (EST). Reference epochs:
#   2024-01-03 00:00 ET = 1704258000   (ET midnight, that trading date)
#   2024-01-01 00:00 ET = 1704085200   (the ISO-week Monday, and Jan month start)
#   2024-02-01 00:00 ET = 1706763600   (Feb month start)
# Session bars on 2024-01-03 (09:30/15:00/15:59 ET = 14:30/20:00/20:59 UTC):
_D0930 = 1704292200
_D1500 = 1704312000
_D1559 = 1704315540


def test_daily_bar_is_one_eastern_session():
    df = _df([
        (_D0930, 100, 101,  99, 100, 5),
        (_D1500, 100, 110,  95, 105, 7),
        (_D1559, 105, 106, 104, 102, 3),
    ])
    out = aggregate(df, "1D")
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == 1704258000  # 2024-01-03 00:00 ET
    assert row["open"] == 100 and row["close"] == 102
    assert row["high"] == 110 and row["low"] == 95
    assert row["volume"] == 15


def test_weekly_bar_anchors_to_eastern_monday():
    df = _df([(_D0930, 1, 1, 1, 1, 1), (_D1559, 2, 2, 2, 2, 1)])
    out = aggregate(df, "1W")
    assert len(out) == 1
    assert int(out.iloc[0]["time"]) == 1704085200  # Mon 2024-01-01 00:00 ET
    assert out.iloc[0]["close"] == 2


def test_monthly_bars_split_by_eastern_month():
    feb = 1706800000  # 2024-02-01 13:46 UTC, an ET-February instant
    df = _df([(_D0930, 1, 1, 1, 1, 1), (feb, 2, 2, 2, 2, 1)])
    out = aggregate(df, "1M")
    assert list(out["time"]) == [1704085200, 1706763600]  # Jan, Feb month-starts ET


def test_three_day_bucket_fixed_grid():
    # 2024-01-03 (ordinal 738888, %3==0) starts a 3-day bucket {01-03,01-04,01-05}.
    # 2024-01-06 (ordinal 738891, %3==0) starts the next bucket.
    d_0103 = _D0930              # 2024-01-03 14:30 UTC
    d_0104 = _D0930 + 86400      # 2024-01-04 14:30 UTC (same bucket)
    d_0106 = _D0930 + 3 * 86400  # 2024-01-06 14:30 UTC (next bucket)
    df = _df([
        (d_0103, 10, 10, 10, 10, 1),
        (d_0104, 20, 30, 20, 25, 1),
        (d_0106, 40, 40, 40, 40, 1),
    ])
    out = aggregate(df, "3D")
    assert list(out["time"]) == [1704258000, 1704517200]  # 01-03 ET, 01-06 ET
    first = out.iloc[0]
    assert first["open"] == 10 and first["close"] == 25 and first["high"] == 30
    assert int(out.iloc[1]["open"]) == 40
