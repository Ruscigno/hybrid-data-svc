"""gRPC client wrapper for BarService.

Used by Phase 1 routes (/v1/quote and /healthz) to honour the spec's
"thin gateway — just call into the existing BarService" architecture.

A single insecure channel + stub is held on the app state and reused for
the process lifetime; calls are synchronous (uvicorn worker is fine for
the load Ghostfolio puts on this — one request per asset per hour).

Phase 2 routes (/v1/historical, /v1/search, /v1/profile) do NOT use this
client — they hit Postgres directly via AssetsRepo / BarCache. The
existing BarService proto has no RPCs for range queries, asset search,
or profile lookups; expanding the proto is out of scope here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import grpc

# The proto stubs live at data_svc/grpc_server/proto/ and rely on a sys.path
# hack in that package's __init__.py to resolve `import bars_pb2` from
# bars_pb2_grpc.
from ..grpc_server.proto import bars_pb2 as _pb
from ..grpc_server.proto import bars_pb2_grpc as _pb_grpc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BarRow:
    """Plain-Python view of a proto Bar — keeps the route layer free of
    proto types in case the wire format changes."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class HealthRow:
    ready: bool
    bars_available: int
    last_bar_ts: int


class BarServiceClient:
    """Long-lived BarService gRPC client. Thread-safe (grpcio channels are)."""

    def __init__(self, target: str) -> None:
        self._target = target
        # Insecure channel — bar-rest and bar-grpc share the docker network
        # behind a single trust boundary. TLS is not configured for the
        # internal hop.
        self._channel = grpc.insecure_channel(target)
        self._stub = _pb_grpc.BarServiceStub(self._channel)
        logger.info("[grpc-client] BarServiceClient initialised target=%s", target)

    def close(self) -> None:
        try:
            self._channel.close()
        except Exception:  # pragma: no cover
            logger.warning("[grpc-client] error closing channel", exc_info=True)

    def latest_bar(
        self,
        storage_symbol: str,
        timeframe: str,
        timeout: float = 3.0,
    ) -> Optional[BarRow]:
        """Return the most recent bar from BarService.GetRecentBars(count=1)
        or None if the service has no bars for that (symbol, timeframe).

        Raises grpc.RpcError on transport/unavailable errors — the caller
        translates that into a 503 response.
        """
        req = _pb.GetRecentBarsRequest(symbol=storage_symbol, timeframe=timeframe, count=1)
        resp = self._stub.GetRecentBars(req, timeout=timeout)
        if not resp.bars:
            return None
        b = resp.bars[-1]
        return BarRow(
            ts=int(b.ts),
            open=float(b.open),
            high=float(b.high),
            low=float(b.low),
            close=float(b.close),
            volume=float(b.volume),
        )

    def ping(self, timeout: float = 1.5) -> bool:
        """Best-effort liveness check against BarService.HealthCheck.

        We pass a sentinel symbol/timeframe ("__healthz__"/"1D", min_bars=0).
        The servicer responds with ready=False and bars_available=0 for an
        unknown symbol — that's fine; we only need the RPC round-trip to
        succeed. Returns True iff the RPC returned without raising.
        """
        try:
            self._stub.HealthCheck(
                _pb.HealthRequest(symbol="__healthz__", timeframe="1D", min_bars=0),
                timeout=timeout,
            )
            return True
        except grpc.RpcError as exc:  # pragma: no cover (covered in tests)
            logger.warning("[grpc-client] HealthCheck failed: %s", exc)
            return False
