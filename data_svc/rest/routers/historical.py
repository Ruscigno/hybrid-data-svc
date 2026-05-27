"""/v1/historical/{symbol} — OHLCV bars over a date range.

Thin adapter:
  1. Resolve TV id → storage id via AssetsRepo.
  2. Pull rows from BarCache.get_bars_in_range.
  3. Wrap in HistoricalResponse.

The 5000-bar cap and `truncated` flag are decided in BarCache (sentinel +1
trick); this router just surfaces the result.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .._responses import HISTORICAL_RESPONSES
from ..auth import require_bearer
from ..deps import get_assets_repo, get_bar_cache
from ..models import Bar, HistoricalResponse, Timeframe
from ...db.assets import AssetsRepo
from ...db.cache import BarCache

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
    assets: AssetsRepo = Depends(get_assets_repo),
    bar_cache: BarCache = Depends(get_bar_cache),
) -> HistoricalResponse:
    if from_ >= to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "bad_range",
                "message": "`from` must be strictly less than `to`",
            },
        )
    asset = assets.resolve(symbol)
    if asset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_symbol",
                "message": f"{symbol} is not in the asset catalog",
            },
        )
    rows, truncated = bar_cache.get_bars_in_range(
        asset.storage_symbol, interval.value, from_, to, _MAX_BARS
    )
    return HistoricalResponse(
        symbol=asset.symbol,
        interval=interval,
        bars=[Bar(**r) for r in rows],
        truncated=True if truncated else None,
    )
