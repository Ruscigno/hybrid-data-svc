"""Pytest fixtures.

Spins up an ephemeral Postgres via testcontainers, applies migrations, and
yields a FastAPI TestClient whose gRPC client is swapped for an in-process
stub backed by the same DB. Each test function gets a fresh empty schema
(TRUNCATE between tests) so order-independence is free.

Skipped automatically if Docker isn't available — useful for partial local
runs that only need the unit/spec tests under `tests/unit/`.
"""

from __future__ import annotations

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

    with PostgresContainer(
        image="postgres:16-alpine",
        username="datasvc",
        password="datasvc",
        dbname="datasvc",
    ) as pg:
        url = pg.get_connection_url().replace("postgresql+psycopg2://", "postgresql://")

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


class _FakeGrpcClient:
    """In-process stand-in for data_svc.rest.grpc_client.BarServiceClient.

    Backed by the same BarCache + AssetsRepo the real BarService /
    AssetService servicers would use, so REST tests don't need to spin
    up an actual gRPC server but exercise the real business logic in the
    Python service modules.

    `set_reachable(False)` simulates a transport-level gRPC failure: ping()
    returns (False, False) and other methods raise an RpcError-like object.
    """

    def __init__(self, pg_url: str) -> None:
        from data_svc.db.assets import AssetsRepo
        from data_svc.db.cache import BarCache
        self._cache = BarCache(pg_url)
        self._assets_repo = AssetsRepo(pg_url)
        self._reachable = True

    def set_reachable(self, value: bool) -> None:
        self._reachable = value

    def _check_reachable(self) -> None:
        if not self._reachable:
            import grpc

            class _UnavailableError(grpc.RpcError):
                def code(self):  # noqa: D401
                    return grpc.StatusCode.UNAVAILABLE

                def details(self):  # noqa: D401
                    return "fake gRPC unreachable"

            raise _UnavailableError()

    # --- BarService -------------------------------------------------------

    def latest_bar(self, storage_symbol: str, timeframe: str, timeout: float = 3.0):
        self._check_reachable()
        from data_svc.rest.grpc_client import BarRow
        row = self._cache.latest_bar(storage_symbol, timeframe)
        if row is None:
            return None
        return BarRow(
            ts=row["ts"], open=row["open"], high=row["high"],
            low=row["low"], close=row["close"], volume=row["volume"],
        )

    def bars_in_range(self, storage_symbol: str, timeframe: str, from_ts: int, to_ts: int, limit: int, timeout: float = 5.0):
        self._check_reachable()
        from data_svc.rest.grpc_client import BarRow
        rows, truncated = self._cache.get_bars_in_range(storage_symbol, timeframe, from_ts, to_ts, limit)
        return (
            [
                BarRow(ts=r["ts"], open=r["open"], high=r["high"], low=r["low"], close=r["close"], volume=r["volume"])
                for r in rows
            ],
            truncated,
        )

    def ping(self, timeout: float = 1.5):
        if not self._reachable:
            return False, False
        try:
            with self._cache._pool.connection() as conn:  # noqa: SLF001
                conn.execute("SELECT 1").fetchone()
            return True, True
        except Exception:
            return True, False

    # --- AssetService -----------------------------------------------------

    def get_asset_profile(self, tv_symbol: str, timeout: float = 1.5):
        self._check_reachable()
        from data_svc.rest.grpc_client import AssetRow
        row = self._assets_repo.get(tv_symbol)
        if row is None:
            return None
        return AssetRow(
            symbol=row.symbol, storage_symbol=row.storage_symbol, name=row.name,
            exchange=row.exchange, currency=row.currency, asset_class=row.asset_class,
            asset_subclass=row.asset_subclass, isin=row.isin, country=row.country,
        )

    def search_assets(self, query: str, limit: int, timeout: float = 2.0):
        self._check_reachable()
        from data_svc.rest.grpc_client import AssetRow
        rows = self._assets_repo.search(query, limit)
        return [
            AssetRow(
                symbol=r.symbol, storage_symbol=r.storage_symbol, name=r.name,
                exchange=r.exchange, currency=r.currency, asset_class=r.asset_class,
                asset_subclass=r.asset_subclass, isin=r.isin, country=r.country,
            )
            for r in rows
        ]

    def close(self) -> None:
        pass


@pytest.fixture
def app(pg_url: str, reset_db, monkeypatch):
    """Fresh FastAPI app whose gRPC client is the in-process stub."""
    from data_svc.db import postgres as pg_mod
    pg_mod.close_pool()

    # REST itself no longer reads POSTGRES_URL, but the FakeGrpcClient does
    # (to reach the ephemeral DB). Set it for the fake's benefit.
    monkeypatch.setenv("POSTGRES_URL", pg_url)
    monkeypatch.setenv("REST_AUTH_TOKEN", "")

    from data_svc.rest.app import create_app
    from data_svc.rest.settings import RestSettings

    settings = RestSettings()
    return create_app(settings)


@pytest.fixture
def client(app, pg_url):
    from fastapi.testclient import TestClient
    with TestClient(app) as c:
        # Replace the real gRPC client (constructed by lifespan, already
        # entered by TestClient.__enter__) with the in-process stub.
        previous = c.app.state.grpc_client
        try:
            previous.close()
        except Exception:
            pass
        c.app.state.grpc_client = _FakeGrpcClient(pg_url)
        yield c


@pytest.fixture
def authed_client(pg_url, reset_db, monkeypatch):
    """Same as `client` but with REST_AUTH_TOKEN=secret. Used by auth +
    Ghostfolio contract tests."""
    from data_svc.db import postgres as pg_mod
    pg_mod.close_pool()

    monkeypatch.setenv("POSTGRES_URL", pg_url)
    monkeypatch.setenv("REST_AUTH_TOKEN", "secret")

    from fastapi.testclient import TestClient

    from data_svc.rest.app import create_app
    from data_svc.rest.settings import RestSettings

    settings = RestSettings()
    with TestClient(create_app(settings)) as c:
        previous = c.app.state.grpc_client
        try:
            previous.close()
        except Exception:
            pass
        c.app.state.grpc_client = _FakeGrpcClient(pg_url)
        yield c


@pytest.fixture
def seed_asset(pg_url: str):
    """Insert an asset row directly into the assets table."""
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
    """Insert a single bar row directly into the bars table."""
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
