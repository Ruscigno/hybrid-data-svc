#!/usr/bin/env python3
"""
Docker HEALTHCHECK for data-svc.
Exits 0 (healthy) when the cache holds >=200 bars for the configured symbol/timeframe.
"""
from __future__ import annotations

import os
import sys

from .db.postgres import get_pool
from .db.providers import DEFAULT_PROVIDER


def main() -> int:
    sym = os.getenv("SYMBOL", "BTC/USDT:USDT")
    tf  = os.getenv("TIMEFRAME", "1h")
    pg_url = os.getenv("POSTGRES_URL", "postgresql://datasvc:datasvc@postgres:5432/datasvc")

    try:
        pool = get_pool(pg_url, max_size=2)
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT bar_count FROM cache_meta "
                "WHERE symbol=%s AND timeframe=%s AND provider=%s",
                (sym, tf, DEFAULT_PROVIDER),
            ).fetchone()
        return 0 if row and int(row[0]) >= 200 else 1
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(main())
