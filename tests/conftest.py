"""Pytest fixtures.

Spins up an ephemeral Postgres via testcontainers, applies migrations, and
yields a FastAPI TestClient bound to that DB. Each test function gets a
fresh empty schema (TRUNCATE between tests) so order-independence is free.

Skipped automatically if Docker isn't available — useful for partial local
runs that only need the unit/spec tests under `tests/unit/`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _docker_available() -> bool:
    import shutil
    return shutil.which("docker") is not None


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    """Spin up an ephemeral Postgres (with pg_trgm), apply migrations, yield URL."""
    if not _docker_available():
        pytest.skip("docker not available — skipping DB-backed tests")

    from testcontainers.postgres import PostgresContainer

    # postgres:16-alpine matches docker-compose.yml.
    with PostgresContainer(
        image="postgres:16-alpine",
        username="datasvc",
        password="datasvc",
        dbname="datasvc",
    ) as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")
        # testcontainers/postgres exposes a *random* port; the url already has it.

        # Apply migrations in lexical order.
        import psycopg
        migrations_dir = REPO_ROOT / "migrations"
        with psycopg.connect(url, autocommit=True) as conn:
            for sql_path in sorted(migrations_dir.glob("*.sql")):
                conn.execute(sql_path.read_text())

        yield url


@pytest.fixture
def reset_db(pg_url: str) -> None:
    """Truncate all data between tests. Schema stays in place."""
    import psycopg
    with psycopg.connect(pg_url, autocommit=True) as conn:
        conn.execute("TRUNCATE TABLE bars, cache_meta, assets RESTART IDENTITY")


class _FakeBarServiceClient:
    """In-process stand-in for data_svc.rest.grpc_client.BarServiceClient.

    Reads from the same `bars` table the real BarService.GetRecentBars
    would query, so the `seed_bar` fixture works identically against the
    real gRPC server and this stub. `ping()` returns whatever the test
    configured via `set_reachable()`.
    """

    def __init__(self, pg_url: str) -> None:
        from data_svc.db.cache import BarCache
        self._cache = BarCache(pg_url)
        self._reachable = True

    def set_reachable(self, value: bool) -> None:
        self._reachable = value

    def latest_bar(self, storage_symbol: str, timeframe: str, timeout: float = 3.0):
        from data_svc.rest.grpc_client import BarRow
        row = self._cache.latest_bar(storage_symbol, timeframe)
        if row is None:
            return None
        return BarRow(
            ts=row["ts"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            volume=row["volume"],
        )

    def ping(self, timeout: float = 1.5) -> bool:
        return self._reachable

    def close(self) -> None:
        pass


@pytest.fixture
def app(pg_url: str, reset_db, monkeypatch):
    """A fresh FastAPI app pointed at the ephemeral DB.

    Resets data per test; assets catalog is empty unless the test seeds it.
    The BarService gRPC client is swapped for an in-process stub so we
    don't need to spin up a real gRPC server in tests.
    """
    # Force a fresh pool per test session so the pg_url change is honored.
    from data_svc.db import postgres as pg_mod
    pg_mod.close_pool()

    monkeypatch.setenv("POSTGRES_URL", pg_url)
    monkeypatch.setenv("REST_AUTH_TOKEN", "")
    # Don't auto-load any YAML; tests seed via the AssetsRepo directly.
    monkeypatch.setenv("ASSETS_YAML_PATH", "/nonexistent/assets.yaml")

    from data_svc.rest.app import create_app
    from data_svc.rest.settings import RestSettings

    settings = RestSettings()
    return create_app(settings)


@pytest.fixture
def client(app, pg_url):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # Replace the real BarService gRPC client (constructed by the
        # lifespan, which has already run by the time we're past
        # __enter__) with the in-process stub.
        previous = c.app.state.grpc_client
        try:
            previous.close()
        except Exception:
            pass
        c.app.state.grpc_client = _FakeBarServiceClient(pg_url)
        yield c


@pytest.fixture
def seed_asset(pg_url: str):
    """Helper to insert an asset row for the active test."""
    import psycopg

    def _seed(
        symbol: str,
        storage_symbol: str,
        name: str = "Test Asset",
        exchange: str | None = None,
        currency: str = "USD",
        asset_class: str = "CRYPTO",
        asset_subclass: str | None = None,
        isin: str | None = None,
        country: str | None = None,
    ) -> None:
        if exchange is None:
            exchange = symbol.split(":", 1)[0]
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO assets
                     (symbol, storage_symbol, name, exchange, currency,
                      asset_class, asset_subclass, isin, country, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol) DO NOTHING""",
                (symbol, storage_symbol, name, exchange, currency,
                 asset_class, asset_subclass, isin, country),
            )

    return _seed


@pytest.fixture
def seed_bar(pg_url: str):
    """Helper to insert a single bar row."""
    import psycopg

    def _seed(
        storage_symbol: str,
        timeframe: str,
        ts: int,
        close: float = 100.0,
        open_: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float = 1.0,
    ) -> None:
        if open_ is None:
            open_ = close
        if high is None:
            high = close
        if low is None:
            low = close
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO bars
                     (symbol, timeframe, ts, open, high, low, close, volume, fetched_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol, timeframe, ts) DO NOTHING""",
                (storage_symbol, timeframe, ts, open_, high, low, close, volume),
            )

    return _seed
