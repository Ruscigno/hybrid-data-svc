"""Yahoo Finance v8 chart JSON -> DataFrame parser.

Pure function — no I/O, no side effects.
"""

from __future__ import annotations

import pandas as pd

_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _empty() -> pd.DataFrame:
    return pd.DataFrame(columns=_COLUMNS)


def parse_chart(payload: dict | None) -> pd.DataFrame:
    """Yahoo v8 chart JSON -> DataFrame[time,open,high,low,close,volume].

    ``time`` is epoch seconds UTC, rows sorted ascending. Drops any row where
    any of open/high/low/close is null. Returns an empty DataFrame with those
    columns for an empty/None/malformed payload — never raises on 'no data'.
    """
    if not payload:
        return _empty()

    chart = payload.get("chart", {})
    if not chart:
        return _empty()

    # Non-null error field means the API reported an error
    if chart.get("error"):
        return _empty()

    result = chart.get("result")
    if not result:
        return _empty()

    try:
        first = result[0]
        timestamps = first["timestamp"]
        quote = first["indicators"]["quote"][0]
        opens = quote["open"]
        highs = quote["high"]
        lows = quote["low"]
        closes = quote["close"]
        volumes = quote["volume"]
    except (KeyError, IndexError, TypeError):
        return _empty()

    # Guard against ragged arrays — all lists must have equal length.
    # Also catches the ValueError pandas raises for mismatched-length arrays.
    lengths = {len(arr) for arr in (timestamps, opens, highs, lows, closes, volumes)}
    if len(lengths) != 1:
        return _empty()

    try:
        df = pd.DataFrame(
            {
                "time": timestamps,
                "open": opens,
                "high": highs,
                "low": lows,
                "close": closes,
                "volume": volumes,
            }
        )
    except ValueError:
        return _empty()

    # Drop rows where any price column is null
    df = df.dropna(subset=["open", "high", "low", "close"])

    # Sort ascending by time
    df = df.sort_values("time").reset_index(drop=True)

    # Ensure correct column order
    return df[_COLUMNS]
