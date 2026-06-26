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
        conn.execute("TRUNCATE TABLE bars, cache_meta, assets, feeds RESTART IDENTITY")


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

    def list_assets(
        self,
        exchange,
        asset_class,
        q,
        cursor,
        limit: int,
        timeout: float = 3.0,
    ):
        """In-process equivalent of the gRPC ListAssets RPC.

        Goes through the same AssetsRepo.list_with_status query the real
        servicer uses, so the tests exercise the JOIN/aggregation SQL end
        to end.
        """
        self._check_reachable()
        from data_svc.rest.grpc_client import (
            AssetRow as ClientAssetRow,
        )
        from data_svc.rest.grpc_client import (
            AssetWithStatusRow as ClientAssetWithStatusRow,
        )
        # Mirror the servicer's clamp.
        clamped_limit = max(1, min(int(limit) if limit else 100, 500))
        rows, next_cursor = self._assets_repo.list_with_status(
            exchange=exchange or None,
            asset_class=asset_class or None,
            q=q or None,
            cursor=cursor or None,
            limit=clamped_limit,
        )
        out = []
        for r in rows:
            inner = ClientAssetRow(
                symbol=r.asset.symbol,
                storage_symbol=r.asset.storage_symbol,
                name=r.asset.name,
                exchange=r.asset.exchange,
                currency=r.asset.currency,
                asset_class=r.asset.asset_class,
                asset_subclass=r.asset.asset_subclass,
                isin=r.asset.isin,
                country=r.asset.country,
            )
            out.append(
                ClientAssetWithStatusRow(
                    asset=inner,
                    status=r.status,
                    added_at=r.asset.added_at,
                    last_bar_ts=r.last_bar_ts,
                )
            )
        return out, next_cursor

    def create_asset(self, asset, timeframes, tv_symbol, timeout: float = 5.0):
        """In-process equivalent of the gRPC CreateAsset RPC.

        Mirrors AssetServiceServicer.CreateAsset (validation rules + default
        rules + create_with_feeds call + get_with_status fallback for the
        already-exists path) so the routing-layer tests exercise the same
        end-to-end behaviour as production would.
        """
        self._check_reachable()
        import re

            # Local imports to avoid pulling grpc into module scope.
        import grpc

        from data_svc.db.assets import AssetRow as DbAssetRow
        from data_svc.db.assets import AssetWithStatusRow as DbAssetWithStatusRow
        from data_svc.rest.grpc_client import (
            AssetRow as ClientAssetRow,
        )
        from data_svc.rest.grpc_client import (
            AssetWithStatusRow as ClientAssetWithStatusRow,
        )

        _TV_SYMBOL_RE = re.compile(r"^[A-Z0-9]+:[A-Z0-9._-]+$")
        _VALID_ASSET_CLASSES = {"EQUITY", "CRYPTO", "ETF", "FUND"}
        _DEFAULT_TIMEFRAMES = ("1h",)
        _POLL_ETA_SECONDS = 30

        class _InvalidArgError(grpc.RpcError):
            def __init__(self, msg: str) -> None:
                self._msg = msg

            def code(self):  # noqa: D401
                return grpc.StatusCode.INVALID_ARGUMENT

            def details(self):  # noqa: D401
                return self._msg

        # Mirror the servicer's validation. The router maps INVALID_ARGUMENT
        # to 422, so these become 422 responses in the tests.
        if not asset.symbol or not _TV_SYMBOL_RE.match(asset.symbol):
            raise _InvalidArgError("asset.symbol is invalid")
        if not asset.storage_symbol:
            raise _InvalidArgError("asset.storage_symbol is required")
        if not asset.name:
            raise _InvalidArgError("asset.name is required")
        if not asset.exchange:
            raise _InvalidArgError("asset.exchange is required")
        if not asset.currency:
            raise _InvalidArgError("asset.currency is required")
        if asset.asset_class not in _VALID_ASSET_CLASSES:
            raise _InvalidArgError("asset.asset_class is invalid")

        tfs = list(timeframes) or list(_DEFAULT_TIMEFRAMES)
        tv = tv_symbol or asset.symbol

        db_row = DbAssetRow(
            symbol=asset.symbol,
            storage_symbol=asset.storage_symbol,
            name=asset.name,
            exchange=asset.exchange,
            currency=asset.currency,
            asset_class=asset.asset_class,
            asset_subclass=asset.asset_subclass,
            isin=asset.isin,
            country=asset.country,
        )
        result_row, created = self._assets_repo.create_with_feeds(
            db_row, tfs, tv,
        )

        def _to_client(r: DbAssetWithStatusRow) -> ClientAssetWithStatusRow:
            return ClientAssetWithStatusRow(
                asset=ClientAssetRow(
                    symbol=r.asset.symbol,
                    storage_symbol=r.asset.storage_symbol,
                    name=r.asset.name,
                    exchange=r.asset.exchange,
                    currency=r.asset.currency,
                    asset_class=r.asset.asset_class,
                    asset_subclass=r.asset.asset_subclass,
                    isin=r.asset.isin,
                    country=r.asset.country,
                ),
                status=r.status,
                added_at=r.asset.added_at,
                last_bar_ts=r.last_bar_ts,
            )

        if not created:
            existing = self._assets_repo.get_with_status(result_row.symbol)
            assert existing is not None, "row vanished between conflict and re-read"
            return _to_client(existing), False, _POLL_ETA_SECONDS

        fresh = DbAssetWithStatusRow(
            asset=result_row,
            status="pending",
            last_bar_ts=0,
        )
        return _to_client(fresh), True, _POLL_ETA_SECONDS

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
def admin_client(pg_url, reset_db, monkeypatch):
    """TestClient with REST_ADMIN_TOKEN=admin set (and no REST_AUTH_TOKEN).

    Used by catalog-management tests to verify the admin-token precedence
    rule in data_svc/rest/auth.py.
    """
    from data_svc.db import postgres as pg_mod
    pg_mod.close_pool()

    monkeypatch.setenv("POSTGRES_URL", pg_url)
    monkeypatch.setenv("REST_AUTH_TOKEN", "")
    monkeypatch.setenv("REST_ADMIN_TOKEN", "admin")

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
        added_at: int | None = None,
    ) -> None:
        if exchange is None:
            exchange = symbol.split(":", 1)[0]
        with psycopg.connect(pg_url, autocommit=True) as conn:
            if added_at is None:
                conn.execute(
                    """INSERT INTO assets
                         (symbol, storage_symbol, name, exchange, currency,
                          asset_class, asset_subclass, isin, country,
                          updated_at, added_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               EXTRACT(epoch FROM now())::bigint,
                               EXTRACT(epoch FROM now())::bigint)
                       ON CONFLICT (symbol) DO NOTHING""",
                    (symbol, storage_symbol, name, exchange, currency,
                     asset_class, asset_subclass, isin, country),
                )
            else:
                conn.execute(
                    """INSERT INTO assets
                         (symbol, storage_symbol, name, exchange, currency,
                          asset_class, asset_subclass, isin, country,
                          updated_at, added_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s,
                               EXTRACT(epoch FROM now())::bigint, %s)
                       ON CONFLICT (symbol) DO NOTHING""",
                    (symbol, storage_symbol, name, exchange, currency,
                     asset_class, asset_subclass, isin, country, int(added_at)),
                )

    return _seed


