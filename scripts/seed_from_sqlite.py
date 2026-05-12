#!/usr/bin/env python3
"""
One-shot migration: copy bars + cache_meta from a legacy SQLite bars.db into Postgres.

Why: the bot previously consumed bars from a shared SQLite file. The new
architecture uses Postgres + gRPC. This script preserves 100% of the historical
bars already collected — no long backfill required from TV or Binance REST.

Usage:
    python -m scripts.seed_from_sqlite --src /seed/bars.db --pg-url <postgres-url>

Idempotent: INSERT ... ON CONFLICT DO NOTHING. Safe to re-run.
After the script finishes it prints per-(symbol, timeframe) counts on both
sides; any non-zero diff is a bug.
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

import psycopg

logger = logging.getLogger(__name__)


_BATCH_SIZE = 5000


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _read_sqlite_counts(src: Path) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    with sqlite3.connect(str(src)) as conn:
        for symbol, timeframe, n in conn.execute(
            "SELECT symbol, timeframe, COUNT(*) FROM bars GROUP BY symbol, timeframe"
        ):
            counts[(symbol, timeframe)] = int(n)
    return counts


def _read_pg_counts(pg: psycopg.Connection) -> dict[tuple[str, str], int]:
    counts: dict[tuple[str, str], int] = {}
    cur = pg.execute(
        "SELECT symbol, timeframe, COUNT(*) FROM bars GROUP BY symbol, timeframe"
    )
    for symbol, timeframe, n in cur:
        counts[(symbol, timeframe)] = int(n)
    return counts


def _seed_bars(src: Path, pg: psycopg.Connection) -> int:
    """Stream rows from SQLite into Postgres in batches. Returns rows inserted."""
    total = 0
    with sqlite3.connect(str(src)) as sqlite_conn:
        sqlite_conn.row_factory = sqlite3.Row
        rows = sqlite_conn.execute(
            "SELECT symbol, timeframe, ts, open, high, low, close, volume, fetched_at "
            "FROM bars ORDER BY symbol, timeframe, ts"
        )

        batch: list[tuple] = []
        with pg.cursor() as cur:
            for r in rows:
                batch.append((
                    r["symbol"], r["timeframe"], int(r["ts"]),
                    float(r["open"]), float(r["high"]),
                    float(r["low"]),  float(r["close"]),
                    float(r["volume"]), int(r["fetched_at"]),
                ))
                if len(batch) >= _BATCH_SIZE:
                    cur.executemany(
                        """INSERT INTO bars
                             (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (symbol, timeframe, ts) DO NOTHING""",
                        batch,
                    )
                    total += len(batch)
                    logger.info("inserted %d rows so far...", total)
                    batch = []

            if batch:
                cur.executemany(
                    """INSERT INTO bars
                         (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (symbol, timeframe, ts) DO NOTHING""",
                    batch,
                )
                total += len(batch)

    pg.commit()
    return total


def _refresh_cache_meta(pg: psycopg.Connection) -> None:
    """Rebuild cache_meta from the bars table after the seed."""
    with pg.cursor() as cur:
        cur.execute("DELETE FROM cache_meta")
        cur.execute(
            """INSERT INTO cache_meta (symbol, timeframe, last_bar_ts, bar_count, last_fetched_at)
               SELECT
                 symbol, timeframe,
                 MAX(ts)                          AS last_bar_ts,
                 COUNT(*)                         AS bar_count,
                 COALESCE(MAX(fetched_at), 0)     AS last_fetched_at
               FROM bars
               GROUP BY symbol, timeframe"""
        )
    pg.commit()


def main() -> int:
    _setup_logging()

    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, help="Path to source bars.db (SQLite)")
    parser.add_argument(
        "--pg-url",
        default=os.getenv("POSTGRES_URL", "postgresql://datasvc:datasvc@postgres:5432/datasvc"),
        help="Postgres connection URL",
    )
    args = parser.parse_args()

    src = Path(args.src)
    if not src.exists():
        logger.error("source SQLite db not found: %s", src)
        return 1

    logger.info("seeding from %s into %s", src, args.pg_url.split("@")[-1])

    src_counts = _read_sqlite_counts(src)
    logger.info("source has %d (symbol, timeframe) buckets", len(src_counts))
    for key, n in sorted(src_counts.items()):
        logger.info("  %s/%s  %d bars", key[0], key[1], n)

    with psycopg.connect(args.pg_url) as pg:
        before = sum(_read_pg_counts(pg).values())
        inserted = _seed_bars(src, pg)
        _refresh_cache_meta(pg)
        after = sum(_read_pg_counts(pg).values())
        pg_counts_final = _read_pg_counts(pg)

    logger.info("rows in postgres: before=%d  after=%d  inserted=%d", before, after, inserted)

    # Validation: per-(symbol, timeframe) match
    ok = True
    for key, sqlite_n in sorted(src_counts.items()):
        pg_n = pg_counts_final.get(key, 0)
        if pg_n != sqlite_n:
            logger.error("MISMATCH %s/%s — sqlite=%d pg=%d (diff=%+d)",
                         key[0], key[1], sqlite_n, pg_n, pg_n - sqlite_n)
            ok = False
        else:
            logger.info("OK       %s/%s — %d bars in both", key[0], key[1], sqlite_n)

    if not ok:
        logger.error("validation FAILED — investigate before cutover")
        return 2

    logger.info("validation PASSED — all (symbol, timeframe) buckets match")
    return 0


if __name__ == "__main__":
    sys.exit(main())
