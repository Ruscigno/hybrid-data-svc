"""
Data service — multi-feed rotating scraper with schedule-aware refresh.

For each configured feed:
  1. Checks whether a new bar can have closed (fast, DB-only check).
  2. If yes  → switches chart (if needed) and fetches from TradingView.
  3. If no   → skips the TV call entirely; cache is mathematically fresh.

The feed list is now driven by the `feeds` Postgres table (Phase 3 of the
REST API spec). FEEDS env is upserted into that table at startup; further
additions via POST /v1/assets are picked up by the next poll cycle without
restart. Pending feeds are promoted to 'active' on first successful insert.

After every cycle the service sleeps until the soonest upcoming bar close
across all currently-polling feeds (plus a small grace period). Chart
switches are serialized: the loop is single-threaded and processes one
feed at a time.

Usage: python -m data_svc
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from .config import DataSvcConfig, Feed
from .db.audit import audit_integrity, format_audit
from .db.feeds import FeedRow, FeedsRepo
from .fetcher import DataFetchError, DataFetcher, bar_secs
from .services import feeds_loader
from .tab_pin import TabPinError


_MIN_SLEEP_S = 5.0
_BAR_CLOSE_GRACE_S = 5.0
# Service-internal cron for the integrity audit (gap + off-grid scan).
# Runs once per day after a poll cycle completes; pure read-only — no auto-fix.
_AUDIT_INTERVAL_S = 86400.0  # 24h


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger(__name__)


def _feedrow_to_feed(row: FeedRow) -> Feed:
    """Convert the DB row to the Feed NamedTuple the fetcher expects."""
    return Feed(symbol=row.storage_symbol, timeframe=row.timeframe, tv_symbol=row.tv_symbol)


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
    cdp_origin = (
        f"{cfg.cdp_host}:{cfg.cdp_port} (env)"
        if cfg.cdp_host is not None and cfg.cdp_port is not None
        else "auto-discover"
    )
    logger.info(
        "data-svc starting — %d env feed(s) — pg=%s — cdp=%s",
        len(cfg.feeds),
        cfg.postgres_url.split("@")[-1] if "@" in cfg.postgres_url else "<redacted>",
        cdp_origin,
    )

    # Seed the feeds table from env (idempotent — no-op when rows exist).
    # The DB is the runtime source of truth from here on.
    feeds_repo = FeedsRepo(cfg.postgres_url)
    seeded = feeds_loader.sync(feeds_repo, cfg.feeds)
    if seeded:
        logger.info("[feeds-loader] seeded %d new feed row(s)", seeded)

    initial_targets = feeds_repo.polling_targets()
    for row in initial_targets:
        logger.info(
            "  %s/%s  tv=%s  status=%s  bar=%.0fs",
            row.storage_symbol, row.timeframe, row.tv_symbol, row.status,
            bar_secs(row.timeframe),
        )

    try:
        fetcher = DataFetcher(cfg)
    except TabPinError as exc:
        logger.error("data-svc startup failed: %s", exc)
        raise SystemExit(1)

    last_ts: dict[tuple[str, str], Optional[int]] = {}
    last_audit_ts: float = 0.0  # fires at first cycle, then every 24h

    while True:
        # Re-read the polling target list at the top of each cycle so newly
        # POSTed assets (which insert into feeds with status='pending') are
        # picked up without restart. The list is small (dozens of rows);
        # one SELECT per cycle is well below the cost of the TV fetches it
        # gates.
        targets = feeds_repo.polling_targets()
        feeds_for_sleep: list[Feed] = []
        for row in targets:
            feed = _feedrow_to_feed(row)
            feeds_for_sleep.append(feed)
            key = (feed.symbol, feed.timeframe)
            last_ts.setdefault(key, None)  # lazy init for feeds added at runtime

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

                # Promote pending → active. No-op when already active.
                if row.status == "pending":
                    feeds_repo.mark_active(feed.symbol, feed.timeframe)

            except DataFetchError as exc:
                logger.warning("[%s/%s] fetch error (retrying next cycle): %s",
                               feed.symbol, feed.timeframe, exc)
            except Exception as exc:
                logger.error("[%s/%s] unexpected error: %s",
                             feed.symbol, feed.timeframe, exc, exc_info=True)

        # Service-internal cron: integrity audit once per day.
        now = time.time()
        if now - last_audit_ts >= _AUDIT_INTERVAL_S:
            try:
                report = audit_integrity(cfg.postgres_url)
                level = logging.WARNING if report.problems else logging.INFO
                for line in format_audit(report).splitlines():
                    logger.log(level, line)
            except Exception:
                logger.exception("[AUDIT] integrity audit failed")
            last_audit_ts = now

        _sleep_until_next_event(feeds_for_sleep, fetcher, cfg.poll_interval_s)


if __name__ == "__main__":
    main()
