"""BarService gRPC implementation. Reads straight from Postgres via BarCache.
Never triggers TradingView fetches — the data-svc writer process owns that path.
"""
from __future__ import annotations

import logging
import os

import grpc

from ..db.cache import BarCache
from ..db.postgres import get_pool

# Generated stubs (see data_svc/grpc_server/proto/__init__.py for path bootstrap).
from .proto import bars_pb2, bars_pb2_grpc  # noqa: F401  (imported for side effect)
from .proto import bars_pb2 as _pb
from .proto import bars_pb2_grpc as _pb_grpc

logger = logging.getLogger(__name__)

_MAX_BARS = 5000
_DEFAULT_RANGE_LIMIT = _MAX_BARS


class BarServiceServicer(_pb_grpc.BarServiceServicer):
    def __init__(self, cache: BarCache, postgres_url: str) -> None:
        self._cache = cache
        self._postgres_url = postgres_url

    def GetRecentBars(self, request, context):  # noqa: N802 (gRPC naming)
        symbol = request.symbol
        timeframe = request.timeframe
        count = max(1, min(int(request.count or 300), _MAX_BARS))
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        df = self._cache.read_bars(symbol, timeframe, count, provider)
        bars = [
            _pb.Bar(
                ts=int(row["time"]),
                open=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                close=float(row["close"]),
                volume=float(row["volume"]),
            )
            for _, row in df.iterrows()
        ]
        return _pb.BarsResponse(bars=bars, truncated=False)

    def GetBarsInRange(self, request, context):  # noqa: N802
        symbol = request.symbol
        timeframe = request.timeframe
        from_ts = int(request.from_ts)
        to_ts = int(request.to_ts)
        requested = int(request.limit) if request.limit else _DEFAULT_RANGE_LIMIT
        limit = max(1, min(requested, _MAX_BARS))
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        rows, truncated = self._cache.get_bars_in_range(symbol, timeframe, from_ts, to_ts, limit, provider)
        bars = [
            _pb.Bar(
                ts=int(r["ts"]),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=float(r["volume"]),
            )
            for r in rows
        ]
        return _pb.BarsResponse(bars=bars, truncated=truncated)

    def HealthCheck(self, request, context):  # noqa: N802
        symbol = request.symbol
        timeframe = request.timeframe
        min_bars = int(request.min_bars or 200)
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        count = self._cache.bar_count(symbol, timeframe, provider)
        last_ts = self._cache.latest_bar_ts(symbol, timeframe, provider) or 0
        ready = count >= min_bars
        return _pb.HealthResponse(
            ready=ready,
            bars_available=int(count),
            last_bar_ts=int(last_ts),
        )

    def Ping(self, request, context):  # noqa: N802
        """Verifies Postgres is reachable via SELECT 1.

        Returns db_reachable=true iff the pool round-trips successfully.
        Used by the REST gateway's /healthz route to populate db_reachable
        without holding its own Postgres connection.
        """
        try:
            pool = get_pool(self._postgres_url)
            with pool.connection() as conn:
                conn.execute("SELECT 1").fetchone()
            return _pb.PingResponse(db_reachable=True)
        except Exception as exc:
            logger.warning("[grpc] Ping: db unreachable: %s", exc)
            return _pb.PingResponse(db_reachable=False)


def build_servicer() -> BarServiceServicer:
    pg_url = os.getenv(
        "POSTGRES_URL",
        "postgresql://datasvc:datasvc@postgres:5432/datasvc",
    )
    cache = BarCache(pg_url)
    return BarServiceServicer(cache, pg_url)
