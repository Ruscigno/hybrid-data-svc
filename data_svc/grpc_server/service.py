"""
BarService gRPC implementation. Reads straight from Postgres via BarCache.
Never triggers TradingView fetches — the data-svc writer process owns that path.
"""
from __future__ import annotations

import logging
import os

from ..config import DataSvcConfig
from ..db.cache import BarCache

# Generated stubs (see data_svc/grpc_server/proto/__init__.py for path bootstrap).
from .proto import bars_pb2, bars_pb2_grpc  # noqa: F401  (imported for side effect)
from .proto import bars_pb2 as _pb
from .proto import bars_pb2_grpc as _pb_grpc

logger = logging.getLogger(__name__)

_MAX_BARS = 5000


class BarServiceServicer(_pb_grpc.BarServiceServicer):
    def __init__(self, cache: BarCache) -> None:
        self._cache = cache

    def GetRecentBars(self, request, context):  # noqa: N802 (gRPC naming)
        symbol = request.symbol
        timeframe = request.timeframe
        count = max(1, min(int(request.count or 300), _MAX_BARS))

        df = self._cache.read_bars(symbol, timeframe, count)
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
        return _pb.BarsResponse(bars=bars)

    def HealthCheck(self, request, context):  # noqa: N802
        symbol = request.symbol
        timeframe = request.timeframe
        min_bars = int(request.min_bars or 200)

        count = self._cache.bar_count(symbol, timeframe)
        last_ts = self._cache.latest_bar_ts(symbol, timeframe) or 0
        ready = count >= min_bars
        return _pb.HealthResponse(
            ready=ready,
            bars_available=int(count),
            last_bar_ts=int(last_ts),
        )


def build_servicer() -> BarServiceServicer:
    pg_url = os.getenv(
        "POSTGRES_URL",
        "postgresql://datasvc:datasvc@postgres:5432/datasvc",
    )
    cache = BarCache(pg_url)
    return BarServiceServicer(cache)
