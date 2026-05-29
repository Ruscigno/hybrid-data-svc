"""Feeds repository — runtime source of truth for what data-svc polls.

Phase 3 introduces this table to replace the env-only FEEDS list, so new
symbols POSTed via /v1/assets are picked up by the writer on its next
poll cycle without a restart.

Lifecycle:
  - data-svc startup calls `seed_from_env()` to upsert env-configured feeds.
    Initial status is computed from cache_meta presence: feeds that already
    have stored bars are seeded as 'active'; everything else is 'pending'.
  - data-svc top of each poll cycle calls `polling_targets()` to get the
    current list of (storage_symbol, timeframe, tv_symbol) to fetch.
  - After a successful bar insert, data-svc calls `mark_active()` which
    promotes 'pending' → 'active'. No-op when already active.
  - AssetService.CreateAsset calls `upsert()` with status='pending' for
    each timeframe requested via POST /v1/assets.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from psycopg_pool import ConnectionPool

from ..config import Feed
from .postgres import get_pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FeedRow:
    """A single (storage_symbol, timeframe) polling target."""

    storage_symbol: str
    timeframe: str
    tv_symbol: str
    status: str  # 'active' | 'pending' | 'inactive'


_COLUMNS = "storage_symbol, timeframe, tv_symbol, status"


def _row_to_feed(row: tuple) -> FeedRow:
    return FeedRow(
        storage_symbol=row[0],
        timeframe=row[1],
        tv_symbol=row[2],
        status=row[3],
    )


class FeedsRepo:
    """Postgres-backed feeds table. Process-wide (shares the pool)."""

    def __init__(self, postgres_url: str) -> None:
        self._url = postgres_url
        self._pool: ConnectionPool = get_pool(postgres_url)

    # --- read paths -------------------------------------------------------

    def polling_targets(self) -> list[FeedRow]:
        """Return the (storage_symbol, timeframe, tv_symbol, status) rows
        the writer should fetch — i.e. status IN ('active','pending').

        Ordered (storage_symbol, timeframe) so the writer's chart-switch
        path is deterministic across cycles.
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {_COLUMNS}
                          FROM feeds
                         WHERE status IN ('active', 'pending')
                         ORDER BY storage_symbol, timeframe"""
                )
                rows = cur.fetchall()
        return [_row_to_feed(r) for r in rows]

    def list_for_storage_symbol(self, storage_symbol: str) -> list[FeedRow]:
        """All feeds (any status) for a given storage_symbol — used by the
        list_with_status JOIN when callers want the per-asset view."""
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {_COLUMNS}
                          FROM feeds
                         WHERE storage_symbol = %s
                         ORDER BY timeframe""",
                    (storage_symbol,),
                )
                rows = cur.fetchall()
        return [_row_to_feed(r) for r in rows]

    # --- write paths ------------------------------------------------------

    def seed_from_env(self, env_feeds: Iterable[Feed]) -> int:
        """Idempotent INSERT for each env-configured feed. Computes initial
        status from cache_meta: rows that already have stored bars are
        seeded 'active'; the rest are 'pending'.

        Returns the number of rows the INSERT actually wrote (zero on
        subsequent runs, since `ON CONFLICT DO NOTHING`).
        """
        now = int(time.time())
        payload = [
            (f.symbol, f.timeframe, f.tv_symbol, now) for f in env_feeds
        ]
        if not payload:
            return 0
        # Two queries in one connection: insert new rows with status derived
        # from cache_meta presence at insert time.
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO feeds
                             (storage_symbol, timeframe, tv_symbol, status, updated_at)
                           VALUES (
                             %s, %s, %s,
                             CASE WHEN EXISTS (
                               SELECT 1 FROM cache_meta
                                WHERE symbol = %s AND timeframe = %s
                             ) THEN 'active' ELSE 'pending' END,
                             %s
                           )
                           ON CONFLICT (storage_symbol, timeframe) DO NOTHING""",
                        [
                            (sym, tf, tv, sym, tf, ts)
                            for (sym, tf, tv, ts) in payload
                        ],
                    )
                    written = cur.rowcount
        logger.info("[feeds-loader] seeded %d feed row(s) from env", written)
        return int(written or 0)

    def upsert(
        self,
        storage_symbol: str,
        timeframe: str,
        tv_symbol: str,
        status: str = "pending",
    ) -> None:
        """Insert-or-update a single feed row. Used by AssetService.CreateAsset
        to register the (storage_symbol, timeframe) pair the new asset
        should be polled at."""
        now = int(time.time())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO feeds
                         (storage_symbol, timeframe, tv_symbol, status, updated_at)
                       VALUES (%s, %s, %s, %s, %s)
                       ON CONFLICT (storage_symbol, timeframe) DO UPDATE SET
                         tv_symbol   = EXCLUDED.tv_symbol,
                         updated_at  = EXCLUDED.updated_at""",
                    (storage_symbol, timeframe, tv_symbol, status, now),
                )

    def mark_active(self, storage_symbol: str, timeframe: str) -> None:
        """Promote 'pending' → 'active'. No-op when already active or inactive.

        Called by the writer after a successful `insert_bars`. Cheap UPDATE;
        idempotent.
        """
        now = int(time.time())
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE feeds SET status = 'active', updated_at = %s
                         WHERE storage_symbol = %s
                           AND timeframe = %s
                           AND status = 'pending'""",
                    (now, storage_symbol, timeframe),
                )
