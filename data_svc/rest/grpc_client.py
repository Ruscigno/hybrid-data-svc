"""gRPC clients for BarService + AssetService.

Used by all REST routes to honour the spec's "thin gateway — no business
logic, just call into BarService and serialize the response" architecture.

A single insecure channel is shared by both stubs and is held on the app
state for the process lifetime. Calls are synchronous (uvicorn worker is
fine for the load Ghostfolio puts on this — one request per asset per hour).
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
    """Plain-Python view of a proto Bar — keeps route handlers free of
    proto types in case the wire format changes."""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class AssetRow:
    """Plain-Python view of a proto Asset."""

    symbol: str
    storage_symbol: str
    name: str
    exchange: str
    currency: str
    asset_class: str
    asset_subclass: Optional[str] = None
    isin: Optional[str] = None
    country: Optional[str] = None


def _asset_message_to_row(msg: _pb.Asset) -> AssetRow:
    return AssetRow(
        symbol=msg.symbol,
        storage_symbol=msg.storage_symbol,
        name=msg.name,
        exchange=msg.exchange,
        currency=msg.currency,
        asset_class=msg.asset_class,
        # proto3 scalar default is "" — surface as None so response_model_exclude_none
        # drops them from the JSON output, per spec §"/v1/profile".
        asset_subclass=msg.asset_subclass or None,
        isin=msg.isin or None,
        country=msg.country or None,
    )


class BarServiceClient:
    """Long-lived BarService + AssetService client over a shared channel."""

    def __init__(self, target: str) -> None:
        self._target = target
        # Insecure channel — bar-rest and bar-grpc share a docker network
        # behind a single trust boundary; TLS is not configured for the
        # internal hop.
        self._channel = grpc.insecure_channel(target)
        self._bars = _pb_grpc.BarServiceStub(self._channel)
        self._assets = _pb_grpc.AssetServiceStub(self._channel)
        logger.info("[grpc-client] initialised target=%s", target)

    def close(self) -> None:
        try:
            self._channel.close()
        except Exception:  # pragma: no cover
            logger.warning("[grpc-client] error closing channel", exc_info=True)

    # --- BarService -------------------------------------------------------

    def latest_bar(
        self,
        storage_symbol: str,
        timeframe: str,
        timeout: float = 3.0,
    ) -> Optional[BarRow]:
        """Return the most recent bar via BarService.GetRecentBars(count=1)
        or None if the service has no bars for that (symbol, timeframe).

        Raises grpc.RpcError on transport/unavailable errors — the caller
        translates that into a 503 response.
        """
        req = _pb.GetRecentBarsRequest(symbol=storage_symbol, timeframe=timeframe, count=1)
        resp = self._bars.GetRecentBars(req, timeout=timeout)
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

    def bars_in_range(
        self,
        storage_symbol: str,
        timeframe: str,
        from_ts: int,
        to_ts: int,
        limit: int,
        timeout: float = 5.0,
    ) -> tuple[list[BarRow], bool]:
        """Return (rows, truncated) via BarService.GetBarsInRange.

        rows is ascending by ts; truncated is True when the server capped
        the result at `limit` bars (most recent N kept).
        """
        req = _pb.GetBarsInRangeRequest(
            symbol=storage_symbol,
            timeframe=timeframe,
            from_ts=int(from_ts),
            to_ts=int(to_ts),
            limit=int(limit),
        )
        resp = self._bars.GetBarsInRange(req, timeout=timeout)
        rows = [
            BarRow(
                ts=int(b.ts),
                open=float(b.open),
                high=float(b.high),
                low=float(b.low),
                close=float(b.close),
                volume=float(b.volume),
            )
            for b in resp.bars
        ]
        return rows, bool(resp.truncated)

    def ping(self, timeout: float = 1.5) -> tuple[bool, bool]:
        """Returns (grpc_reachable, db_reachable) via BarService.Ping.

        grpc_reachable=False when the RPC raises on transport; in that
        case db_reachable is also False because we couldn't ask the server.
        """
        try:
            resp = self._bars.Ping(_pb.PingRequest(), timeout=timeout)
            return True, bool(resp.db_reachable)
        except grpc.RpcError as exc:
            logger.warning("[grpc-client] Ping failed: %s", exc)
            return False, False

    # --- AssetService -----------------------------------------------------

    def get_asset_profile(
        self,
        tv_symbol: str,
        timeout: float = 1.5,
    ) -> Optional[AssetRow]:
        """Return the asset row, or None if NOT_FOUND."""
        try:
            msg = self._assets.GetProfile(
                _pb.GetAssetProfileRequest(symbol=tv_symbol),
                timeout=timeout,
            )
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                return None
            raise
        return _asset_message_to_row(msg)

    def search_assets(
        self,
        query: str,
        limit: int,
        timeout: float = 2.0,
    ) -> list[AssetRow]:
        resp = self._assets.Search(
            _pb.SearchAssetsRequest(query=query, limit=int(limit)),
            timeout=timeout,
        )
        return [_asset_message_to_row(m) for m in resp.results]