@pytest.fixture
def seed_feed(pg_url: str):
    """Insert a feed row directly into the feeds table. Useful for tests
    that need to verify status aggregation without round-tripping through
    POST /v1/assets."""
    import psycopg

    def _seed(
        storage_symbol: str,
        timeframe: str,
        provider_symbol: str | None = None,
        status: str = "pending",
        provider: str = "tradingview",
    ) -> None:
        ps = provider_symbol or storage_symbol
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO feeds
                     (storage_symbol, timeframe, provider_symbol, provider, status, updated_at)
                   VALUES (%s, %s, %s, %s, %s, EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (storage_symbol, timeframe, provider) DO UPDATE SET
                     provider_symbol = EXCLUDED.provider_symbol,
                     status          = EXCLUDED.status,
                     updated_at      = EXCLUDED.updated_at""",
                (storage_symbol, timeframe, ps, provider, status),
            )

    return _seed


@pytest.fixture
def seed_bar(pg_url: str):
    """Insert a single bar (+ its cache_meta row) directly into Postgres."""
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
        provider: str = "tradingview",
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
                     (symbol, timeframe, provider, ts, open, high, low, close, volume, fetched_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol, timeframe, provider, ts) DO NOTHING""",
                (storage_symbol, timeframe, provider, ts, open_, high, low, close, volume),
            )
            conn.execute(
                """INSERT INTO cache_meta
                     (symbol, timeframe, provider, last_bar_ts, bar_count, last_fetched_at)
                   VALUES (%s, %s, %s, %s,
                           (SELECT COUNT(*) FROM bars
                              WHERE symbol=%s AND timeframe=%s AND provider=%s),
                           EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol, timeframe, provider) DO UPDATE SET
                     last_bar_ts=GREATEST(cache_meta.last_bar_ts, EXCLUDED.last_bar_ts),
                     bar_count=EXCLUDED.bar_count,
                     last_fetched_at=EXCLUDED.last_fetched_at""",
                (storage_symbol, timeframe, provider, ts, storage_symbol, timeframe, provider),
            )

    return _seed
