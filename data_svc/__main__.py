"""
Data service — multi-feed rotating scraper with schedule-aware refresh.

For each configured feed:
  1. Checks whether a new bar can have closed (fast, DB-only check).
  2. If yes  → switches chart (if needed) and fetches from TradingView.
  3. If no   → skips the TV call entirely; cache is mathematically fresh.

After every cycle the service sleeps until the soonest upcoming bar close
across all feeds (plus a small grace period). Chart switches are serialized:
the loop is single-threaded and processes one feed at a time.

Usage: python -m data_svc
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import DataSvcConfig, Feed
from .fetcher import DataFetchError, DataFetcher, bar_secs
from .tab_pin import TabPinError


_MIN_SLEEP_S = 5.0
_BAR_CLOSE_GRACE_S = 5.0


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger(__name__)


def _sleep_until_next_event(feeds: list[Feed], fetcher: DataFetcher, fallback_s: float) -> None:
    now = time.time()
    earliest_wake = now + fallback_s

    for feed in feeds:
        last_ts = fetcher.cache.latest_bar_ts(feed.symbol, feed.timeframe)
        if last_ts is None:
            earliest_wake = min(earliest_wake, now + _MIN_SLEEP_S)
            continue
        secs = bar_secs(feed.timeframe)
        next_close = last_ts + 2 * secs + _BAR_CLOSE_GRACE_S
        earliest_wake = min(earliest_wake, next_close)

    sleep_s = max(_MIN_SLEEP_S, earliest_wake - time.time())
    logger.debug("sleeping %.0fs until next scheduled bar close", sleep_s)
    time.sleep(sleep_s)


def main() -> None:
    _setup_logging()

    cfg = DataSvcConfig.from_env()
    logger.info(
        "data-svc starting — %d feed(s) — pg=%s — cdp=%s:%d",
        len(cfg.feeds),
        cfg.postgres_url.split("@")[-1] if "@" in cfg.postgres_url else "<redacted>",
        cfg.cdp_host, cfg.cdp_port,
    )
    for feed in cfg.feeds:
        logger.info("  %s/%s  tv=%s  bar=%.0fs",
                    feed.symbol, feed.timeframe, feed.tv_symbol, bar_secs(feed.timeframe))

    try:
        fetcher = DataFetcher(cfg)
    except TabPinError as exc:
        logger.error("data-svc startup failed: %s", exc)
        raise SystemExit(1)

    last_ts: dict[tuple[str, str], Optional[int]] = {
        (f.symbol, f.timeframe): None for f in cfg.feeds
    }

    while True:
        for feed in cfg.feeds:
            key = (feed.symbol, feed.timeframe)
            try:
                df = fetcher.fetch(feed)
                current_ts = int(df["time"].iloc[-1])
                prev_ts = last_ts[key]

                if prev_ts is None:
                    last_ts[key] = current_ts
                    logger.info("[%s/%s] baseline ts=%d", feed.symbol, feed.timeframe, current_ts)
                elif current_ts > prev_ts:
                    close = float(df["close"].iloc[-1])
                    logger.info(
                        "[%s/%s] bar closed ts=%d close=%.4f",
                        feed.symbol, feed.timeframe, current_ts, close,
                    )
                    last_ts[key] = current_ts

            except DataFetchError as exc:
                logger.warning("[%s/%s] fetch error (retrying next cycle): %s",
                               feed.symbol, feed.timeframe, exc)
            except Exception as exc:
                logger.error("[%s/%s] unexpected error: %s",
                             feed.symbol, feed.timeframe, exc, exc_info=True)

        _sleep_until_next_event(cfg.feeds, fetcher, cfg.poll_interval_s)


if __name__ == "__main__":
    main()
