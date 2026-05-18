"""
Postgres bar cache shared across all consumers.

Validation flow (called on every fetch):
  1. No cache row → full fetch from TV, store, return.
  2. Cache row exists:
     a. Fetch (gap_bars + 1) fresh bars from TV — the +1 is the overlap bar.
     b. Find the overlap bar: the most recent cached bar (ts == last_bar_ts).
     c. Compare overlap bar close price (tolerance 0.001%).
        - Match  → insert only new bars, update meta.
        - Mismatch → invalidate all rows for (symbol, timeframe), full refetch.
     d. Return latest `count` rows from DB.

Cross-symbol leak guards (per .ports the SQLite version):
  - PLAUSIBLE_RANGES: absolute price band per symbol.
  - INSERT_DRIFT_REJECT: rejects new bars whose close diverges >50% from the
    most recent cached close for that symbol.
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

import pandas as pd
import psycopg

from .postgres import get_pool

logger = logging.getLogger(__name__)

OVERLAP_TOLERANCE = 0.00001  # 0.001 %

INSERT_DRIFT_REJECT = 0.5

PLAUSIBLE_RANGES: dict[str, tuple[float, float]] = {
    "BTC/USDT:USDT": (1_000.0,    1_000_000.0),
    "ETH/USDT:USDT": (   100.0,      50_000.0),
    "BNB/USDT:USDT": (    20.0,      10_000.0),
    "SOL/USDT:USDT": (     5.0,      10_000.0),
    "XRP/USDT:USDT": (     0.05,        100.0),
}


def _is_plausible(symbol: str, close: float) -> bool:
    band = PLAUSIBLE_RANGES.get(symbol)
    if band is None:
        return True
    lo, hi = band
    return lo <= close <= hi


_TF_SECONDS: dict[str, int] = {
    "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "1h": 3600, "2h": 7200, "4h": 14400,
    "D": 86400, "1D": 86400, "W": 604800, "1W": 604800,
}

# Binance weekly bars start at Monday 00:00 UTC.
# Unix epoch (1970-01-01) is a Thursday → Monday is 4 days = 345600 s away.
# All other timeframes align cleanly to the unix epoch (ts % secs == 0).
_TF_EPOCH_OFFSET: dict[str, int] = {"W": 345600, "1W": 345600}


def _tf_seconds(timeframe: str) -> int:
    s = _TF_SECONDS.get(timeframe)
    if s is None:
        raise ValueError(f"Unknown timeframe: {timeframe!r}")
    return s


def _ts_aligned(ts: int, timeframe: str) -> bool:
    """True iff `ts` falls on the natural grid for `timeframe`.

    Binance's exchange convention drives the alignment:
      • Intraday timeframes (1–4h) align to the unix epoch (ts % secs == 0).
      • Daily bars open at 00:00 UTC (same rule).
      • Weekly bars open Monday 00:00 UTC, which is offset 4 days from the
        Thursday-epoch — hence the dedicated offset entry.
    """
    secs = _tf_seconds(timeframe)
    offset = _TF_EPOCH_OFFSET.get(timeframe, 0)
    return (ts - offset) % secs == 0


class BarCache:
    """Postgres-backed bar cache. Public API mirrors the legacy SQLite version."""

    def __init__(self, postgres_url: str) -> None:
        self._url = postgres_url
        self._pool = get_pool(postgres_url)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        count: int,
        tv_fetcher: Callable[[str, str, int], pd.DataFrame],
    ) -> pd.DataFrame:
        meta = self._get_meta(symbol, timeframe)

        if meta is None:
            logger.info("[cache] cold start %s/%s — full fetch", symbol, timeframe)
            df = tv_fetcher(symbol, timeframe, count)
            self._insert_bars(df, symbol, timeframe)
            return self._read_bars(symbol, timeframe, count)

        bar_secs = _tf_seconds(timeframe)
        now_ts = int(time.time())
        gap_bars = max(1, (now_ts - meta["last_bar_ts"]) // bar_secs)

        fetch_count = min(gap_bars + 1, count)
        logger.info(
            "[cache] validating %s/%s — gap=%d bars, fetching %d from TV",
            symbol, timeframe, gap_bars, fetch_count,
        )
        fresh = tv_fetcher(symbol, timeframe, fetch_count)

        if not self._validate_overlap(symbol, timeframe, meta["last_bar_ts"], fresh):
            logger.warning(
                "[cache] overlap mismatch for %s/%s — invalidating and full refetch",
                symbol, timeframe,
            )
            self._invalidate(symbol, timeframe)
            full = tv_fetcher(symbol, timeframe, count)
            self._insert_bars(full, symbol, timeframe)
            return self._read_bars(symbol, timeframe, count)

        new_bars = fresh[fresh["time"] > meta["last_bar_ts"]]
        if not new_bars.empty:
            logger.info("[cache] appending %d new bar(s) for %s/%s", len(new_bars), symbol, timeframe)
            self._insert_bars(new_bars, symbol, timeframe)
        else:
            logger.debug("[cache] no new bars for %s/%s", symbol, timeframe)

        return self._read_bars(symbol, timeframe, count)

    def invalidate(self, symbol: str, timeframe: str) -> None:
        self._invalidate(symbol, timeframe)

    def read_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        return self._read_bars(symbol, timeframe, count)

    def latest_bar_ts(self, symbol: str, timeframe: str) -> Optional[int]:
        meta = self._get_meta(symbol, timeframe)
        return meta["last_bar_ts"] if meta else None

    def bar_count(self, symbol: str, timeframe: str) -> int:
        meta = self._get_meta(symbol, timeframe)
        return int(meta["bar_count"]) if meta else 0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_meta(self, symbol: str, timeframe: str) -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT last_bar_ts, bar_count, last_fetched_at "
                "FROM cache_meta WHERE symbol=%s AND timeframe=%s",
                (symbol, timeframe),
            ).fetchone()
        if row is None:
            return None
        return {
            "last_bar_ts": int(row[0]),
            "bar_count": int(row[1]),
            "last_fetched_at": int(row[2]),
        }

    def _validate_overlap(
        self,
        symbol: str,
        timeframe: str,
        last_bar_ts: int,
        fresh: pd.DataFrame,
    ) -> bool:
        overlap_fresh = fresh[fresh["time"] == last_bar_ts]
        if overlap_fresh.empty:
            logger.debug("[cache] no overlap bar in fresh fetch — assuming valid")
            return True

        fresh_close = float(overlap_fresh.iloc[0]["close"])

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT close FROM bars WHERE symbol=%s AND timeframe=%s AND ts=%s",
                (symbol, timeframe, int(last_bar_ts)),
            ).fetchone()

        if row is None:
            return True

        cached_close = float(row[0])
        if cached_close == 0:
            return True

        diff = abs(fresh_close - cached_close) / abs(cached_close)
        if diff > OVERLAP_TOLERANCE:
            logger.warning(
                "[cache] close mismatch at ts=%d: cached=%.4f fresh=%.4f diff=%.6f%%",
                last_bar_ts, cached_close, fresh_close, diff * 100,
            )
            return False

        return True

    def _invalidate(self, symbol: str, timeframe: str) -> None:
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute(
                    "DELETE FROM bars WHERE symbol=%s AND timeframe=%s",
                    (symbol, timeframe),
                )
                conn.execute(
                    "DELETE FROM cache_meta WHERE symbol=%s AND timeframe=%s",
                    (symbol, timeframe),
                )
        logger.info("[cache] invalidated %s/%s", symbol, timeframe)

    def _last_close(self, symbol: str, timeframe: str) -> Optional[float]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT close FROM bars WHERE symbol=%s AND timeframe=%s "
                "ORDER BY ts DESC LIMIT 1",
                (symbol, timeframe),
            ).fetchone()
        return float(row[0]) if row else None

    def _insert_bars(self, df: pd.DataFrame, symbol: str, timeframe: str) -> None:
        bar_secs = _tf_seconds(timeframe)
        now = int(time.time())
        closed_df = df[df["time"].astype(int) + bar_secs <= now]
        if closed_df.empty:
            return

        # Cross-symbol leak guard, layer 1 — absolute plausibility.
        bad = [float(c) for c in closed_df["close"] if not _is_plausible(symbol, float(c))]
        if bad:
            sample = bad[:3]
            logger.error(
                "[cache] cross-symbol leak suspected for %s/%s: "
                "%d/%d incoming closes outside plausible band (sample=%s) — REJECTING insert",
                symbol, timeframe, len(bad), len(closed_df), sample,
            )
            return

        # Cross-symbol leak guard, layer 2 — relative drift vs last cached.
        last_close = self._last_close(symbol, timeframe)
        if last_close is not None and last_close > 0:
            fresh_close = float(closed_df.iloc[-1]["close"])
            drift = abs(fresh_close - last_close) / last_close
            if drift > INSERT_DRIFT_REJECT:
                logger.error(
                    "[cache] cross-symbol leak suspected for %s/%s: "
                    "last_cached_close=%.6f fresh_close=%.6f drift=%.1f%% — REJECTING insert",
                    symbol, timeframe, last_close, fresh_close, drift * 100,
                )
                return

        # Cross-symbol leak guard, layer 3 — timeframe-grid alignment.
        # Sub-grid timestamps (e.g. /30 bars at :30 leaking into a /1h cache)
        # are silent on the price-band + drift checks but corrupt indicator
        # calculations on read. Reject any incoming bar whose ts doesn't fall
        # on the natural grid for this timeframe.
        misaligned = [
            int(t) for t in closed_df["time"].astype(int) if not _ts_aligned(int(t), timeframe)
        ]
        if misaligned:
            sample = misaligned[:3]
            logger.error(
                "[cache] alignment violation for %s/%s: %d/%d incoming bars off-grid "
                "(sample_ts=%s) — REJECTING insert",
                symbol, timeframe, len(misaligned), len(closed_df), sample,
            )
            return

        rows = [
            (
                symbol, timeframe,
                int(row["time"]),
                float(row["open"]), float(row["high"]),
                float(row["low"]),  float(row["close"]),
                float(row["volume"]),
                now,
            )
            for _, row in closed_df.iterrows()
        ]

        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO bars
                             (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (symbol, timeframe, ts) DO UPDATE SET
                             open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                             close=EXCLUDED.close, volume=EXCLUDED.volume,
                             fetched_at=EXCLUDED.fetched_at""",
                        rows,
                    )
                    last_ts = int(closed_df["time"].max())
                    cur.execute(
                        """INSERT INTO cache_meta
                             (symbol, timeframe, last_bar_ts, bar_count, last_fetched_at)
                           VALUES (
                             %s, %s,
                             %s,
                             (SELECT COUNT(*) FROM bars WHERE symbol=%s AND timeframe=%s),
                             %s
                           )
                           ON CONFLICT (symbol, timeframe) DO UPDATE SET
                             last_bar_ts=EXCLUDED.last_bar_ts,
                             bar_count=EXCLUDED.bar_count,
                             last_fetched_at=EXCLUDED.last_fetched_at""",
                        (symbol, timeframe, last_ts, symbol, timeframe, now),
                    )

    def _read_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ts, open, high, low, close, volume
                         FROM bars
                        WHERE symbol=%s AND timeframe=%s
                        ORDER BY ts DESC
                        LIMIT %s""",
                    (symbol, timeframe, int(count)),
                )
                rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        return df.iloc[::-1].reset_index(drop=True)  # oldest first
