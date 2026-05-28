"""/v1/quote/{symbol} — latest closed-bar price.

Thin gateway over BarService gRPC (spec §Motivation):

  1. Resolve TV symbol → ccxt storage_symbol + quote currency via AssetsRepo
     (the spec only says the gateway "may have to infer this from a mapping
     table"; the assets catalog IS that mapping table).
  2. Call BarService.GetRecentBars(storage_symbol, timeframe, count=1) via
     the gRPC client.
  3. Compose the Quote response. No business logic in this route.
"""

from __future__ import annotations

import time

import grpc
from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .._responses import QUOTE_RESPONSES
from ..auth import require_bearer
from ..deps import get_assets_repo, get_grpc_client
from ..grpc_client import BarServiceClient
from ..models import Quote, Timeframe
from ...db.assets import AssetsRepo

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
    assets: AssetsRepo = Depends(get_assets_repo),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> Quote:
    asset = assets.resolve(symbol)
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
