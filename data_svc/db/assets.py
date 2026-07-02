"""
Asset catalog repository.

Bridges TradingView identifiers (used by the REST API and assets.symbol PK)
to ccxt storage identifiers (used by bars.symbol). The same row carries
human-readable metadata that powers /v1/search and /v1/profile.

The repo is consumed in-process by both the REST layer (data_svc/rest/) and
any future gRPC RPC — it is the shared service layer for asset metadata.
Never mix business rules into the routers/handlers; they belong here.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from psycopg_pool import ConnectionPool

from .postgres import get_pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AssetRow:
    """Asset catalog row.

    `symbol` is the external (TradingView) identity surfaced to REST callers;
    `storage_symbol` is the internal ccxt key joined against bars.symbol.
    Optional fields (`asset_subclass`, `isin`, `country`) are `None` when
    unknown and must be omitted (not nulled) in REST responses.

    `added_at` is unix-seconds set on first insert and preserved across
    upserts. Defaults to 0 when constructed by callers that don't have it
    (e.g. assets_loader passing YAML rows) — the upsert path sets the real
    value at INSERT time.
    """

    symbol: str
    storage_symbol: str
    name: str
    exchange: str
    currency: str
    asset_class: str
    asset_subclass: Optional[str] = None
    isin: Optional[str] = None
    country: Optional[str] = None
    added_at: int = 0


@dataclass(frozen=True)
class AssetWithStatusRow:
    """AssetRow + runtime fields aggregated from feeds + cache_meta.

    Returned by `list_with_status` to back GET /v1/assets. Kept separate
    from AssetRow so callers that only want the catalog row (e.g.
    /v1/profile) don't have to ignore status fields.
    """

    asset: AssetRow
    status: str  # 'active' | 'pending' | 'inactive'
    last_bar_ts: int  # 0 when no bars yet


_COLUMNS = (
    "symbol, storage_symbol, name, exchange, currency, asset_class, "
    "asset_subclass, isin, country, added_at"
)


def _row_to_asset(row: tuple) -> AssetRow:
    return AssetRow(
        symbol=row[0],
        storage_symbol=row[1],
        name=row[2],
        exchange=row[3],
        currency=row[4],
        asset_class=row[5],
        asset_subclass=row[6],
        isin=row[7],
        country=row[8],
        added_at=int(row[9]) if row[9] is not None else 0,
    )


def _row_to_asset_with_status(row: tuple) -> AssetWithStatusRow:
    return AssetWithStatusRow(
        asset=_row_to_asset(row[:10]),
        status=row[10],
        last_bar_ts=int(row[11]) if row[11] is not None else 0,
    )


_VALID_ASSET_CLASSES = {"EQUITY", "CRYPTO", "ETF", "FUND"}


class AssetsRepo:
    """Postgres-backed asset catalog. Lifetime: process-wide (shares the pool)."""

    def __init__(self, postgres_url: str) -> None:
        self._url = postgres_url
        self._pool: ConnectionPool = get_pool(postgres_url)

    def get(self, tv_symbol: str) -> Optional[AssetRow]:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM assets WHERE symbol = %s",
                (tv_symbol,),
            ).fetchone()
        return _row_to_asset(row) if row else None

    def resolve(self, tv_symbol: str) -> Optional[AssetRow]:
        """Alias of `get`, kept distinct so calling sites read as intent.

        The REST layer calls `resolve()` when it needs the bridge from
        TV → storage; it calls `get()` when it wants to return the row to
        the client. Same query today; allowed to diverge later.
        """
        return self.get(tv_symbol)

    def search(self, q: str, limit: int) -> list[AssetRow]:
        """Case-insensitive substring match against (symbol, name).

        Uses the pg_trgm GIN index on (lower(symbol) || ' ' || lower(name)).
        The composite index handles substring matches across both fields in
        one scan; per-field ILIKEs would force a sequential scan.
        """
        q = q.strip()
        if not q:
            return []
        pattern = f"%{q.lower()}%"
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""SELECT {_COLUMNS}
                          FROM assets
                         WHERE (lower(symbol) || ' ' || lower(name)) LIKE %s
                         ORDER BY symbol
                         LIMIT %s""",
                    (pattern, int(limit)),
                )
                rows = cur.fetchall()
        return [_row_to_asset(r) for r in rows]

    def count(self) -> int:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM assets").fetchone()
        return int(row[0]) if row else 0

    def upsert_many(self, rows: Iterable[AssetRow]) -> int:
        """Insert-or-update each row. Returns the number of rows written.

        Loader contract: upsert-only. Rows present in the DB but absent from
        the incoming iterable are NOT deleted — that's an explicit decision
        to avoid a bad assets.yaml edit wiping the catalog on next restart.

        `added_at` is set to `now()` on INSERT and PRESERVED on UPDATE so the
        catalog tracks when each symbol was first onboarded.
        """
        now = int(time.time())
        payload = [
            (
                r.symbol, r.storage_symbol, r.name, r.exchange, r.currency,
                r.asset_class, r.asset_subclass, r.isin, r.country, now, now,
            )
            for r in rows
        ]
        if not payload:
            return 0
        with self._pool.connection() as conn:
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO assets
                             (symbol, storage_symbol, name, exchange, currency,
                              asset_class, asset_subclass, isin, country,
                              updated_at, added_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                           ON CONFLICT (symbol) DO UPDATE SET
                             storage_symbol = EXCLUDED.storage_symbol,
                             name           = EXCLUDED.name,
                             exchange       = EXCLUDED.exchange,
                             currency       = EXCLUDED.currency,
                             asset_class    = EXCLUDED.asset_class,
                             asset_subclass = EXCLUDED.asset_subclass,
                             isin           = EXCLUDED.isin,
                             country        = EXCLUDED.country,
                             updated_at     = EXCLUDED.updated_at""",
                        payload,
                    )
        return len(payload)

    # ------------------------------------------------------------------
    # Phase 3 — catalog management
    # ------------------------------------------------------------------

    def list_with_status(
        self,
        exchange: Optional[str],
        asset_class: Optional[str],
        q: Optional[str],
        cursor: Optional[str],
        limit: int,
    ) -> tuple[list[AssetWithStatusRow], Optional[str]]:
        """Paginated catalog list with per-asset runtime status.

        Cursor pagination: rows are returned in ascending `symbol` order.
        `cursor` is the last `symbol` from the previous page (exclusive).
        Returns `(rows, next_cursor)`; `next_cursor` is None when no more
        pages.

        Status aggregation rule (priority): if any feed is 'active' → asset
        is 'active'; else if any 'pending' → 'pending'; else 'inactive'.
        Assets without any feeds default to 'pending'.

        `last_bar_ts` is the max across all (symbol, timeframe) pairs the
        asset has feeds for, joined against cache_meta. 0 when no bars yet.
        """
        clamped_limit = max(1, min(int(limit), 500))
        params: list = []
        where_clauses: list[str] = []
        if exchange:
            where_clauses.append("a.exchange = %s")
            params.append(exchange)
        if asset_class:
            where_clauses.append("a.asset_class = %s")
            params.append(asset_class)
        if q:
            where_clauses.append("(lower(a.symbol) || ' ' || lower(a.name)) LIKE %s")
            params.append(f"%{q.lower()}%")
        if cursor:
            where_clauses.append("a.symbol > %s")
            params.append(cursor)
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        params.append(clamped_limit + 1)  # sentinel +1 for next-cursor detection

        sql = f"""
            SELECT
                a.symbol, a.storage_symbol, a.name, a.exchange, a.currency,
                a.asset_class, a.asset_subclass, a.isin, a.country, a.added_at,
                CASE
                    WHEN bool_or(f.status = 'active')   THEN 'active'
                    WHEN bool_or(f.status = 'pending')  THEN 'pending'
                    WHEN bool_or(f.status = 'inactive') THEN 'inactive'
                    ELSE 'pending'
                END AS agg_status,
                COALESCE(MAX(cm.last_bar_ts), 0)::bigint AS max_last_bar_ts
            FROM assets a
            LEFT JOIN feeds f
              ON f.storage_symbol = a.storage_symbol
            LEFT JOIN cache_meta cm
              ON cm.symbol = a.storage_symbol AND cm.timeframe = f.timeframe
            {where_sql}
            GROUP BY a.symbol, a.storage_symbol, a.name, a.exchange, a.currency,
                     a.asset_class, a.asset_subclass, a.isin, a.country, a.added_at
            ORDER BY a.symbol
            LIMIT %s
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        next_cursor: Optional[str] = None
        if len(rows) > clamped_limit:
            # We over-fetched by 1 to detect whether there's another page.
            rows = rows[:clamped_limit]
            next_cursor = rows[-1][0]

        return ([_row_to_asset_with_status(r) for r in rows], next_cursor)

    def get_with_status(self, tv_symbol: str) -> Optional[AssetWithStatusRow]:
        """Lookup-by-symbol variant that also returns status + last_bar_ts.

        Used by AssetService.CreateAsset when the symbol already exists, so
        the 409 response can carry the existing row with its current runtime
        state (not just the catalog metadata).
        """
        sql = f"""
            SELECT
                a.symbol, a.storage_symbol, a.name, a.exchange, a.currency,
                a.asset_class, a.asset_subclass, a.isin, a.country, a.added_at,
                CASE
                    WHEN bool_or(f.status = 'active')   THEN 'active'
                    WHEN bool_or(f.status = 'pending')  THEN 'pending'
                    WHEN bool_or(f.status = 'inactive') THEN 'inactive'
                    ELSE 'pending'
                END AS agg_status,
                COALESCE(MAX(cm.last_bar_ts), 0)::bigint AS max_last_bar_ts
            FROM assets a
            LEFT JOIN feeds f
              ON f.storage_symbol = a.storage_symbol
            LEFT JOIN cache_meta cm
              ON cm.symbol = a.storage_symbol AND cm.timeframe = f.timeframe
            WHERE a.symbol = %s
            GROUP BY a.symbol, a.storage_symbol, a.name, a.exchange, a.currency,
                     a.asset_class, a.asset_subclass, a.isin, a.country, a.added_at
        """
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (tv_symbol,))
                row = cur.fetchone()
        return _row_to_asset_with_status(row) if row else None

    def create_with_feeds(
        self,
        asset: AssetRow,
        timeframes: list[str],
        tv_symbol: str,
    ) -> tuple[AssetRow, bool]:
        """Atomic create-asset + register feeds.

        Returns `(asset, created)` where `created` is True for a fresh
        insert, False when the symbol already exists (in which case the
        returned `asset` is the existing row, untouched).

        Both the asset INSERT and the feeds INSERTs happen in a single
        transaction. Feed rows are created with `status='pending'`; the
        writer flips them to 'active' on first successful bar insert.

        Validation (server-side, matches gRPC INVALID_ARGUMENT translation
        at the route layer):
          - asset.asset_class must be one of EQUITY|CRYPTO|ETF|FUND
          - asset.symbol must be non-empty (regex validation is the caller's job)
          - timeframes must be non-empty
        Caller is responsible for checking these; this method assumes
        inputs are valid.
        """
        now = int(time.time())
        with self._pool.connection() as conn:
            with conn.transaction():
                existing = conn.execute(
                    f"SELECT {_COLUMNS} FROM assets WHERE symbol = %s FOR UPDATE",
                    (asset.symbol,),
                ).fetchone()
                if existing is not None:
                    return (_row_to_asset(existing), False)

                # Insert the asset row.
                conn.execute(
                    """INSERT INTO assets
                         (symbol, storage_symbol, name, exchange, currency,
                          asset_class, asset_subclass, isin, country,
                          updated_at, added_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        asset.symbol, asset.storage_symbol, asset.name,
                        asset.exchange, asset.currency, asset.asset_class,
                        asset.asset_subclass, asset.isin, asset.country,
                        now, now,
                    ),
                )

                # Insert one feeds row per requested timeframe. ON CONFLICT
                # DO NOTHING is defensive — repeated (storage_symbol, tf)
                # entries in `timeframes` shouldn't error.
                with conn.cursor() as cur:
                    cur.executemany(
                        """INSERT INTO feeds
                             (storage_symbol, timeframe, provider_symbol, provider, status, updated_at)
                           VALUES (%s, %s, %s, 'tradingview', 'pending', %s)
                           ON CONFLICT (storage_symbol, timeframe, provider) DO NOTHING""",
                        [
                            (asset.storage_symbol, tf, tv_symbol, now)
                            for tf in timeframes
                        ],
                    )

        # Return the asset with the populated added_at we just set.
        created_row = AssetRow(
            symbol=asset.symbol,
            storage_symbol=asset.storage_symbol,
            name=asset.name,
            exchange=asset.exchange,
            currency=asset.currency,
            asset_class=asset.asset_class,
            asset_subclass=asset.asset_subclass,
            isin=asset.isin,
            country=asset.country,
            added_at=now,
        )
        return (created_row, True)
