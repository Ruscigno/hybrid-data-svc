"""
One-shot historical backfill from Binance public REST API into Postgres.

Why this exists: Pine Script `ta.ema(close, 200)` running on TradingView's
BINANCE:BTCUSDT chart sees years of history, so its EMA is fully converged.
The data-svc cache, populated incrementally via the `tv` CLI, only holds a
few weeks of bars — not enough for EMA(200) to converge from its init bias.

Binance's public klines endpoint is the upstream source for TradingView's
BINANCE:* charts, so backfilling from it produces bars that match Pine's
view to the cent. No auth needed, free, 1000 bars per request.

Usage:
    python -m data_svc.backfill --symbol "BTC/USDT:USDT" --timeframe 1h --bars 1500

Idempotent: re-runs are safe (INSERT ... ON CONFLICT DO UPDATE).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request

from .db.cache import BarCache, _is_plausible

logger = logging.getLogger(__name__)


_BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
_MAX_PER_CALL = 1000

_TF_TO_BINANCE: dict[str, str] = {
    "1": "1m", "3": "3m", "5": "5m", "15": "15m", "30": "30m",
    "60": "1h", "1h": "1h", "2h": "2h", "4h": "4h",
    "D": "1d", "1D": "1d", "W": "1w", "1W": "1w",
}

_TF_SECONDS: dict[str, int] = {
    "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "1h": 3600, "2h": 7200, "4h": 14400,
    "D": 86400, "1D": 86400, "W": 604800, "1W": 604800,
}


class BackfillError(Exception):
    pass


def _to_binance_pair(db_symbol: str) -> str:
    base_quote = db_symbol.split(":", 1)[0]
    return base_quote.replace("/", "").upper()


def _fetch_klines_page(symbol: str, interval: str, end_time_ms: int, limit: int) -> list[list]:
    url = (
        f"{_BINANCE_KLINES_URL}"
        f"?symbol={symbol}&interval={interval}&endTime={end_time_ms}&limit={limit}"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            payload = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise BackfillError(f"Binance fetch failed: {exc}") from exc
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"Binance returned non-JSON: {payload[:200]}") from exc


def fetch_history(binance_symbol: str, timeframe: str, total_bars: int) -> list[dict]:
    interval = _TF_TO_BINANCE.get(timeframe)
    if interval is None:
        raise BackfillError(f"Unsupported timeframe: {timeframe!r}")

    bars: list[dict] = []
    cursor_ms = int(time.time() * 1000)

    while len(bars) < total_bars:
        remaining = total_bars - len(bars)
        page_limit = min(_MAX_PER_CALL, remaining)
        klines = _fetch_klines_page(binance_symbol, interval, cursor_ms, page_limit)
        if not klines:
            break

        for kl in klines:
            bars.append({
                "ts": int(kl[0]) // 1000,
                "open": float(kl[1]),
                "high": float(kl[2]),
                "low":  float(kl[3]),
                "close": float(kl[4]),
                "volume": float(kl[5]),
            })

        oldest_ms = int(klines[0][0])
        cursor_ms = oldest_ms - 1

        if len(klines) < page_limit:
            break

    unique = {b["ts"]: b for b in bars}
    out = sorted(unique.values(), key=lambda b: b["ts"])
    return out[-total_bars:]


def write_bars(pg_url: str, db_symbol: str, timeframe: str, bars: list[dict]) -> int:
    """Insert bars into Postgres via BarCache (re-uses the same write path + guards)."""
    if not bars:
        return 0

    # Pre-filter by plausibility so the BarCache guard doesn't reject the entire batch
    # if one outlier slipped through (Binance is reliable, so this is defensive).
    plausible = [b for b in bars if _is_plausible(db_symbol, b["close"])]
    skipped = len(bars) - len(plausible)
    if skipped:
        logger.warning("backfill: dropped %d implausible bars for %s", skipped, db_symbol)

    cache = BarCache(pg_url)

    # Build a DataFrame-like list compatible with BarCache._insert_bars.
    import pandas as pd
    df = pd.DataFrame([
        {"time": b["ts"], "open": b["open"], "high": b["high"],
         "low": b["low"], "close": b["close"], "volume": b["volume"]}
        for b in plausible
    ])

    cache._insert_bars(df, db_symbol, timeframe)  # noqa: SLF001 — internal helper, intentional
    return cache.bar_count(db_symbol, timeframe)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> None:
    _setup_logging()

    parser = argparse.ArgumentParser(description="Backfill Postgres with Binance historical OHLCV.")
    parser.add_argument("--symbol", required=True, help="DB symbol (e.g. 'BTC/USDT:USDT')")
    parser.add_argument("--timeframe", required=True, help="Timeframe (e.g. '1h', '4h')")
    parser.add_argument("--bars", type=int, default=1500, help="Number of bars to backfill")
    parser.add_argument(
        "--pg-url",
        default=os.getenv("POSTGRES_URL", "postgresql://datasvc:datasvc@postgres:5432/datasvc"),
        help="Postgres connection URL",
    )
    args = parser.parse_args()

    binance_pair = _to_binance_pair(args.symbol)

    logger.info(
        "backfill — db_symbol=%s binance=%s tf=%s bars=%d",
        args.symbol, binance_pair, args.timeframe, args.bars,
    )

    bars = fetch_history(binance_pair, args.timeframe, args.bars)
    logger.info("fetched %d bars (oldest=%s, newest=%s)",
                len(bars),
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(bars[0]["ts"])) if bars else "n/a",
                time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(bars[-1]["ts"])) if bars else "n/a")

    total_after = write_bars(args.pg_url, args.symbol, args.timeframe, bars)
    logger.info("done — %s/%s now holds %d bars in Postgres", args.symbol, args.timeframe, total_after)


if __name__ == "__main__":
    main()
