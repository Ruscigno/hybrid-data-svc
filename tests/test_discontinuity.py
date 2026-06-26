"""Pure unit tests for corporate-action discontinuity detection (ADR 0002, D11)."""
from __future__ import annotations

import pandas as pd

from data_svc.discontinuity import find_jumps, is_stale_vs_refetch

_COLS = ["time", "open", "high", "low", "close", "volume"]


def _df(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


def _daily(closes: list[float], start: int = 1_700_000_000, step: int = 86_400) -> pd.DataFrame:
    rows = [(start + i * step, c, c, c, c, 1) for i, c in enumerate(closes)]
    return _df(rows)


# ---- find_jumps ----

def test_find_jumps_detects_split() -> None:
    # 100 -> 100 -> 50 is a -50% move (a 2:1 split artifact) on the 3rd bar.
    df = _daily([100.0, 100.0, 50.0, 50.0])
    jumps = find_jumps(df)
    assert len(jumps) == 1
    assert jumps[0] == int(df.iloc[2]["time"])   # the later bar of the jump


def test_find_jumps_ignores_normal_volatility() -> None:
    df = _daily([100.0, 105.0, 102.0, 108.0])    # all < 20% moves
    assert find_jumps(df) == []


def test_find_jumps_custom_threshold() -> None:
    df = _daily([100.0, 112.0])                  # +12% move
    assert find_jumps(df, threshold_pct=20.0) == []
    assert find_jumps(df, threshold_pct=10.0) == [int(df.iloc[1]["time"])]


def test_find_jumps_short_or_missing() -> None:
    assert find_jumps(_daily([100.0])) == []                 # < 2 rows
    assert find_jumps(_df([])) == []                         # empty
    assert find_jumps(pd.DataFrame({"time": [1, 2]})) == []  # no 'close'


def test_find_jumps_sorts_unsorted_input() -> None:
    df = _daily([100.0, 50.0])
    df = df.iloc[::-1].reset_index(drop=True)     # reversed
    assert find_jumps(df) == [int(df["time"].max())]


# ---- is_stale_vs_refetch ----

def test_stale_when_close_differs_beyond_tolerance() -> None:
    stored = _daily([100.0, 200.0, 300.0])            # un-adjusted middle bar
    refetched = _daily([100.0, 150.0, 300.0])         # adjusted: 200 vs 150 = 33% off
    assert is_stale_vs_refetch(stored, refetched) is True


def test_not_stale_when_within_tolerance() -> None:
    stored = _daily([100.0, 150.0, 300.0])
    refetched = _daily([100.0, 150.5, 300.0])         # 0.33% off, under 1%
    assert is_stale_vs_refetch(stored, refetched) is False


def test_not_stale_with_no_overlap() -> None:
    stored = _daily([100.0, 110.0], start=1_700_000_000)
    refetched = _daily([100.0, 110.0], start=1_800_000_000)   # disjoint times
    assert is_stale_vs_refetch(stored, refetched) is False


def test_not_stale_on_empty_or_missing() -> None:
    good = _daily([100.0, 110.0])
    assert is_stale_vs_refetch(_df([]), good) is False
    assert is_stale_vs_refetch(good, _df([])) is False
    assert is_stale_vs_refetch(pd.DataFrame({"time": [1]}), good) is False
