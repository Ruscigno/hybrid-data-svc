"""/v1/quote/{symbol} — latest closed-bar price.

Thin gateway. Two gRPC calls:
  1. AssetService.GetProfile(tv_symbol) — TV → storage_symbol + currency.
     Returns NOT_FOUND when symbol isn't in the configured feed list.
  2. BarService.GetRecentBars(storage_symbol, timeframe, 1) — latest bar.
     Empty result → 503 no_data.
"""

from __future__ import annotations

import time

import grpc
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .._responses import QUOTE_RESPONSES
from ..auth import require_bearer
from ..deps import get_grpc_client
from ..grpc_client import BarServiceClient
from ..models import Quote, Timeframe

router = APIRouter(prefix="/v1", tags=["quote"], dependencies=[Depends(require_bearer)])


@router.get(
    "/quote/{symbol}",
    response_model=Quote,
    response_model_exclude_none=True,
    operation_id="getQuote",
    summary="Latest closed-bar price for a symbol.",
    responses=QUOTE_RESPONSES,
)
def get_quote(
    symbol: str = Path(
        ...,
        min_length=3,
        max_length=32,
        pattern=r"^[A-Z0-9]+:[A-Z0-9._/-]+$",
        description="TradingView identifier, uppercase, `:`-separated. URL-encode `:` as `%3A`.",
    ),
    timeframe: Timeframe = Query(Timeframe.field_1_d),
    max_age_seconds: int = Query(3600, ge=1),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> Quote:
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
        bar = grpc_client.latest_bar(asset.storage_symbol, timeframe.value)
    except grpc.RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "grpc_unavailable",
                "message": f"BarService unreachable: {exc.code().name if exc.code() else 'UNKNOWN'}",
            },
        ) from exc

    if bar is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "no_data",
                "message": f"No bars available for {symbol}@{timeframe.value}",
            },
        )

    now = int(time.time())
    stale = (now - bar.ts) > max_age_seconds
    return Quote(
        symbol=asset.symbol,
        timeframe=timeframe,
        price=float(bar.close),
        currency=asset.currency,
        ts=int(bar.ts),
        stale=stale,
    )
