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


_COLUMNS = (
    "symbol, storage_symbol, name, exchange, currency, asset_class, "
    "asset_subclass, isin, country"
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
    )


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
        """
        now = int(time.time())
        payload = [
            (
                r.symbol, r.storage_symbol, r.name, r.exchange, r.currency,
                r.asset_class, r.asset_subclass, r.isin, r.country, now,
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
                              asset_class, asset_subclass, isin, country, updated_at)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
