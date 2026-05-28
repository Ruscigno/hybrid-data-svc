"""/v1/historical/{symbol} — OHLCV bars over a date range.

Thin gateway. Two gRPC calls:
  1. AssetService.GetProfile to resolve TV → storage_symbol.
  2. BarService.GetBarsInRange with the resolved storage_symbol.
"""

from __future__ import annotations

import grpc
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .._responses import HISTORICAL_RESPONSES
from .._timeframes import rest_to_storage
from ..auth import require_bearer
from ..deps import get_grpc_client
from ..grpc_client import BarServiceClient
from ..models import Bar, HistoricalResponse, Timeframe

router = APIRouter(prefix="/v1", tags=["historical"], dependencies=[Depends(require_bearer)])

_MAX_BARS = 5000


@router.get(
    "/historical/{symbol}",
    response_model=HistoricalResponse,
    response_model_exclude_none=True,
    operation_id="getHistorical",
    summary="OHLCV bars over a date range.",
    responses=HISTORICAL_RESPONSES,
)
def get_historical(
    symbol: str = Path(
        ...,
        min_length=3,
        max_length=32,
        pattern=r"^[A-Z0-9]+:[A-Z0-9._/-]+$",
    ),
    from_: int = Query(..., ge=0, alias="from"),
    to: int = Query(..., ge=0),
    interval: Timeframe = Query(Timeframe.field_1_d),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> HistoricalResponse:
    if from_ >= to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_range",
                "message": "`from` must be strictly less than `to`",
            },
        )

    asset = grpc_client.get_asset_profile(symbol)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_symbol",
                "message": f"{symbol} is not in the configured feed list",
            },
        )

    try:
        rows, truncated = grpc_client.bars_in_range(
            asset.storage_symbol, rest_to_storage(interval.value), from_, to, _MAX_BARS
        )
    except grpc.RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "grpc_unavailable",
                "message": f"BarService unreachable: {exc.code().name if exc.code() else 'UNKNOWN'}",
            },
        ) from exc

    return HistoricalResponse(
        symbol=asset.symbol,
        interval=interval,
        bars=[Bar(ts=r.ts, open=r.open, high=r.high, low=r.low, close=r.close, volume=r.volume) for r in rows],
        truncated=True if truncated else None,
    )
