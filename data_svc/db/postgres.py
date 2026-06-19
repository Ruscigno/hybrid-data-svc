"""
Postgres connection pool, lazily initialized.

We use psycopg (v3) with a small ConnectionPool. The pool is shared across
BarCache, healthcheck, and the gRPC service.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

_pool: Optional[ConnectionPool] = None
_lock = threading.Lock()


def get_pool(conninfo: str, max_size: int = 8) -> ConnectionPool:
    """Return the process-wide connection pool, creating it on first call."""
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                logger.info("opening postgres pool (max_size=%d)", max_size)
                _pool = ConnectionPool(
                    conninfo,
                    min_size=1,
                    max_size=max_size,
                    open=True,
                    kwargs={"autocommit": False},
                )
                _pool.wait(timeout=30.0)
    return _pool


def close_pool() -> None:
    """Close the pool (for tests / clean shutdown)."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


def assert_provider_schema(conninfo: str) -> None:
    """Fail fast at boot if migration 004 (provider column) is not applied."""
    pool = get_pool(conninfo)
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.columns "
            "WHERE table_name IN ('bars','cache_meta') AND column_name='provider'",
        ).fetchall()
    have = {r[0] for r in rows}
    missing = {"bars", "cache_meta"} - have
    if missing:
        raise RuntimeError(
            f"schema out of date: {sorted(missing)} missing the 'provider' column. "
            "Apply migrations/004_provider.sql before starting this service."
        )
