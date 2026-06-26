"""Detect OHLCV adjustment discontinuities (ADR 0002, D11). Pure pandas, no I/O.

Ported from Wagner's `market-data-service` and adapted to this repo's bar shape
(lowercase columns; `time` = epoch seconds UTC — so no timezone juggling is
needed when comparing series).

Why it exists: a provider can return *un-adjusted* prices for older bars and
*adjusted* prices for newer ones (e.g. yfinance flipped its `auto_adjust`
default in Dec 2024). When a corporate action (split, large distribution) sits
on that boundary, the stored daily series shows a spurious one-day jump.

Repair strategy (driven by the writer, D11): scan the stored daily series for
jumps; for each, re-fetch a small window from the provider and compare. If the
refetch does NOT show the jump, the stored bars are stale → overwrite the window
via `INSERT … ON CONFLICT DO UPDATE`. If the refetch *also* shows it, the move
is real (earnings shock, etc.) — skip.
"""

from __future__ import annotations

import pandas as pd

# Day-over-day relative move large enough to suspect a corporate action rather
# than ordinary volatility. 20% catches typical splits (2:1 ⇒ −50%) and large
# ETF distributions (~10–30%) without flagging real volatility (earnings rarely
# exceed ~15%).
DEFAULT_THRESHOLD_PCT = 20.0

# Comparing stored bars vs a fresh refetch: a single bar differing by more than
# this fraction (1%) is taken as evidence the stored bar is stale.
DEFAULT_REL_ERR = 0.01


def find_jumps(df: pd.DataFrame, threshold_pct: float = DEFAULT_THRESHOLD_PCT) -> list[int]:
    """Return the `time`s (epoch s) where ``|close.pct_change()| × 100 > threshold_pct``.

    Each returned time is the *later* bar of the jump (the bar on which the jump
    is observed). Empty list if the series has fewer than 2 rows or no ``close``.
    """
    if df is None or df.empty or "close" not in df.columns or len(df) < 2:
        return []
    d = df.sort_values("time", kind="stable").drop_duplicates(subset="time", keep="last")
    pct = d["close"].astype(float).pct_change().abs() * 100.0
    return [int(t) for t in d.loc[pct > threshold_pct, "time"]]


def is_stale_vs_refetch(
    stored: pd.DataFrame,
    refetched: pd.DataFrame,
    rel_err: float = DEFAULT_REL_ERR,
) -> bool:
    """True iff the stored series differs from the refetch beyond tolerance.

    Compares ``close`` on overlapping `time`s only (both epoch-UTC, so the join
    is exact). "Stale" = at least one common `time` whose stored close differs
    from the refetched close by more than ``rel_err`` (default 1%).
    """
    if (
        stored is None or refetched is None or stored.empty or refetched.empty
        or "close" not in stored.columns or "close" not in refetched.columns
    ):
        return False

    s = (
        stored.drop_duplicates(subset="time", keep="last")
        .set_index("time")["close"].astype(float)
    )
    r = (
        refetched.drop_duplicates(subset="time", keep="last")
        .set_index("time")["close"].astype(float)
    )
    common = s.index.intersection(r.index)
    if len(common) == 0:
        return False
    rel = (s.loc[common] - r.loc[common]).abs() / r.loc[common].abs().clip(lower=1e-9)
    return bool((rel > rel_err).any())
