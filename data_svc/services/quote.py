"""Quote composition: assets catalog + latest closed bar.

Both the REST /v1/quote route and any future gRPC GetQuote RPC delegate to
QuoteService.get_latest() — there is one place that decides "what is the
quote shape for this symbol and how do we mark it stale". That place is
this module.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..db.assets import AssetsRepo, AssetRow
from ..db.cache import BarCache


class QuoteOutcome(Enum):
    OK = "ok"
    UNKNOWN_SYMBOL = "unknown_symbol"
    NO_DATA = "no_data"


@dataclass(frozen=True)
class QuoteResult:
    """Outcome wrapper. Transport adapters map outcomes to wire errors.

    On OK, every field is populated. On UNKNOWN_SYMBOL / NO_DATA, only
    `outcome` is meaningful.
    """

    outcome: QuoteOutcome
    symbol: Optional[str] = None
    timeframe: Optional[str] = None
    price: Optional[float] = None
    currency: Optional[str] = None
    ts: Optional[int] = None
    stale: Optional[bool] = None


class QuoteService:
    def __init__(self, assets: AssetsRepo, cache: BarCache) -> None:
        self._assets = assets
        self._cache = cache

    def get_latest(
        self,
        tv_symbol: str,
        timeframe: str,
        max_age_seconds: int,
    ) -> QuoteResult:
        asset: Optional[AssetRow] = self._assets.resolve(tv_symbol)
        if asset is None:
            return QuoteResult(outcome=QuoteOutcome.UNKNOWN_SYMBOL)

        bar = self._cache.latest_bar(asset.storage_symbol, timeframe)
        if bar is None:
            return QuoteResult(outcome=QuoteOutcome.NO_DATA)

        now = int(time.time())
        stale = (now - bar["ts"]) > max(1, max_age_seconds)
        return QuoteResult(
            outcome=QuoteOutcome.OK,
            symbol=asset.symbol,
            timeframe=timeframe,
            price=float(bar["close"]),
            currency=asset.currency,
            ts=int(bar["ts"]),
            stale=stale,
        )
