"""/v1/quote/{symbol} — latest closed-bar price.

Thin adapter: delegates to QuoteService.get_latest() and maps the outcome
to either a `Quote` payload or one of the documented error responses.
No business logic lives here.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status

from .._responses import QUOTE_RESPONSES
from ..auth import require_bearer
from ..deps import get_quote_service
from ..models import Quote, Timeframe
from ...services.quote import QuoteOutcome, QuoteService

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
    svc: QuoteService = Depends(get_quote_service),
) -> Quote:
    result = svc.get_latest(symbol, timeframe.value, max_age_seconds)
    if result.outcome is QuoteOutcome.UNKNOWN_SYMBOL:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_symbol",
                "message": f"{symbol} is not in the asset catalog",
            },
        )
    if result.outcome is QuoteOutcome.NO_DATA:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "no_data",
                "message": f"No bars available for {symbol}@{timeframe.value}",
            },
        )
    return Quote(
        symbol=result.symbol,
        timeframe=Timeframe(result.timeframe),
        price=result.price,
        currency=result.currency,
        ts=result.ts,
        stale=result.stale,
    )
